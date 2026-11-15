"""The tools the agent may call. Each one is a thin, validated wrapper around code.

The driver's identity comes from the session, never from the model: a tool cannot
read or change another driver's shipment.
"""
from __future__ import annotations

import re
import sqlite3
from typing import Any, Callable

from . import capacity as cap, clock, escalation

SIM_DATE = "2026-08-04"


class ToolContext:
    def __init__(self, con: sqlite3.Connection, driver_id: str, thread_id: str, facts: dict,
                 transcript: list[dict]):
        self.con, self.driver_id, self.thread_id = con, driver_id, thread_id
        self.facts, self.transcript = facts, transcript

    @property
    def now(self) -> str:
        return clock.now(self.con)


class ToolError(Exception):
    pass


def to_ts(value: str | None, now: str) -> str | None:
    """Accept 'HH:MM' (24h, today) or a full ISO timestamp."""
    if not value:
        return None
    value = value.strip()
    if re.fullmatch(r"\d{1,2}:\d{2}", value):
        h, m = value.split(":")
        return f"{now[:10]}T{int(h):02d}:{m}:00+05:30"
    try:
        return clock.fmt(clock.parse(value))
    except ValueError as e:
        raise ToolError(f"Could not read time '{value}'. Use HH:MM in 24-hour format.") from e


def _own(ctx: ToolContext, shipment_id: str) -> None:
    row = ctx.con.execute("SELECT driver_id FROM shipments WHERE shipment_id=?", (shipment_id,)).fetchone()
    if row is None or row["driver_id"] != ctx.driver_id:
        raise ToolError(f"{shipment_id} is not assigned to this driver.")


def _shipment_summary(ctx: ToolContext, shipment_id: str) -> dict:
    snap = cap.shipment_snapshot(ctx.con, shipment_id, ctx.now)
    s = snap["shipment"]
    cur = cap.check_current_appointment(ctx.con, shipment_id, ctx.now)
    hold = ctx.con.execute("""SELECT hold_id, expires_at, first_slot_id FROM slot_holds
                              WHERE shipment_id=? AND hold_status='ACTIVE' AND expires_at>?""",
                           (shipment_id, ctx.now)).fetchone()
    return {
        "shipment_id": shipment_id,
        "order_reference": s["order_reference"],
        "status": s["current_status"],
        "from": s["origin_city"],
        "to_facility": s["destination_facility_id"],
        "product": s["product_category"],
        "priority": s["priority_code"],
        "dock_need": s["required_dock_type"],
        "load_kg": s["load_weight_kg"],
        "unload_minutes": s["expected_unload_min"],
        "truck_position": snap["yard_state"],
        "gate_in": clock.hhmm(snap["gate_in_ts"]),
        "eta": {"time": clock.hhmm(snap["eta"]["eta"]), "source": snap["eta"]["source"],
                "confidence": snap["eta"]["confidence"]},
        "current_booking": cur,
        "active_hold": dict(hold) if hold else None,
        "driver_constraints_on_file": ctx.facts.get("constraints", {}).get(shipment_id),
    }


# --------------------------------------------------------------------------- tool functions

def get_my_shipments(ctx: ToolContext) -> dict:
    rows = ctx.con.execute("""SELECT shipment_id FROM shipments WHERE driver_id=?
                              ORDER BY original_eta_ts""", (ctx.driver_id,)).fetchall()
    out = []
    for r in rows:
        summ = _shipment_summary(ctx, r["shipment_id"])
        hint = None
        if summ["status"] == "CANCELLED":
            hint = "Cancelled: no slot can be offered."
        elif summ["status"] == "COMPLETED":
            hint = "Already delivered."
        elif summ["truck_position"] == "IN_YARD":
            hint = f"Truck already at the gate since {summ['gate_in']}; a 'running late' message is unlikely to be about this one."
        out.append({**summ, "hint": hint})
    active = [s for s in out if s["status"] not in ("CANCELLED", "COMPLETED")]
    return {"now": clock.hhmm(ctx.now), "shipments": out, "active_count": len(active),
            "note": "If more than one active shipment could match, confirm which one with the driver."}


def get_shipment_status(ctx: ToolContext, shipment_id: str) -> dict:
    _own(ctx, shipment_id)
    return _shipment_summary(ctx, shipment_id)


def record_eta(ctx: ToolContext, shipment_id: str, eta_time: str, confidence: str, driver_words: str,
               reason_code: str | None = None) -> dict:
    _own(ctx, shipment_id)
    if confidence not in ("HIGH", "MEDIUM", "LOW"):
        raise ToolError("confidence must be HIGH, MEDIUM or LOW")
    ts = to_ts(eta_time, ctx.now)
    return cap.record_eta(ctx.con, shipment_id, ctx.driver_id, ts, ctx.now, confidence=confidence,
                          reason_code=reason_code, note=f"Driver said: {driver_words}")


def set_driver_constraints(ctx: ToolContext, shipment_id: str, meaning: str, earliest_start: str | None = None,
                           latest_finish: str | None = None) -> dict:
    """Store only what the driver confirmed (audit B03)."""
    _own(ctx, shipment_id)
    c = ctx.facts.setdefault("constraints", {}).setdefault(shipment_id, {})
    if earliest_start:
        c["earliest_start"] = to_ts(earliest_start, ctx.now)
    if latest_finish:
        c["latest_finish"] = to_ts(latest_finish, ctx.now)
    c["meaning"] = meaning
    cap.log(ctx.con, "agent", "set_driver_constraints", shipment_id, "STORED", c)
    return {"ok": True, "constraints": c}


def find_slot_options(ctx: ToolContext, shipment_id: str, earliest_start: str | None = None,
                      latest_finish: str | None = None) -> dict:
    _own(ctx, shipment_id)
    c = ctx.facts.get("constraints", {}).get(shipment_id, {})
    res = cap.find_options(ctx.con, shipment_id, ctx.now,
                           earliest_acceptable=to_ts(earliest_start, ctx.now) or c.get("earliest_start"),
                           latest_finish=to_ts(latest_finish, ctx.now) or c.get("latest_finish"))
    if res.get("ok"):
        ctx.facts.setdefault("last_options", {})[shipment_id] = [
            {"n": i + 1, "slot_id": o["first_slot_id"], "dock": o["dock_code"], "start": o["start_hhmm"],
             "end": o["end_hhmm"]} for i, o in enumerate(res["options"])]
    return res


def hold_slot(ctx: ToolContext, shipment_id: str, slot_id: str) -> dict:
    _own(ctx, shipment_id)
    c = ctx.facts.get("constraints", {}).get(shipment_id, {})
    return cap.hold_slot(ctx.con, shipment_id, slot_id, ctx.now, thread_id=ctx.thread_id,
                         latest_finish=c.get("latest_finish"))


def confirm_booking(ctx: ToolContext, hold_id: str) -> dict:
    h = ctx.con.execute("SELECT shipment_id FROM slot_holds WHERE hold_id=?", (hold_id,)).fetchone()
    if h is None:
        raise ToolError("Unknown hold_id.")
    _own(ctx, h["shipment_id"])
    return cap.confirm_booking(ctx.con, hold_id, ctx.now)


def release_hold(ctx: ToolContext, hold_id: str) -> dict:
    h = ctx.con.execute("SELECT shipment_id FROM slot_holds WHERE hold_id=?", (hold_id,)).fetchone()
    if h is None:
        raise ToolError("Unknown hold_id.")
    _own(ctx, h["shipment_id"])
    return cap.release_hold(ctx.con, hold_id, ctx.now)


def escalate_to_operations(ctx: ToolContext, reason_code: str, summary: str, driver_needs: str,
                           shipment_id: str | None = None) -> dict:
    if shipment_id:
        _own(ctx, shipment_id)
    if reason_code not in escalation.REASONS:
        raise ToolError(f"reason_code must be one of {sorted(escalation.REASONS)}")
    return escalation.escalate(ctx.con, driver_id=ctx.driver_id, shipment_id=shipment_id, thread_id=ctx.thread_id,
                               reason_code=reason_code, summary=summary, driver_needs=driver_needs,
                               transcript=ctx.transcript, now=ctx.now)


# --------------------------------------------------------------------------- schemas

def _schema(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required}


SHIP = {"type": "string", "description": "Shipment ID, e.g. SHP1006"}
TIME = {"type": "string", "description": "Time today as HH:MM, 24-hour"}

TOOLS: dict[str, tuple[Callable[..., dict], str, dict]] = {
    "get_my_shipments": (get_my_shipments,
        "List this driver's shipments today with booking, ETA, truck position and hints. Call this first "
        "in any conversation.", _schema({}, [])),
    "get_shipment_status": (get_shipment_status,
        "Current booking, whether it is still achievable, ETA with source and confidence, and any active hold.",
        _schema({"shipment_id": SHIP}, ["shipment_id"])),
    "record_eta": (record_eta,
        "Store a new arrival time the driver has stated clearly. Use HIGH or MEDIUM only for a specific time; "
        "LOW for vague statements. Never invent a time the driver did not give.",
        _schema({"shipment_id": SHIP, "eta_time": TIME,
                 "confidence": {"type": "string", "enum": ["HIGH", "MEDIUM", "LOW"]},
                 "driver_words": {"type": "string", "description": "The driver's own words about the time"},
                 "reason_code": {"type": "string", "enum": ["TRAFFIC", "BREAKDOWN", "WEATHER", "LATE_DEPARTURE",
                                                             "LOADING_DELAY", "ROUTE_ISSUE", "OTHER"]}},
                ["shipment_id", "eta_time", "confidence", "driver_words"])),
    "set_driver_constraints": (set_driver_constraints,
        "Store a time limit the driver has confirmed, e.g. 'must leave by 13:30' confirmed as latest finish. "
        "Only after the driver confirms what the time means.",
        _schema({"shipment_id": SHIP, "earliest_start": TIME, "latest_finish": TIME,
                 "meaning": {"type": "string", "description": "What the driver confirmed, in plain words"}},
                ["shipment_id", "meaning"])),
    "find_slot_options": (find_slot_options,
        "Feasible dock slots for a shipment, computed by code from live state. Reserves nothing. Uses stored "
        "driver constraints automatically.",
        _schema({"shipment_id": SHIP, "earliest_start": TIME, "latest_finish": TIME}, ["shipment_id"])),
    "hold_slot": (hold_slot,
        "Hold the option the driver picked for 5 minutes so nobody else can take it. Pass the option's "
        "first_slot_id. If refused, fresh options are returned.",
        _schema({"shipment_id": SHIP, "slot_id": {"type": "string"}}, ["shipment_id", "slot_id"])),
    "confirm_booking": (confirm_booking,
        "Turn a hold into the confirmed booking. Only after the driver said yes to that specific held slot.",
        _schema({"hold_id": {"type": "string"}}, ["hold_id"])),
    "release_hold": (release_hold, "Release a hold the driver no longer wants.",
        _schema({"hold_id": {"type": "string"}}, ["hold_id"])),
    "escalate_to_operations": (escalate_to_operations,
        "Hand the case to a person with a complete record. Use when no feasible slot exists, a priority "
        "conflict needs a human decision, a slot needs manual approval, there is a safety issue or the data "
        "conflicts.",
        _schema({"reason_code": {"type": "string", "enum": sorted(escalation.REASONS)},
                 "summary": {"type": "string", "description": "What happened, in two sentences"},
                 "driver_needs": {"type": "string", "description": "What the driver needs and by when"},
                 "shipment_id": SHIP}, ["reason_code", "summary", "driver_needs"])),
}


def bedrock_tool_config() -> dict:
    return {"tools": [{"toolSpec": {"name": name, "description": desc, "inputSchema": {"json": schema}}}
                      for name, (_, desc, schema) in TOOLS.items()]}


def call(ctx: ToolContext, name: str, args: dict[str, Any]) -> tuple[dict, bool]:
    """Run a tool. Returns (result, is_error)."""
    if name not in TOOLS:
        return {"error": f"Unknown tool {name}"}, True
    fn = TOOLS[name][0]
    try:
        result = fn(ctx, **(args or {}))
        return result, False
    except ToolError as e:
        return {"error": str(e)}, True
    except cap.CapacityError as e:
        return e.as_dict(), True
    except TypeError as e:
        return {"error": f"Bad arguments for {name}: {e}"}, True
