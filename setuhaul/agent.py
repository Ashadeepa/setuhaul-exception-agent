"""The conversation agent: reads messy driver messages, asks for missing facts, calls tools.

It decides nothing about capacity. Feasibility, holds and bookings come only from tools.
"""
from __future__ import annotations

import json
import re
import uuid

from . import capacity as cap, clock, db, escalation, guardrails as gr, llm, memory as mem, tools

MAX_TOOL_ROUNDS = 8
HISTORY_LIMIT = 40
DEDUPE_WINDOW_MIN = 5

SYSTEM = """You are SetuHaul's dispatch assistant. Truck drivers message you when they are early, late or \
stuck. You help them get a dock appointment they can actually make at the destination warehouse.

Today is {date}. The time now is {now}. You are talking to {driver_name} ({driver_id}).
{earlier}
How you work:
- Start by calling get_my_shipments. Never ask the driver for anything the tools already tell you \
(booking, dock, ETA on file, whether they have checked in).
- If two or more active shipments could match, ask which one in one short question, naming them by \
destination and time. Use the hints: a truck already at the gate is not "running late".
- Ask the driver only for what only they know: where they are, a specific new arrival time, what a time \
limit means (for example "leave by 1:30": latest gate-out or latest unload start?), whether a breakdown is fixed.
- Turn vague delays into a specific time before searching. "Late by one hour" is not an ETA: ask for the \
arrival time. Record it with record_eta. A range like 12:30-13:00: use the later end and say so.
- Only find_slot_options decides what is possible. Never suggest a dock or time it did not return. \
Options reserve nothing; say so if the driver hesitates.
- When the driver picks an option, call hold_slot, then ask them to confirm that exact dock and time \
(held for 5 minutes). Call confirm_booking only after a clear yes to that held slot.
- If a hold or confirmation is refused, explain in plain words (another truck took it, the dock is now \
unavailable) and offer the fresh options returned.
- Early arrival does not jump the queue. Priority loads do not displace other bookings automatically: \
if find_slot_options returns a priority_flag, offer to escalate.
- Escalate with escalate_to_operations when no feasible slot exists, only manual-approval slots remain, \
a priority decision is needed, the data contradicts itself, there is any safety issue, or the driver asks \
for a person. Never invent an answer instead. After escalating, tell the driver a person will contact \
them; do not say the problem is solved. If the escalation failed, say so and give them the gate as fallback.
- A cancelled shipment gets no slot: tell the driver to contact dispatch before travelling.

Style: short WhatsApp-style replies, plain words, times as HH:MM, at most three options as a numbered \
list with dock and time. No internal codes or IDs except the dock name. Reply in the driver's language."""


class DuplicateMessage(Exception):
    def __init__(self, reply: str | None):
        self.reply = reply


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9:]+", " ", text.lower()).strip()


def start_thread(con, driver_id: str) -> str:
    d = con.execute("SELECT driver_id FROM drivers WHERE driver_id=?", (driver_id,)).fetchone()
    if d is None:
        raise ValueError(f"Unknown driver {driver_id}")
    n = con.execute("SELECT COUNT(*) FROM chat_threads WHERE thread_id LIKE 'THR-C%'").fetchone()[0]
    tid = f"THR-C{n + 1:04d}"
    con.execute("""INSERT INTO chat_threads(thread_id, driver_id, shipment_id, opened_at, thread_status, thread_intent)
                   VALUES (?,?,NULL,?, 'OPEN', 'UNKNOWN')""", (tid, driver_id, clock.now(con)))
    return tid


def _earlier_messages(con, driver_id: str, now: str) -> str:
    rows = con.execute("""SELECT m.sender_type, m.message_text, m.message_ts FROM chat_messages m
                          JOIN chat_threads t USING (thread_id)
                          WHERE t.driver_id=? AND t.thread_id NOT LIKE 'THR-C%' AND m.message_ts<=?
                            AND m.is_duplicate=0
                          ORDER BY m.message_ts DESC LIMIT 4""", (driver_id, now)).fetchall()
    if not rows:
        return ""
    lines = [f"  {clock.hhmm(r['message_ts'])} {r['sender_type'].lower()}: {r['message_text']}" for r in reversed(rows)]
    return ("\nEarlier messages from this driver today (already on file; do not ask again for what they said):\n"
            + "\n".join(lines) + "\n")


def _store_message(con, thread_id: str, sender: str, ref: str, text: str, now: str, *,
                   external_id: str | None = None, duplicate: bool = False) -> int:
    return con.execute("""INSERT INTO chat_messages(chat_message_id, thread_id, sender_type, sender_reference, message_text,
                       message_ts, external_message_id, is_duplicate)
                   VALUES (?,?,?,?,?,?,?,?)""",
                ("MSG-" + uuid.uuid4().hex[:10].upper(), thread_id, sender, ref, text, now, external_id,
                 1 if duplicate else 0)).lastrowid


def _check_duplicate(con, driver_id: str, thread_id: str, text: str, external_id: str | None, now: str) -> None:
    """Same message twice is answered once (audit G03): by external ID, or same text within 5 minutes."""
    if external_id and con.execute("SELECT 1 FROM chat_messages WHERE external_message_id=?",
                                   (external_id,)).fetchone():
        raise DuplicateMessage(_last_reply(con, thread_id))
    since = clock.add_min(now, -DEDUPE_WINDOW_MIN)
    for r in con.execute("""SELECT m.message_text, m.thread_id FROM chat_messages m JOIN chat_threads t USING (thread_id)
                            WHERE t.driver_id=? AND m.sender_type='DRIVER' AND m.message_ts>=? AND m.is_duplicate=0""",
                         (driver_id, since)):
        if _norm(r["message_text"]) == _norm(text):
            raise DuplicateMessage(_last_reply(con, r["thread_id"]))


def _last_reply(con, thread_id: str) -> str | None:
    r = con.execute("""SELECT message_text FROM chat_messages WHERE thread_id=? AND sender_type='AGENT'
                       ORDER BY message_ts DESC, rowid DESC LIMIT 1""", (thread_id,)).fetchone()
    return r["message_text"] if r else None


def _trim(messages: list) -> list:
    """Keep recent history, cutting only at a plain driver message so tool calls stay paired."""
    if len(messages) <= HISTORY_LIMIT:
        return messages
    for i in range(len(messages) - HISTORY_LIMIT, len(messages)):
        m = messages[i]
        if m["role"] == "user" and any("text" in b for b in m["content"]):
            return messages[i:]
    return messages[-HISTORY_LIMIT:]


def handle_message(thread_id: str, text: str, *, external_id: str | None = None,
                   memory: mem.Memory | None = None, model: llm.Model | None = None) -> dict:
    con = db.connect()
    memory = memory or mem.make_memory(db.connect)
    try:
        thread = con.execute("SELECT * FROM chat_threads WHERE thread_id=?", (thread_id,)).fetchone()
        if thread is None:
            raise ValueError("Unknown thread")
        driver = con.execute("SELECT * FROM drivers WHERE driver_id=?", (thread["driver_id"],)).fetchone()
        now = clock.now(con)
        try:
            _check_duplicate(con, driver["driver_id"], thread_id, text, external_id, now)
        except DuplicateMessage as d:
            _store_message(con, thread_id, "DRIVER", driver["driver_id"], text, now,
                           external_id=external_id, duplicate=True)
            cap.log(con, "agent", "dedupe", None, "DUPLICATE_IGNORED", {"thread": thread_id, "text": text})
            return {"thread_id": thread_id, "duplicate": True,
                    "reply": d.reply or "Got it, I'm already working on your earlier message.", "tool_calls": []}

        msg_rowid = _store_message(con, thread_id, "DRIVER", driver["driver_id"], text, now, external_id=external_id)
        verdict = gr.screen_input(text)
        if verdict.flags:
            cap.log(con, "guardrail", "screen_input", thread["shipment_id"], ",".join(verdict.flags),
                    {"thread": thread_id})
        if verdict.safety:
            return _safety_turn(con, memory, thread_id, thread, driver, verdict, now)
        try:
            return _run_turn(con, memory, model, thread_id, driver, verdict, now)
        except llm.ModelUnavailable:
            # Nothing was answered: forget the message so the driver's retry is not treated as a duplicate.
            con.execute("DELETE FROM chat_messages WHERE rowid=?", (msg_rowid,))
            raise
    finally:
        con.close()


def _safety_turn(con, memory, thread_id: str, thread, driver, verdict: gr.InputVerdict, now: str) -> dict:
    """Safety is a person's decision (brief: boundaries). Code escalates; the model is not asked."""
    state = memory.load(thread_id)
    transcript = state["facts"].setdefault("transcript", [])
    transcript.append({"t": clock.hhmm(now), "from": "driver", "text": verdict.text})
    shipment_id = thread["shipment_id"]
    if not shipment_id:
        row = con.execute("""SELECT shipment_id FROM shipments WHERE driver_id=?
                             AND current_status IN ('IN_TRANSIT','AT_GATE','WAITING','ASSIGNED')
                             ORDER BY original_eta_ts LIMIT 1""", (driver["driver_id"],)).fetchone()
        shipment_id = row["shipment_id"] if row else None
    result = escalation.escalate(con, driver_id=driver["driver_id"], shipment_id=shipment_id, thread_id=thread_id,
                                 reason_code="DRIVER_SAFETY", summary=f"Driver reported: {verdict.text[:300]}",
                                 driver_needs="Immediate call from a person; check driver safety first.",
                                 transcript=transcript, now=now)
    reply = gr.SAFETY_REPLY if result["ok"] else gr.SAFETY_REPLY_FAILED
    state["facts"]["escalated"] = result["ok"]
    state["messages"] += [{"role": "user", "content": [{"text": verdict.text}]},
                          {"role": "assistant", "content": [{"text": reply}]}]
    _store_message(con, thread_id, "AGENT", "agent", reply, clock.now(con))
    transcript.append({"t": clock.hhmm(now), "from": "agent", "text": reply})
    memory.save(thread_id, state)
    event = {"stage": "input", "code": "SAFETY_ESCALATION", "detail": result.get("status")}
    return {"thread_id": thread_id, "duplicate": False, "reply": reply,
            "tool_calls": [{"tool": "escalate_to_operations", "input": {"reason_code": "DRIVER_SAFETY"},
                            "ok": result["ok"], "result": result, "by": "guardrail"}],
            "guardrails": [event]}


def _run_turn(con, memory, model, thread_id: str, driver, verdict: gr.InputVerdict, now: str) -> dict:
    state = memory.load(thread_id)
    facts = state["facts"]
    facts["turn"] = turn = facts.get("turn", 0) + 1
    messages = state["messages"]
    messages.append({"role": "user", "content": [{"text": verdict.model_text}]})
    transcript = facts.setdefault("transcript", [])
    transcript.append({"t": clock.hhmm(now), "from": "driver", "text": verdict.text})
    events = [{"stage": "input", "code": f} for f in verdict.flags]

    ctx = tools.ToolContext(con, driver["driver_id"], thread_id, facts, transcript)
    system = SYSTEM.format(date="4 August 2026", now=clock.hhmm(now), driver_name=driver["driver_name"],
                           driver_id=driver["driver_id"], earlier=_earlier_messages(con, driver["driver_id"], now))
    model = model or llm.get_model()
    tool_config = tools.bedrock_tool_config()
    tool_log: list[dict] = []
    reply = ""
    for _ in range(MAX_TOOL_ROUNDS):
        resp = model.converse(system=system, messages=_trim(messages), tool_config=tool_config)
        out = resp["output"]["message"]
        messages.append(out)
        uses = [b["toolUse"] for b in out["content"] if "toolUse" in b]
        if resp.get("stopReason") != "tool_use" or not uses:
            reply = "\n".join(b["text"] for b in out["content"] if "text" in b).strip()
            break
        results = []
        for u in uses:
            args = u.get("input") or {}
            try:
                gr.gate_tool(u["name"], args, facts, turn=turn, driver_text=verdict.text)
                result, is_error = tools.call(ctx, u["name"], args)
            except gr.GateRefusal as g:
                result, is_error = {"ok": False, "code": g.code, "message": g.message}, True
                events.append({"stage": "tool", "code": g.code, "tool": u["name"]})
                cap.log(con, "guardrail", f"gate:{u['name']}", args.get("shipment_id"), g.code, args)
            gr.record_tool_result(u["name"], args, result, not is_error, facts, turn=turn)
            sid = args.get("shipment_id")
            if sid and not is_error:
                con.execute("UPDATE chat_threads SET shipment_id=COALESCE(shipment_id, ?) WHERE thread_id=?",
                            (sid, thread_id))
            tool_log.append({"tool": u["name"], "input": args, "ok": not is_error, "result": result})
            results.append({"toolResult": {"toolUseId": u["toolUseId"],
                                           "content": [{"json": json.loads(json.dumps(result, default=str))}],
                                           "status": "error" if is_error else "success"}})
        messages.append({"role": "user", "content": results})
    else:
        reply = ""
        events.append({"stage": "output", "code": "TOOL_LOOP_LIMIT"})

    reply, out_events = _validate_reply(con, model, system, tool_config, messages, reply, facts, tool_log)
    events += out_events
    if messages and messages[-1]["role"] == "assistant":
        messages[-1] = {"role": "assistant", "content": [{"text": reply}]}
    else:
        messages.append({"role": "assistant", "content": [{"text": reply}]})
    now = clock.now(con)
    _store_message(con, thread_id, "AGENT", "agent", reply, now)
    transcript.append({"t": clock.hhmm(now), "from": "agent", "text": reply})
    state["messages"] = messages
    memory.save(thread_id, state)
    return {"thread_id": thread_id, "duplicate": False, "reply": reply, "tool_calls": tool_log,
            "guardrails": events}


def _validate_reply(con, model, system, tool_config, messages, reply, facts, tool_log):
    """Check the reply against tool results; one repair attempt, then a template reply."""
    events = []
    problems = gr.check_reply(reply, facts, tool_log) if reply else ["EMPTY_REPLY"]
    if not problems:
        return reply, events
    events.append({"stage": "output", "code": "REPLY_BLOCKED", "problems": problems})
    cap.log(con, "guardrail", "check_reply", None, "BLOCKED", {"problems": problems, "reply": reply})
    attempt = list(messages)
    if attempt and attempt[-1]["role"] != "assistant":
        attempt.append({"role": "assistant", "content": [{"text": reply or "(no reply)"}]})
    attempt.append({"role": "user", "content": [{"text": gr.repair_prompt(problems)}]})
    try:
        resp = model.converse(system=system, messages=_trim(attempt), tool_config=tool_config)
        fixed = "\n".join(b["text"] for b in resp["output"]["message"]["content"] if "text" in b).strip()
    except (IndexError, KeyError):
        fixed = ""
    if fixed and resp.get("stopReason") != "tool_use" and not gr.check_reply(fixed, facts, tool_log):
        events.append({"stage": "output", "code": "REPLY_REPAIRED"})
        cap.log(con, "guardrail", "check_reply", None, "REPAIRED", {"reply": fixed})
        return fixed, events
    safe = gr.fallback_reply(tool_log)
    events.append({"stage": "output", "code": "FALLBACK_REPLY"})
    cap.log(con, "guardrail", "check_reply", None, "FALLBACK", {"reply": safe})
    return safe, events
