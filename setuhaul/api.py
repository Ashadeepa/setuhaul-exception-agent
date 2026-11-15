"""Web API and the one-page chat + ops panel.

Run:  uvicorn setuhaul.api:app --reload
"""
from __future__ import annotations

import json
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import agent, capacity as cap, clock, db, escalation, llm, memory as mem
from .config import load_env

load_env()

STATIC = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(_app):
    if not db.db_path().exists() or os.environ.get("SETUHAUL_RESET_ON_START") == "1":
        db.reset()
    yield


app = FastAPI(title="SetuHaul exception agent", lifespan=lifespan)


def _con():
    return db.connect()


class NewThread(BaseModel):
    driver_id: str


class DriverMessage(BaseModel):
    text: str
    external_id: str | None = None


class ClockChange(BaseModel):
    advance_min: float | None = None
    set_to: str | None = None


class Ack(BaseModel):
    note: str = "Acknowledged"


class Race(BaseModel):
    slot_id: str = "SLOT-JAI-006"
    max_drivers: int = 10


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/drivers")
def drivers():
    con = _con()
    try:
        now = clock.now(con)
        out = []
        for d in con.execute("SELECT driver_id, driver_name FROM drivers ORDER BY driver_id"):
            ships = con.execute("""SELECT shipment_id, current_status, destination_facility_id FROM shipments
                                   WHERE driver_id=? ORDER BY original_eta_ts""", (d["driver_id"],)).fetchall()
            out.append({"driver_id": d["driver_id"], "name": d["driver_name"],
                        "shipments": [dict(s) for s in ships]})
        return {"now": now, "drivers": out}
    finally:
        con.close()


@app.post("/api/threads")
def new_thread(body: NewThread):
    con = _con()
    try:
        return {"thread_id": agent.start_thread(con, body.driver_id)}
    except ValueError as e:
        raise HTTPException(404, str(e))
    finally:
        con.close()


@app.get("/api/threads/{thread_id}")
def get_thread(thread_id: str):
    con = _con()
    try:
        msgs = con.execute("""SELECT sender_type, message_text, message_ts, is_duplicate FROM chat_messages
                              WHERE thread_id=? ORDER BY message_ts, rowid""", (thread_id,)).fetchall()
        return {"thread_id": thread_id, "messages": [dict(m) for m in msgs]}
    finally:
        con.close()


@app.post("/api/threads/{thread_id}/messages")
def post_message(thread_id: str, body: DriverMessage):
    try:
        return agent.handle_message(thread_id, body.text, external_id=body.external_id)
    except llm.ModelUnavailable as e:
        raise HTTPException(503, str(e))
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.get("/api/ops/board")
def ops_board(facility_id: str = "FAC-JAI-01"):
    con = _con()
    try:
        return cap.board(con, facility_id, clock.now(con))
    finally:
        con.close()


@app.get("/api/ops/state")
def ops_state():
    con = _con()
    try:
        now = clock.now(con)
        holds = con.execute("""SELECT hold_id, shipment_id, slot_ids, expires_at FROM slot_holds
                               WHERE hold_status='ACTIVE' AND expires_at>? ORDER BY expires_at""", (now,)).fetchall()
        escs = con.execute("""SELECT escalation_id, shipment_id, driver_id, reason_code, summary, assigned_to,
                                     delivery_status, created_at, record_json FROM escalations
                              ORDER BY created_at DESC""").fetchall()
        log = con.execute("""SELECT sim_time, actor, action, shipment_id, outcome, detail_json FROM decision_log
                             ORDER BY log_id DESC LIMIT 40""").fetchall()
        return {
            "now": now,
            "now_hhmm": clock.hhmm(now),
            "model": os.environ.get("BEDROCK_MODEL_ID") or None,
            "memory": mem.describe(mem.make_memory(db.connect)),
            "holds": [{**dict(h), "expires_hhmm": clock.hhmm(h["expires_at"])} for h in holds],
            "escalations": [{**{k: e[k] for k in e.keys() if k != "record_json"},
                             "record": json.loads(e["record_json"])} for e in escs],
            "decision_log": [{**dict(r), "detail": json.loads(r["detail_json"] or "{}")} for r in log],
        }
    finally:
        con.close()


@app.post("/api/ops/clock")
def ops_clock(body: ClockChange):
    con = _con()
    try:
        if body.set_to:
            clock.set_now(con, body.set_to)
        elif body.advance_min:
            clock.advance(con, body.advance_min)
        return {"now": clock.now(con)}
    finally:
        con.close()


@app.post("/api/ops/reset")
def ops_reset():
    db.reset()
    return {"ok": True}


@app.post("/api/ops/escalations/{escalation_id}/ack")
def ops_ack(escalation_id: str, body: Ack):
    con = _con()
    try:
        escalation.acknowledge(con, escalation_id, body.note, clock.now(con))
        return {"ok": True}
    finally:
        con.close()


@app.post("/api/ops/race")
def ops_race(body: Race):
    """Integrity demo: every shipment that could use this slot tries to hold it at the same instant."""
    con = _con()
    try:
        now = clock.now(con)
        sl = con.execute("SELECT facility_id FROM appointment_slots WHERE slot_id=?", (body.slot_id,)).fetchone()
        if sl is None:
            raise HTTPException(404, "Unknown slot")
        candidates = []
        for s in con.execute("""SELECT shipment_id FROM shipments WHERE destination_facility_id=?
                                ORDER BY shipment_id""", (sl["facility_id"],)):
            try:
                snap, ev = cap.evaluate(con, s["shipment_id"], now, only_first_slot=body.slot_id)
                cap.preconditions(snap)
            except cap.CapacityError:
                continue
            if ev.options:
                candidates.append(s["shipment_id"])
        candidates = candidates[: body.max_drivers]
    finally:
        con.close()
    return run_race(body.slot_id, candidates)


def run_race(slot_id: str, shipment_ids: list[str], *, confirm: bool = True) -> dict:
    barrier = threading.Barrier(len(shipment_ids)) if shipment_ids else None
    results: dict[str, dict] = {}

    def worker(sid: str):
        con = db.connect()
        try:
            barrier.wait()
            now = clock.now(con)
            r = cap.hold_slot(con, sid, slot_id, now, actor=f"race:{sid}")
            if r.get("ok") and confirm:
                r = {"hold": r, "confirm": cap.confirm_booking(con, r["hold_id"], now, actor=f"race:{sid}")}
            results[sid] = r
        finally:
            con.close()

    threads = [threading.Thread(target=worker, args=(s,)) for s in shipment_ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    winners = [s for s, r in results.items() if r.get("ok") or (r.get("confirm") or {}).get("ok")]
    con = db.connect()
    try:
        claims = con.execute("SELECT COUNT(*) FROM slot_claims WHERE slot_id=?", (slot_id,)).fetchone()[0]
        active = con.execute("""SELECT COUNT(*) FROM appointments WHERE slot_id=?
                                AND appointment_status IN ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS')""",
                             (slot_id,)).fetchone()[0]
    finally:
        con.close()
    return {"slot_id": slot_id, "contenders": shipment_ids, "winners": winners,
            "losers": {s: (r.get("code") or (r.get("confirm") or {}).get("code")) for s, r in results.items()
                       if s not in winners},
            "claims_on_slot": claims, "active_appointments_on_slot": active,
            "double_booked": active > 1 or claims > 1}
