"""Run the eval cases against the agent and write a report.

    python -m evals.run                          # all cases, Bedrock (needs BEDROCK_MODEL_ID)
    python -m evals.run --cases L4-rebook,A1-injection
    python -m evals.run --repeat 3               # each case 3 times: shows flaky behaviour
    python -m evals.run --judge                  # also score tone and clarity with a model judge

Every case runs on its own fresh copy of the snapshot, so cases cannot affect each other.
Reports go to evals/results/<timestamp>/ as report.md and results.json.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

from setuhaul import agent, capacity as cap, clock, db, llm, memory as mem
from setuhaul.config import load_env

from .cases import CASES
from .graders import GRADERS, Run

RESULTS_DIR = Path(__file__).parent / "results"


def run_case(case: dict, model: llm.Model, workdir: Path | None = None) -> dict:
    workdir = workdir or Path(tempfile.mkdtemp(prefix="eval-"))
    path = workdir / f"{case['id']}.db"
    os.environ["SETUHAUL_DB"] = str(path)
    db.reset(path, start=case["start"], frozen=True)
    con = db.connect(path)
    memory = mem.SqliteMemory(db.connect)
    counter = llm.CountingModel(model)
    thread = agent.start_thread(con, case["driver"])
    owned = {r[0] for r in con.execute("SELECT shipment_id FROM shipments WHERE driver_id=?", (case["driver"],))}
    baseline = {r["appointment_id"]: (r["shipment_id"], r["appointment_status"], r["is_current"], r["slot_id"])
                for r in con.execute("SELECT * FROM appointments")}
    run = Run(case, con, thread, case["driver"], owned, baseline)
    started = time.time()
    for step in case["steps"]:
        try:
            if "say" in step:
                r = agent.handle_message(thread, step["say"], memory=memory, model=counter)
                r["driver"] = step["say"]
                r.setdefault("guardrails", [])
                run.turns.append(r)
            elif "advance" in step:
                clock.set_now(con, clock.add_min(clock.now(con), step["advance"]), frozen=True)
            elif "other_truck_takes" in step:
                _other_truck_takes(run, memory, step["other_truck_takes"])
        except llm.ModelUnavailable as e:
            run.error = str(e)
            break
        except Exception as e:  # a crash is a failed case, not a crashed eval run
            run.error = f"{type(e).__name__}: {e}"
            break
    checks = []
    if not run.error:
        for name, kwargs in [*case["expect"], ("integrity", {})]:
            ok, detail = GRADERS[name](run, **kwargs)
            checks.append({"grader": name, "args": kwargs, "ok": bool(ok), "detail": detail})
    result = {
        "id": case["id"], "level": case["level"], "title": case["title"],
        "passed": not run.error and all(c["ok"] for c in checks),
        "error": run.error, "checks": checks,
        "transcript": [{"driver": t["driver"], "agent": t["reply"], "duplicate": t.get("duplicate"),
                        "tools": [{"tool": c["tool"], "input": c["input"], "ok": c["ok"],
                                   "outcome": _outcome(c["result"])} for c in t.get("tool_calls", [])],
                        "guardrails": t.get("guardrails", [])} for t in run.turns],
        "metrics": _metrics(run, counter, time.time() - started),
    }
    con.close()
    return result


def _other_truck_takes(run: Run, memory, other_shipment: str) -> None:
    """Another truck books the first slot this driver was offered (brief: the world changes)."""
    facts = memory.load(run.thread_id)["facts"]
    offered = next((slots for s, slots in facts.get("offered_slots", {}).items() if slots), None)
    if not offered:
        raise RuntimeError("setup: the agent offered no slot to take")
    now = clock.now(run.con)
    h = cap.hold_slot(run.con, other_shipment, offered[0], now, actor="eval-setup")
    if not h.get("ok") or not cap.confirm_booking(run.con, h["hold_id"], now, actor="eval-setup").get("ok"):
        raise RuntimeError(f"setup: {other_shipment} could not take {offered[0]}: {h}")
    run.taken_slot = offered[0]
    run.helpers.add(other_shipment)


def _outcome(result: dict) -> str:
    return str(result.get("status") or result.get("code") or result.get("appointment_id") or result.get("hold_id")
               or ("ok" if result.get("ok", True) else "refused"))


def _metrics(run: Run, counter: llm.CountingModel, seconds: float) -> dict:
    calls = list(run.all_calls())
    events = [e for t in run.turns for e in t.get("guardrails", [])]
    outcome_turn = next((i for i, t in enumerate(run.turns, 1)
                         if any(c["tool"] in ("confirm_booking", "escalate_to_operations") and c["ok"]
                                and c["result"].get("ok") for c in t.get("tool_calls", []))), None)
    return {
        "turns": len(run.turns),
        "clarification_turns": sum(1 for t in run.turns if "?" in t.get("reply", "")),
        "tool_calls": len(calls),
        "tool_refusals": sum(1 for c in calls if not c["ok"]),
        "gate_refusals": sum(1 for e in events if e["stage"] == "tool"),
        "replies_blocked": sum(1 for e in events if e["code"] == "REPLY_BLOCKED"),
        "replies_repaired": sum(1 for e in events if e["code"] == "REPLY_REPAIRED"),
        "fallback_replies": sum(1 for e in events if e["code"] == "FALLBACK_REPLY"),
        "input_flags": sorted({e["code"] for e in events if e["stage"] == "input"}),
        "options_found_infeasible_at_hold": sum(1 for c in calls if c["tool"] == "hold_slot"
                                                and c["result"].get("code") in ("SLOT_NO_LONGER_AVAILABLE",
                                                                                "SLOT_NOT_FEASIBLE")),
        "outcome_turn": outcome_turn,
        "model_calls": counter.calls, "input_tokens": counter.input_tokens, "output_tokens": counter.output_tokens,
        "model_seconds": round(counter.seconds, 2), "wall_seconds": round(seconds, 2),
    }


def summarise(results: list[dict]) -> dict:
    total = len(results)
    by_level: dict[str, list] = {}
    for r in results:
        by_level.setdefault(r["level"], []).append(r["passed"])
    m = [r["metrics"] for r in results]
    integrity_fail = sum(1 for r in results for c in r["checks"] if c["grader"] == "integrity" and not c["ok"])
    return {
        "cases": total,
        "passed": sum(r["passed"] for r in results),
        "pass_rate": round(sum(r["passed"] for r in results) / total, 3) if total else 0,
        "by_level": {k: f"{sum(v)}/{len(v)}" for k, v in sorted(by_level.items())},
        "conflicting_allocations": integrity_fail,
        "replies_blocked": sum(x["replies_blocked"] for x in m),
        "replies_repaired": sum(x["replies_repaired"] for x in m),
        "fallback_replies": sum(x["fallback_replies"] for x in m),
        "gate_refusals": sum(x["gate_refusals"] for x in m),
        "options_found_infeasible_at_hold": sum(x["options_found_infeasible_at_hold"] for x in m),
        "avg_clarification_turns": round(sum(x["clarification_turns"] for x in m) / total, 2) if total else 0,
        "avg_model_seconds_per_case": round(sum(x["model_seconds"] for x in m) / total, 2) if total else 0,
        "tokens": {"input": sum(x["input_tokens"] for x in m), "output": sum(x["output_tokens"] for x in m)},
        "errors": [r["id"] for r in results if r["error"]],
    }


def write_report(results: list[dict], summary: dict, out: Path, meta: dict) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    (out / "results.json").write_text(json.dumps({"meta": meta, "summary": summary, "results": results},
                                                 indent=2, default=str))
    lines = [f"# Eval report: {meta['started']}", "",
             f"Model: `{meta['model']}` · cases: {summary['cases']} · passed: **{summary['passed']}** "
             f"({summary['pass_rate']:.0%})", "",
             "| Measure (brief rubric) | Value |", "| --- | --- |",
             f"| Conflicting or duplicate allocations | {summary['conflicting_allocations']} |",
             f"| Options found infeasible at hold time | {summary['options_found_infeasible_at_hold']} |",
             f"| Replies blocked by the output check | {summary['replies_blocked']} "
             f"(repaired {summary['replies_repaired']}, fallback {summary['fallback_replies']}) |",
             f"| Tool calls refused by guardrail gates | {summary['gate_refusals']} |",
             f"| Average clarification turns per case | {summary['avg_clarification_turns']} |",
             f"| Average model time per case | {summary['avg_model_seconds_per_case']} s |",
             f"| Tokens in / out | {summary['tokens']['input']} / {summary['tokens']['output']} |",
             "", "| Level | Passed |", "| --- | --- |",
             *[f"| {k} | {v} |" for k, v in summary["by_level"].items()], "",
             "| Case | Result | Failed checks |", "| --- | --- | --- |"]
    for r in results:
        failed = "; ".join(f"{c['grader']}: {c['detail']}" for c in r["checks"] if not c["ok"]) or (r["error"] or "")
        judge = f" · judge {r['judge']['overall']}/5" if r.get("judge", {}).get("overall") else ""
        lines.append(f"| {r['id']}: {r['title']} | {'PASS' if r['passed'] else 'FAIL'}{judge} | {failed} |")
    lines += ["", "## Transcripts", ""]
    for r in results:
        lines.append(f"### {r['id']} ({'PASS' if r['passed'] else 'FAIL'})")
        for t in r["transcript"]:
            lines.append(f"- **Driver:** {t['driver']}")
            for tool in t["tools"]:
                lines.append(f"  - `{tool['tool']}` → {tool['outcome']}")
            for g in t["guardrails"]:
                lines.append(f"  - guardrail: {g['code']}")
            lines.append(f"- **Agent:** {t['agent']}".replace("\n", " / "))
        if r.get("judge"):
            lines.append(f"- Judge: {json.dumps(r['judge'])}")
        lines.append("")
    path = out / "report.md"
    path.write_text("\n".join(lines))
    return path


def main(argv=None) -> int:
    load_env()
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--cases", help="comma-separated case ids (default: all)")
    p.add_argument("--repeat", type=int, default=1, help="run each case N times")
    p.add_argument("--judge", action="store_true", help="add a model-judged score for tone and clarity")
    args = p.parse_args(argv)
    chosen = [c for c in CASES if not args.cases or c["id"] in args.cases.split(",")]
    if not chosen:
        print("No matching cases.", file=sys.stderr)
        return 2
    try:
        model = llm.BedrockModel()
    except llm.ModelUnavailable as e:
        print(e, file=sys.stderr)
        return 2
    results = []
    for case in chosen:
        for i in range(args.repeat):
            r = run_case(case, model)
            if args.repeat > 1:
                r["id"] = f"{case['id']}#{i + 1}"
            if args.judge and not r["error"]:
                from .judge import judge
                r["judge"] = judge(case, r["transcript"], model)
            print(f"{'PASS' if r['passed'] else 'FAIL'}  {r['id']}  "
                  + "; ".join(c["detail"] for c in r["checks"] if not c["ok"]) + (r["error"] or ""))
            results.append(r)
    summary = summarise(results)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = write_report(results, summary, RESULTS_DIR / stamp,
                        {"started": stamp, "model": model.model_id, "repeat": args.repeat})
    print(f"\n{summary['passed']}/{summary['cases']} passed. Report: {path}")
    return 0 if summary["passed"] == summary["cases"] else 1


if __name__ == "__main__":
    sys.exit(main())
