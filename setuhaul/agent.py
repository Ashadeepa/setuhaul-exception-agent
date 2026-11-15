"""The conversation agent: reads messy driver messages, asks for missing facts, calls tools.

It decides nothing about capacity. Feasibility, holds and bookings come only from tools.
"""
from __future__ import annotations

import json
import re
import uuid

from . import capacity as cap, clock, db, llm, memory as mem, tools

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
        try:
            return _run_turn(con, memory, model, thread_id, driver, text, now)
        except llm.ModelUnavailable:
            # Nothing was answered: forget the message so the driver's retry is not treated as a duplicate.
            con.execute("DELETE FROM chat_messages WHERE rowid=?", (msg_rowid,))
            raise
    finally:
        con.close()


def _run_turn(con, memory, model, thread_id: str, driver, text: str, now: str) -> dict:
    state = memory.load(thread_id)
    messages = state["messages"]
    messages.append({"role": "user", "content": [{"text": text}]})
    transcript = state["facts"].setdefault("transcript", [])
    transcript.append({"t": clock.hhmm(now), "from": "driver", "text": text})

    ctx = tools.ToolContext(con, driver["driver_id"], thread_id, state["facts"], transcript)
    system = SYSTEM.format(date="4 August 2026", now=clock.hhmm(now), driver_name=driver["driver_name"],
                           driver_id=driver["driver_id"], earlier=_earlier_messages(con, driver["driver_id"], now))
    model = model or llm.get_model()
    tool_log = []
    reply = ""
    for _ in range(MAX_TOOL_ROUNDS):
        resp = model.converse(system=system, messages=_trim(messages), tool_config=tools.bedrock_tool_config())
        out = resp["output"]["message"]
        messages.append(out)
        uses = [b["toolUse"] for b in out["content"] if "toolUse" in b]
        if resp.get("stopReason") != "tool_use" or not uses:
            reply = "\n".join(b["text"] for b in out["content"] if "text" in b).strip()
            break
        results = []
        for u in uses:
            result, is_error = tools.call(ctx, u["name"], u.get("input") or {})
            sid = (u.get("input") or {}).get("shipment_id")
            if sid and not is_error:
                con.execute("UPDATE chat_threads SET shipment_id=COALESCE(shipment_id, ?) WHERE thread_id=?",
                            (sid, thread_id))
            tool_log.append({"tool": u["name"], "input": u.get("input"), "ok": not is_error,
                             "result": result})
            results.append({"toolResult": {"toolUseId": u["toolUseId"],
                                           "content": [{"json": json.loads(json.dumps(result, default=str))}],
                                           "status": "error" if is_error else "success"}})
        messages.append({"role": "user", "content": results})
    else:
        reply = "Sorry, I'm having trouble with that request. I'm passing it to operations."
    if not reply:
        reply = "Sorry, could you say that again?"
    now = clock.now(con)
    _store_message(con, thread_id, "AGENT", "agent", reply, now)
    transcript.append({"t": clock.hhmm(now), "from": "agent", "text": reply})
    state["messages"] = messages
    memory.save(thread_id, state)
    return {"thread_id": thread_id, "duplicate": False, "reply": reply, "tool_calls": tool_log}
