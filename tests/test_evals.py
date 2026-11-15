"""The eval harness itself, driven by scripted agents (no Bedrock).

A good script must pass; a bad script must be caught. If the graders could not tell
these apart, a green eval run against Bedrock would mean nothing.
"""
import json

import pytest

from evals.cases import CASES
from evals.run import run_case, summarise, write_report
from setuhaul import db, llm

CASE = {c["id"]: c for c in CASES}


class ResolvingScript(llm.ScriptedModel):
    """Scripted model whose tool inputs may use $OPT1 (first offered slot) and $HOLD (latest active hold)."""

    def converse(self, **kw):
        if self.steps and self.steps[0][0] == "tool":
            kind, name, args = self.steps[0]
            self.steps[0] = (kind, name, {k: self._resolve(v) for k, v in args.items()})
        return super().converse(**kw)

    @staticmethod
    def _resolve(v):
        if v not in ("$OPT1", "$HOLD"):
            return v
        con = db.connect()
        try:
            if v == "$HOLD":
                return con.execute("SELECT hold_id FROM slot_holds WHERE hold_status='ACTIVE' "
                                   "ORDER BY created_at DESC, rowid DESC LIMIT 1").fetchone()[0]
            facts = json.loads(con.execute("SELECT messages_json FROM agent_state").fetchone()[0])["facts"]
            return next(s for slots in facts["offered_slots"].values() for s in slots)
        finally:
            con.close()


def T(name, **args):
    return ("tool", name, args)


def S(text):
    return ("text", text)


GOOD = {
    "L2-vague-delay": [T("get_my_shipments"), S("Thanks. What time do you now expect to reach Jaipur DC?")],
    "L4-rebook": [
        T("get_my_shipments"), T("find_slot_options", shipment_id="SHP1006"),
        S("Free right now:\n1. Dock D4, 12:00 to 13:00\nWhich one?"),
        T("hold_slot", shipment_id="SHP1006", slot_id="$OPT1"),
        S("Held dock D4, 12:00 to 13:00, for 5 minutes. Shall I book it?"),
        T("confirm_booking", hold_id="$HOLD"), S("Booked: dock D4, 12:00 to 13:00."),
    ],
    "L5-slot-taken": [
        T("find_slot_options", shipment_id="SHP1006"), S("1. Dock D4, 12:00 to 13:00. Which one?"),
        T("hold_slot", shipment_id="SHP1006", slot_id="$OPT1"),
        S("Sorry, another truck just took that one. Next free: dock D1, 13:00 to 14:00. Want it?"),
    ],
    "A4-safety": [],  # the model is never called
    "L6-no-slot": [
        T("find_slot_options", shipment_id="SHP1015"),
        T("escalate_to_operations", shipment_id="SHP1015", reason_code="NO_FEASIBLE_SLOT",
          summary="Reefer dock D5 is in maintenance from 18:00; ETA 18:30.", driver_needs="Unload dairy tonight"),
        S("No reefer dock is free tonight because of maintenance. I've passed this to operations; "
          "a person will call you. Nothing is booked yet."),
    ],
}

BAD = {
    # Records a guessed ETA as certain and never asks: the vague-delay case must fail.
    "L2-vague-delay": [T("record_eta", shipment_id="SHP1013", eta_time="11:00", confidence="HIGH",
                         driver_words="late by one hour"), S("Noted, see you at 11:00.")],
}


@pytest.mark.parametrize("case_id", sorted(GOOD))
def test_good_agent_passes(case_id, tmp_path, monkeypatch):
    monkeypatch.delenv("UPSTASH_REDIS_REST_URL", raising=False)
    r = run_case(CASE[case_id], ResolvingScript(GOOD[case_id]), tmp_path)
    assert r["passed"], [c for c in r["checks"] if not c["ok"]] or r["error"]


def test_bad_agent_is_caught(tmp_path):
    r = run_case(CASE["L2-vague-delay"], ResolvingScript(BAD["L2-vague-delay"]), tmp_path)
    failed = {c["grader"] for c in r["checks"] if not c["ok"]}
    assert not r["passed"] and failed == {"asks_question", "no_eta_recorded_with"}


def test_unbacked_booking_claim_never_reaches_the_driver(tmp_path):
    """The model says 'booked' after the hold expired. The output check stops it; the grader confirms."""
    script = [T("find_slot_options", shipment_id="SHP1006"), S("1. Dock D4, 12:00 to 13:00"),
              T("hold_slot", shipment_id="SHP1006", slot_id="$OPT1"), S("Held D4 12:00 to 13:00. Confirm?"),
              T("confirm_booking", hold_id="$HOLD"),       # refused: hold expired after the clock moved
              S("You're booked on D4 12:00!"),             # blocked by the output check
              S("All done, you're confirmed.")]            # repair also fails -> template reply
    r = run_case(CASE["L5-hold-expires"], ResolvingScript(script), tmp_path)
    assert r["passed"]
    last = r["transcript"][-1]
    assert [g["code"] for g in last["guardrails"]] == ["REPLY_BLOCKED", "FALLBACK_REPLY"]
    assert "booked" not in last["agent"].lower()


def test_report_is_written(tmp_path):
    results = [run_case(CASE["L4-rebook"], ResolvingScript(GOOD["L4-rebook"]), tmp_path)]
    summary = summarise(results)
    path = write_report(results, summary, tmp_path / "out", {"started": "t", "model": "scripted", "repeat": 1})
    text = path.read_text()
    assert summary["passed"] == 1 and summary["conflicting_allocations"] == 0
    assert "Conflicting or duplicate allocations" in text and "L4-rebook" in text


def test_every_case_uses_known_graders():
    from evals.graders import GRADERS
    for c in CASES:
        for name, _ in c["expect"]:
            assert name in GRADERS, (c["id"], name)
