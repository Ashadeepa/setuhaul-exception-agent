"""Capacity layer: feasibility, holds and bookings.

This module decides. The language model never does: it can only call these functions
through the tools in tools.py, and every write re-checks feasibility inside a single
SQLite write transaction (BEGIN IMMEDIATE). The slot_claims primary key is the final
guard: one slot, one claim, whatever the code above it gets wrong.

State model for a slot, as seen by one driver (brief decision 6):
    SHOWN      an option in a list. Reserves nothing. May be gone a second later.
    HELD       a claim for HOLD_MINUTES while the driver says yes or no. Other drivers
               cannot take it. Expires on its own.
    CONFIRMED  an appointment in the system of record. The old appointment is cancelled
               in the same transaction (audit F02) and linked (audit F03).
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any

from . import clock

HOLD_MINUTES = 5
GATE_TO_DOCK_MIN = 15        # truck still on the road: gate processing after arrival
YARD_TO_DOCK_MIN = 5         # truck already in the yard
CHANGEOVER_MIN = 10          # dock turnaround after an overrunning truck leaves
ACTIVE_APPT = ("PENDING_CONFIRMATION", "CONFIRMED", "IN_PROGRESS")


class CapacityError(Exception):
    def __init__(self, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.code, self.message, self.extra = code, message, extra

    def as_dict(self) -> dict:
        return {"ok": False, "code": self.code, "message": self.message, **self.extra}


# --------------------------------------------------------------------------- helpers

def log(con: sqlite3.Connection, actor: str, action: str, shipment_id: str | None,
        outcome: str, detail: dict | None = None) -> None:
    import datetime as _dt
    con.execute("""INSERT INTO decision_log(logged_at, sim_time, actor, action, shipment_id, outcome, detail_json)
                   VALUES (?,?,?,?,?,?,?)""",
                (_dt.datetime.now().isoformat(timespec="seconds"), clock.now(con), actor, action,
                 shipment_id, outcome, json.dumps(detail or {}, default=str)))


def facility_rules(con: sqlite3.Connection, facility_id: str) -> dict:
    fac = con.execute("SELECT * FROM facilities WHERE facility_id=?", (facility_id,)).fetchone()
    rules = {r["rule_type"]: r["rule_value"] for r in con.execute(
        "SELECT rule_type, rule_value FROM facility_rules WHERE facility_id=? AND active_flag=1", (facility_id,))}
    return {
        "open_time": fac["open_time"],
        "close_time": fac["close_time"],
        "grace_min": int(rules.get("NO_SHOW_GRACE_MIN", fac["checkin_grace_min"])),
        "early_limit_min": int(rules.get("CHECKIN_EARLY_LIMIT_MIN", 60)),
        # Heavy threshold: Jaipur's rule; elsewhere the vehicle type decides.
        "heavy_kg": int(rules["HEAVY_DOCK_REQUIRED_KG"]) if "HEAVY_DOCK_REQUIRED_KG" in rules else None,
        # Audit H01: a start at exactly the limit is allowed; later needs manual approval.
        "last_start": rules.get("LAST_NEW_START_TIME", fac["close_time"]),
    }


def effective_eta(con: sqlite3.Connection, shipment_id: str, now: str) -> dict:
    """Latest ETA known at `now`, with its source and confidence.

    Ignores ETA rows created after `now` (audit A02). A revised latest_eta_ts with no
    update row behind it is labelled UNRECORDED, not ORIGINAL_PLAN/HIGH (audit B01).
    """
    s = con.execute("SELECT original_eta_ts, latest_eta_ts FROM shipments WHERE shipment_id=?",
                    (shipment_id,)).fetchone()
    row = con.execute("""SELECT declared_eta_ts, source_type, confidence_code, note, created_at
                         FROM eta_updates WHERE shipment_id=? AND created_at<=?
                         ORDER BY created_at DESC, eta_update_id DESC LIMIT 1""",
                      (shipment_id, now)).fetchone()
    if row:
        return {"eta": row["declared_eta_ts"], "source": row["source_type"],
                "confidence": row["confidence_code"], "note": row["note"], "updated_at": row["created_at"]}
    if s["latest_eta_ts"] and s["latest_eta_ts"] != s["original_eta_ts"]:
        return {"eta": s["latest_eta_ts"], "source": "UNRECORDED_REVISION", "confidence": "MEDIUM",
                "note": "Revised ETA with no update record", "updated_at": None}
    return {"eta": s["original_eta_ts"], "source": "ORIGINAL_PLAN", "confidence": "MEDIUM",
            "note": None, "updated_at": None}


def _sweep_expired_holds(con: sqlite3.Connection, now: str) -> None:
    expired = [r["hold_id"] for r in con.execute(
        "SELECT hold_id FROM slot_holds WHERE hold_status='ACTIVE' AND expires_at<=?", (now,))]
    for hid in expired:
        con.execute("UPDATE slot_holds SET hold_status='EXPIRED' WHERE hold_id=?", (hid,))
        con.execute("DELETE FROM slot_claims WHERE claim_kind='HOLD' AND claim_ref=?", (hid,))


def current_appointment(con: sqlite3.Connection, shipment_id: str):
    return con.execute(f"""SELECT a.*, sl.dock_id, sl.slot_start_ts, sl.slot_end_ts, d.dock_code
                           FROM appointments a
                           JOIN appointment_slots sl USING (slot_id)
                           JOIN docks d ON d.dock_id = sl.dock_id
                           WHERE a.shipment_id=? AND a.is_current=1
                             AND a.appointment_status IN {ACTIVE_APPT}""", (shipment_id,)).fetchone()


def appointment_slots(con: sqlite3.Connection, appointment_id: str) -> list[str]:
    first = con.execute("SELECT slot_id FROM appointments WHERE appointment_id=?", (appointment_id,)).fetchone()
    extra = [r["slot_id"] for r in con.execute(
        "SELECT slot_id FROM appointment_extra_slots WHERE appointment_id=?", (appointment_id,))]
    return [first["slot_id"], *extra] if first else extra


# --------------------------------------------------------------------------- snapshot

def shipment_snapshot(con: sqlite3.Connection, shipment_id: str, now: str) -> dict:
    s = con.execute("""SELECT s.*, v.vehicle_type_code, v.refrigeration_capable, v.registration_number
                       FROM shipments s JOIN vehicles v USING (vehicle_id)
                       WHERE s.shipment_id=?""", (shipment_id,)).fetchone()
    if s is None:
        raise CapacityError("UNKNOWN_SHIPMENT", f"No shipment {shipment_id}.")
    chk = con.execute("SELECT * FROM facility_checkins WHERE shipment_id=?", (shipment_id,)).fetchone()
    appt = current_appointment(con, shipment_id)
    eta = effective_eta(con, shipment_id, now)
    yard_state = "ON_ROAD"
    if chk and chk["gate_out_ts"]:
        yard_state = "DEPARTED"
    elif chk and chk["dock_in_ts"]:
        yard_state = "IN_DOCK"
    elif chk and chk["gate_in_ts"]:
        yard_state = "IN_YARD"
    return {
        "shipment": dict(s),
        "eta": eta,
        "yard_state": yard_state,
        "gate_in_ts": chk["gate_in_ts"] if chk else None,
        "appointment": dict(appt) if appt else None,
        "appointment_slot_ids": appointment_slots(con, appt["appointment_id"]) if appt else [],
    }


def earliest_dock_start(snap: dict, now: str) -> str:
    if snap["yard_state"] == "IN_YARD":
        return clock.add_min(now, YARD_TO_DOCK_MIN)
    base = max(now, snap["eta"]["eta"])
    return clock.add_min(base, GATE_TO_DOCK_MIN)


def dock_busy_until(con: sqlite3.Connection, dock_id: str, now: str, exclude_shipment: str | None = None) -> str | None:
    """A truck still unloading blocks the dock until it is likely to finish (audit E02)."""
    busy = None
    for r in con.execute("""SELECT c.shipment_id, c.unload_start_ts, c.dock_in_ts, s.expected_unload_min
                            FROM facility_checkins c JOIN shipments s USING (shipment_id)
                            WHERE c.actual_dock_id=? AND c.dock_in_ts IS NOT NULL AND c.unload_end_ts IS NULL""",
                         (dock_id,)):
        if r["shipment_id"] == exclude_shipment:
            continue
        planned_end = clock.add_min(r["unload_start_ts"] or r["dock_in_ts"], r["expected_unload_min"])
        until = clock.add_min(max(planned_end, now), CHANGEOVER_MIN)
        busy = until if busy is None else max(busy, until)
    return busy


def dock_reasons(dock, shipment, rules: dict) -> list[str]:
    """Why this dock cannot take this load. Empty list = compatible (audit D01, P3, P4)."""
    reasons = []
    needs_reefer = shipment["temperature_control_required"] == 1 or shipment["required_dock_type"] == "REEFER"
    needs_heavy = shipment["required_dock_type"] == "HEAVY" or (
        rules["heavy_kg"] is not None and shipment["load_weight_kg"] > rules["heavy_kg"])
    if dock["facility_id"] != shipment["destination_facility_id"]:
        reasons.append("WRONG_FACILITY")
    if dock["dock_status"] != "ACTIVE":
        reasons.append("DOCK_NOT_ACTIVE")
    if needs_reefer and not (dock["dock_type"] == "REEFER" and dock["supports_refrigerated"] == 1):
        reasons.append("NEEDS_REEFER_DOCK")
    elif needs_heavy and dock["dock_type"] != "HEAVY":
        reasons.append("NEEDS_HEAVY_DOCK")
    elif not needs_reefer and not needs_heavy and dock["dock_type"] != "STANDARD":
        reasons.append("SCARCE_DOCK_RESERVED")   # reefer and heavy docks kept for loads that need them
    if shipment["load_weight_kg"] > dock["max_vehicle_weight_kg"]:
        reasons.append("OVER_DOCK_WEIGHT_LIMIT")
    return reasons


# --------------------------------------------------------------------------- feasibility

@dataclass
class Option:
    first_slot_id: str
    slot_ids: list[str]
    dock_id: str
    dock_code: str
    dock_type: str
    start: str
    end: str
    needs_manual_approval: bool = False
    approval_reason: str | None = None

    def public(self) -> dict:
        d = asdict(self)
        d["start_hhmm"], d["end_hhmm"] = clock.hhmm(self.start), clock.hhmm(self.end)
        return d


@dataclass
class Evaluation:
    options: list[Option] = field(default_factory=list)
    approval_only: list[Option] = field(default_factory=list)
    rejected: dict[str, int] = field(default_factory=dict)

    def reject(self, reason: str) -> None:
        self.rejected[reason] = self.rejected.get(reason, 0) + 1


def _slot_free(con, slot_id: str, shipment_id: str, now: str) -> str | None:
    """None if free for this shipment, else the reason it is not."""
    c = con.execute("SELECT claim_kind, claim_ref, shipment_id FROM slot_claims WHERE slot_id=?",
                    (slot_id,)).fetchone()
    if c is None or c["shipment_id"] == shipment_id:
        return None
    if c["claim_kind"] == "HOLD":
        h = con.execute("SELECT expires_at, hold_status FROM slot_holds WHERE hold_id=?", (c["claim_ref"],)).fetchone()
        if h is None or h["hold_status"] != "ACTIVE" or h["expires_at"] <= now:
            return None
        return "HELD_BY_ANOTHER_DRIVER"
    return "BOOKED"


def _event_overlap(con, dock_id: str, start: str, end: str) -> str | None:
    """Breakdown or maintenance overlapping the window (audit E01, P2)."""
    r = con.execute("""SELECT event_type FROM dock_status_events
                       WHERE dock_id=? AND event_type IN ('BREAKDOWN','MAINTENANCE','MANUAL_BLOCK')
                         AND event_start_ts < ? AND (event_end_ts IS NULL OR event_end_ts > ?)""",
                    (dock_id, end, start)).fetchone()
    return f"DOCK_{r['event_type']}" if r else None


def _chain(slots_by_dock: list, start_index: int, minutes_needed: int) -> list | None:
    """Consecutive slots on one dock that together cover the unload time (audit D02)."""
    chain = [slots_by_dock[start_index]]
    covered = clock.minutes_between(chain[0]["slot_start_ts"], chain[0]["slot_end_ts"])
    i = start_index
    while covered < minutes_needed:
        i += 1
        if i >= len(slots_by_dock) or slots_by_dock[i]["slot_start_ts"] != chain[-1]["slot_end_ts"]:
            return None
        chain.append(slots_by_dock[i])
        covered += clock.minutes_between(slots_by_dock[i]["slot_start_ts"], slots_by_dock[i]["slot_end_ts"])
    return chain


def evaluate(con: sqlite3.Connection, shipment_id: str, now: str, *,
             earliest_acceptable: str | None = None, latest_finish: str | None = None,
             only_first_slot: str | None = None) -> tuple[dict, Evaluation]:
    """Every feasible slot chain for this shipment at `now`. Pure read."""
    snap = shipment_snapshot(con, shipment_id, now)
    s = snap["shipment"]
    rules = facility_rules(con, s["destination_facility_id"])
    ev = Evaluation()
    earliest = earliest_dock_start(snap, now)
    if earliest_acceptable:
        earliest = max(earliest, earliest_acceptable)

    docks = con.execute("SELECT * FROM docks WHERE facility_id=? ORDER BY dock_code",
                        (s["destination_facility_id"],)).fetchall()
    if only_first_slot:
        target = con.execute("SELECT dock_id FROM appointment_slots WHERE slot_id=?", (only_first_slot,)).fetchone()
        if target is None:
            ev.reject("UNKNOWN_SLOT")
            return snap, ev
        target_dock = target["dock_id"]
        if not any(d["dock_id"] == target_dock for d in docks):
            ev.reject("WRONG_FACILITY")
            return snap, ev
        docks = [d for d in docks if d["dock_id"] == target_dock]
    for dock in docks:
        reasons = dock_reasons(dock, s, rules)
        slots = con.execute("""SELECT * FROM appointment_slots WHERE dock_id=? ORDER BY slot_start_ts""",
                            (dock["dock_id"],)).fetchall()
        if reasons:
            for r in reasons:
                ev.reject(r)
            continue
        busy = dock_busy_until(con, dock["dock_id"], now, exclude_shipment=shipment_id)
        for i, sl in enumerate(slots):
            if only_first_slot and sl["slot_id"] != only_first_slot:
                continue
            if sl["slot_start_ts"] < earliest:
                ev.reject("BEFORE_TRUCK_CAN_REACH_DOCK")
                continue
            chain = _chain(slots, i, s["expected_unload_min"])
            if chain is None:
                ev.reject("NOT_ENOUGH_CONSECUTIVE_TIME")
                continue
            start, end = chain[0]["slot_start_ts"], chain[-1]["slot_end_ts"]
            why = None
            for c in chain:
                if c["slot_status"] != "OPEN":
                    why = f"SLOT_{c['slot_status']}"
                    break
                why = _slot_free(con, c["slot_id"], shipment_id, now)
                if why:
                    break
            why = why or _event_overlap(con, dock["dock_id"], start, end)
            if not why and busy and start < busy:
                why = "DOCK_STILL_OCCUPIED"
            if not why and latest_finish and end > latest_finish:
                why = "ENDS_AFTER_DRIVER_DEADLINE"
            if not why and clock.hhmm(end) > rules["close_time"] and clock.hhmm(end) != "00:00":
                why = "ENDS_AFTER_FACILITY_CLOSE"
            if why:
                ev.reject(why)
                continue
            opt = Option(chain[0]["slot_id"], [c["slot_id"] for c in chain], dock["dock_id"],
                         dock["dock_code"], dock["dock_type"], start, end)
            if clock.hhmm(start) > rules["last_start"]:
                opt.needs_manual_approval = True
                opt.approval_reason = f"Starts after the {rules['last_start']} last-start rule"
                ev.approval_only.append(opt)
            else:
                ev.options.append(opt)
    ev.options.sort(key=lambda o: (o.start, o.dock_code))
    ev.approval_only.sort(key=lambda o: (o.start, o.dock_code))
    return snap, ev


def preconditions(snap: dict) -> None:
    """Refuse to plan for shipments that cannot or should not be re-slotted."""
    s = snap["shipment"]
    if s["current_status"] == "CANCELLED":
        raise CapacityError("SHIPMENT_CANCELLED", "This shipment has been cancelled. Do not offer slots.")
    if s["current_status"] == "COMPLETED" or snap["yard_state"] in ("IN_DOCK", "DEPARTED"):
        raise CapacityError("ALREADY_AT_DOCK", "This truck is already in a dock or has finished.")
    if snap["eta"]["confidence"] == "LOW" and snap["yard_state"] == "ON_ROAD":
        raise CapacityError(
            "ETA_UNCONFIRMED",
            "The latest ETA is low-confidence. Ask the driver for a specific arrival time and record it with "
            "record_eta before searching for slots.",
            current_eta=snap["eta"])


# --------------------------------------------------------------------------- public API

def check_current_appointment(con: sqlite3.Connection, shipment_id: str, now: str) -> dict:
    """Is the booking this shipment holds still achievable? (audit D03)"""
    snap = shipment_snapshot(con, shipment_id, now)
    appt = snap["appointment"]
    if not appt:
        return {"has_appointment": False, "still_feasible": False, "reasons": ["NO_ACTIVE_APPOINTMENT"]}
    s = snap["shipment"]
    rules = facility_rules(con, s["destination_facility_id"])
    dock = con.execute("SELECT * FROM docks WHERE dock_id=?", (appt["dock_id"],)).fetchone()
    reasons = dock_reasons(dock, s, rules)
    slot_ids = snap["appointment_slot_ids"]
    first = con.execute("SELECT * FROM appointment_slots WHERE slot_id=?", (slot_ids[0],)).fetchone()
    last = con.execute("SELECT * FROM appointment_slots WHERE slot_id=?", (slot_ids[-1],)).fetchone()
    start, end = first["slot_start_ts"], last["slot_end_ts"]
    if clock.minutes_between(start, end) < s["expected_unload_min"]:
        reasons.append("SLOT_SHORTER_THAN_UNLOAD")
    deadline = clock.add_min(start, rules["grace_min"])
    if snap["yard_state"] == "ON_ROAD" and clock.add_min(snap["eta"]["eta"], GATE_TO_DOCK_MIN) > deadline:
        reasons.append("TRUCK_ARRIVES_AFTER_GRACE_PERIOD")
    if snap["yard_state"] in ("ON_ROAD", "IN_YARD") and now > deadline:
        reasons.append("SLOT_MISSED_NOT_DOCKED")
    ev = _event_overlap(con, appt["dock_id"], start, end)
    if ev:
        reasons.append(ev)
    busy = dock_busy_until(con, appt["dock_id"], now, exclude_shipment=shipment_id)
    if busy and busy > start and snap["yard_state"] in ("IN_YARD", "ON_ROAD") and end <= busy:
        reasons.append("DOCK_STILL_OCCUPIED")
    elif busy and busy > start:
        reasons.append("DOCK_OCCUPIED_AT_SLOT_START")
    return {"has_appointment": True, "appointment_id": appt["appointment_id"],
            "status": appt["appointment_status"], "dock": appt["dock_code"],
            "start": start, "end": end, "start_hhmm": clock.hhmm(start), "end_hhmm": clock.hhmm(end),
            "still_feasible": not reasons, "reasons": reasons}


def find_options(con: sqlite3.Connection, shipment_id: str, now: str, *, earliest_acceptable: str | None = None,
                 latest_finish: str | None = None, limit: int = 3, actor: str = "agent") -> dict:
    snap = shipment_snapshot(con, shipment_id, now)
    try:
        preconditions(snap)
    except CapacityError as e:
        log(con, actor, "find_options", shipment_id, e.code, {})
        return e.as_dict()
    _, ev = evaluate(con, shipment_id, now, earliest_acceptable=earliest_acceptable, latest_finish=latest_finish)
    s = snap["shipment"]
    result = {
        "ok": True,
        "shipment_id": shipment_id,
        "as_of": now,
        "eta_used": snap["eta"],
        "earliest_dock_start": max(earliest_dock_start(snap, now), earliest_acceptable or ""),
        "options": [o.public() for o in ev.options[:limit]],
        "more_options_available": max(0, len(ev.options) - limit),
        "manual_approval_only": [o.public() for o in ev.approval_only[:2]],
        "rejected_counts": ev.rejected,
        "note": "Options reserve nothing. Use hold_slot when the driver picks one.",
    }
    if not ev.options:
        result["status"] = "NO_FEASIBLE_SLOT"
        result["note"] = ("No slot can be offered automatically. Escalate to operations with escalate_to_operations."
                          + (" Some slots exist only with manual approval." if ev.approval_only else ""))
    else:
        result["status"] = "OPTIONS_FOUND"
        wait = clock.minutes_between(result["earliest_dock_start"], ev.options[0].start)
        if s["priority_code"] in ("CRITICAL", "HIGH") and wait > 60:
            result["priority_flag"] = (f"{s['priority_code']} load would wait {int(wait)} min. Code will not move "
                                       "other bookings; offer to escalate for an operations decision.")
    log(con, actor, "find_options", shipment_id, result["status"],
        {"offered": [o["first_slot_id"] for o in result["options"]], "rejected": ev.rejected})
    return result


def hold_slot(con: sqlite3.Connection, shipment_id: str, first_slot_id: str, now: str, *,
              thread_id: str | None = None, latest_finish: str | None = None, actor: str = "agent") -> dict:
    """Claim the slots for HOLD_MINUTES. Re-checks everything inside the write lock."""
    con.execute("BEGIN IMMEDIATE")
    try:
        _sweep_expired_holds(con, now)
        snap = shipment_snapshot(con, shipment_id, now)
        preconditions(snap)
        # One active hold per shipment: picking a new option releases the previous hold.
        for h in con.execute("SELECT hold_id FROM slot_holds WHERE shipment_id=? AND hold_status='ACTIVE'",
                             (shipment_id,)).fetchall():
            con.execute("UPDATE slot_holds SET hold_status='RELEASED' WHERE hold_id=?", (h["hold_id"],))
            con.execute("DELETE FROM slot_claims WHERE claim_kind='HOLD' AND claim_ref=?", (h["hold_id"],))
        _, ev = evaluate(con, shipment_id, now, latest_finish=latest_finish, only_first_slot=first_slot_id)
        if not ev.options:
            reason = next(iter(ev.rejected), "NOT_FEASIBLE")
            if ev.approval_only:
                reason = "NEEDS_MANUAL_APPROVAL"
            raise CapacityError("SLOT_NO_LONGER_AVAILABLE" if reason in ("BOOKED", "HELD_BY_ANOTHER_DRIVER")
                                else "SLOT_NOT_FEASIBLE",
                                f"That slot cannot be held now ({reason}).", reason=reason)
        opt = ev.options[0]
        hold_id = "H-" + uuid.uuid4().hex[:8].upper()
        expires = clock.add_min(now, HOLD_MINUTES)
        con.execute("""INSERT INTO slot_holds(hold_id, shipment_id, thread_id, first_slot_id, slot_ids,
                                              created_at, expires_at, hold_status)
                       VALUES (?,?,?,?,?,?,?, 'ACTIVE')""",
                    (hold_id, shipment_id, thread_id, opt.first_slot_id, ",".join(opt.slot_ids), now, expires))
        own = {r["slot_id"] for r in con.execute(
            "SELECT slot_id FROM slot_claims WHERE shipment_id=? AND claim_kind='APPOINTMENT'", (shipment_id,))}
        for sid in opt.slot_ids:
            if sid in own:
                continue  # the shipment's own current booking already claims it
            con.execute("""INSERT INTO slot_claims(slot_id, claim_kind, claim_ref, shipment_id, claimed_at)
                           VALUES (?, 'HOLD', ?, ?, ?)""", (sid, hold_id, shipment_id, now))
        log(con, actor, "hold_slot", shipment_id, "HELD", {"hold_id": hold_id, "slots": opt.slot_ids})
        con.execute("COMMIT")
        return {"ok": True, "hold_id": hold_id, "expires_at": expires, "expires_hhmm": clock.hhmm(expires),
                "option": opt.public(),
                "note": f"Held for {HOLD_MINUTES} minutes. Ask the driver to confirm, then call confirm_booking."}
    except sqlite3.IntegrityError:
        con.execute("ROLLBACK")
        err = CapacityError("SLOT_NO_LONGER_AVAILABLE", "Another driver claimed that slot first.",
                            reason="CLAIM_CONFLICT")
    except CapacityError as e:
        con.execute("ROLLBACK")
        err = e
    except Exception:
        con.execute("ROLLBACK")
        raise
    log(con, actor, "hold_slot", shipment_id, err.code, {"slot": first_slot_id, **err.extra})
    out = err.as_dict()
    if err.code in ("SLOT_NO_LONGER_AVAILABLE", "SLOT_NOT_FEASIBLE"):
        fresh = find_options(con, shipment_id, now, latest_finish=latest_finish, actor=actor)
        out["fresh_options"] = fresh.get("options", [])
        out["fresh_status"] = fresh.get("status", fresh.get("code"))
    return out


def confirm_booking(con: sqlite3.Connection, hold_id: str, now: str, *, actor: str = "agent",
                    booking_source: str = "DRIVER_CHAT") -> dict:
    """Turn a hold into the confirmed appointment and cancel the old one, atomically."""
    con.execute("BEGIN IMMEDIATE")
    try:
        _sweep_expired_holds(con, now)
        h = con.execute("SELECT * FROM slot_holds WHERE hold_id=?", (hold_id,)).fetchone()
        if h is None:
            raise CapacityError("UNKNOWN_HOLD", "No such hold.")
        if h["hold_status"] != "ACTIVE":
            raise CapacityError("HOLD_NOT_ACTIVE", f"The hold is {h['hold_status'].lower()}; it cannot be confirmed.",
                                hold_status=h["hold_status"], shipment_id=h["shipment_id"])
        shipment_id = h["shipment_id"]
        snap = shipment_snapshot(con, shipment_id, now)
        preconditions(snap)
        _, ev = evaluate(con, shipment_id, now, only_first_slot=h["first_slot_id"])
        if not ev.options:
            raise CapacityError("SLOT_NOT_FEASIBLE", "The held slot is no longer feasible.",
                                reason=next(iter(ev.rejected), "NOT_FEASIBLE"), shipment_id=shipment_id)
        slot_ids = h["slot_ids"].split(",")
        old = current_appointment(con, shipment_id)
        if old:
            con.execute("""UPDATE appointments SET appointment_status='CANCELLED', is_current=0,
                               cancelled_at=?, cancellation_reason='Rebooked after driver exception', updated_at=?
                           WHERE appointment_id=?""", (now, now, old["appointment_id"]))
            for sid in appointment_slots(con, old["appointment_id"]):
                if sid not in slot_ids:
                    con.execute("DELETE FROM slot_claims WHERE slot_id=? AND claim_ref=?",
                                (sid, old["appointment_id"]))
        n = con.execute("SELECT COUNT(*) FROM appointments WHERE appointment_id LIKE 'APT-C%'").fetchone()[0]
        appt_id = f"APT-C{n + 1:04d}"
        con.execute("""INSERT INTO appointments(appointment_id, shipment_id, slot_id, appointment_status,
                           booking_source, is_current, booked_at, confirmed_at, replaced_appointment_id, updated_at)
                       VALUES (?,?,?, 'CONFIRMED', ?, 1, ?, ?, ?, ?)""",
                    (appt_id, shipment_id, slot_ids[0], booking_source, h["created_at"], now,
                     old["appointment_id"] if old else None, now))
        for sid in slot_ids[1:]:
            con.execute("INSERT INTO appointment_extra_slots(appointment_id, slot_id) VALUES (?,?)", (appt_id, sid))
        for sid in slot_ids:
            con.execute("""INSERT INTO slot_claims(slot_id, claim_kind, claim_ref, shipment_id, claimed_at)
                           VALUES (?, 'APPOINTMENT', ?, ?, ?)
                           ON CONFLICT(slot_id) DO UPDATE SET claim_kind='APPOINTMENT', claim_ref=excluded.claim_ref,
                               claimed_at=excluded.claimed_at
                           WHERE slot_claims.shipment_id = excluded.shipment_id""",
                        (sid, appt_id, shipment_id, now))
            owner = con.execute("SELECT shipment_id FROM slot_claims WHERE slot_id=?", (sid,)).fetchone()
            if owner["shipment_id"] != shipment_id:
                raise sqlite3.IntegrityError(f"slot {sid} claimed by {owner['shipment_id']}")
        con.execute("UPDATE slot_holds SET hold_status='CONVERTED' WHERE hold_id=?", (hold_id,))
        fac = snap["shipment"]["destination_facility_id"]
        coord = con.execute("""SELECT email FROM facility_contacts WHERE facility_id=? AND active_flag=1
                               AND contact_role='WAREHOUSE_COORDINATOR' AND email IS NOT NULL AND email<>''""",
                            (fac,)).fetchone()
        opt = ev.options[0]
        con.execute("""INSERT INTO operational_messages(operational_message_id, shipment_id, appointment_id, channel,
                           sender_address, recipient_address, subject, message_body, sent_at, delivery_status)
                       VALUES (?,?,?, 'INTERNAL', 'agent@setuhaul.example', ?, ?, ?, ?, 'QUEUED')""",
                    ("OM-" + uuid.uuid4().hex[:8].upper(), shipment_id, appt_id,
                     coord["email"] if coord else "ops-queue@setuhaul.example",
                     f"Rebooked {shipment_id} to {opt.dock_code} {clock.hhmm(opt.start)}",
                     f"{shipment_id} moved from {old['appointment_id'] if old else 'no booking'} to {appt_id} "
                     f"({opt.dock_code} {clock.hhmm(opt.start)}-{clock.hhmm(opt.end)}).", now))
        log(con, actor, "confirm_booking", shipment_id, "CONFIRMED",
            {"appointment_id": appt_id, "replaced": old["appointment_id"] if old else None, "slots": slot_ids})
        con.execute("COMMIT")
        return {"ok": True, "appointment_id": appt_id, "replaced_appointment_id": old["appointment_id"] if old else None,
                "dock": opt.dock_code, "start": opt.start, "end": opt.end,
                "start_hhmm": clock.hhmm(opt.start), "end_hhmm": clock.hhmm(opt.end)}
    except sqlite3.IntegrityError as ie:
        con.execute("ROLLBACK")
        err = CapacityError("SLOT_NO_LONGER_AVAILABLE", "Another booking took the slot first.", detail=str(ie))
    except CapacityError as e:
        con.execute("ROLLBACK")
        err = e
    except Exception:
        con.execute("ROLLBACK")
        raise
    log(con, actor, "confirm_booking", err.extra.get("shipment_id"), err.code, {"hold_id": hold_id, **err.extra})
    return err.as_dict()


def release_hold(con: sqlite3.Connection, hold_id: str, now: str, *, actor: str = "agent") -> dict:
    con.execute("BEGIN IMMEDIATE")
    h = con.execute("SELECT * FROM slot_holds WHERE hold_id=?", (hold_id,)).fetchone()
    if h is None or h["hold_status"] != "ACTIVE":
        con.execute("ROLLBACK")
        return {"ok": True, "note": "Nothing to release."}
    con.execute("UPDATE slot_holds SET hold_status='RELEASED' WHERE hold_id=?", (hold_id,))
    con.execute("DELETE FROM slot_claims WHERE claim_kind='HOLD' AND claim_ref=?", (hold_id,))
    log(con, actor, "release_hold", h["shipment_id"], "RELEASED", {"hold_id": hold_id})
    con.execute("COMMIT")
    return {"ok": True, "released": hold_id}


def record_eta(con: sqlite3.Connection, shipment_id: str, driver_id: str, eta_ts: str, now: str, *,
               confidence: str, reason_code: str | None, note: str) -> dict:
    if eta_ts < now:
        return {"ok": False, "code": "ETA_IN_PAST", "message": "That time has already passed. Ask again."}
    n = con.execute("SELECT COUNT(*) FROM eta_updates").fetchone()[0]
    eid = f"ETA-C{n + 1:04d}"
    con.execute("""INSERT INTO eta_updates(eta_update_id, shipment_id, source_type, reported_by_driver_id,
                       declared_eta_ts, confidence_code, delay_reason_code, note, created_at)
                   VALUES (?,?, 'DRIVER_DECLARED', ?,?,?,?,?,?)""",
                (eid, shipment_id, driver_id, eta_ts, confidence, reason_code, note, now))
    con.execute("UPDATE shipments SET latest_eta_ts=?, updated_at=? WHERE shipment_id=?", (eta_ts, now, shipment_id))
    log(con, "agent", "record_eta", shipment_id, confidence, {"eta": eta_ts, "note": note})
    return {"ok": True, "eta_update_id": eid, "eta": eta_ts, "eta_hhmm": clock.hhmm(eta_ts), "confidence": confidence}


def board(con: sqlite3.Connection, facility_id: str, now: str) -> dict:
    """Facility board for the ops panel: every slot with its true state."""
    _sweep = con.execute("SELECT 1")  # read-only; expired holds are shown as free below
    rows = []
    for sl in con.execute("""SELECT sl.*, d.dock_code, d.dock_type FROM appointment_slots sl
                             JOIN docks d ON d.dock_id = sl.dock_id
                             WHERE sl.facility_id=? ORDER BY d.dock_code, sl.slot_start_ts""", (facility_id,)):
        state, who = "FREE", None
        if sl["slot_status"] != "OPEN":
            state = sl["slot_status"]
        else:
            c = con.execute("SELECT * FROM slot_claims WHERE slot_id=?", (sl["slot_id"],)).fetchone()
            if c:
                if c["claim_kind"] == "HOLD":
                    h = con.execute("SELECT * FROM slot_holds WHERE hold_id=?", (c["claim_ref"],)).fetchone()
                    if h and h["hold_status"] == "ACTIVE" and h["expires_at"] > now:
                        state, who = "HELD", c["shipment_id"]
                else:
                    state, who = "BOOKED", c["shipment_id"]
            if state == "FREE":
                ev = _event_overlap(con, sl["dock_id"], sl["slot_start_ts"], sl["slot_end_ts"])
                if ev:
                    state = "DOCK_EVENT"
                else:
                    busy = dock_busy_until(con, sl["dock_id"], now)
                    if busy and sl["slot_start_ts"] < busy and sl["slot_end_ts"] > now:
                        state = "OCCUPIED_OVERRUN"
        rows.append({"slot_id": sl["slot_id"], "dock": sl["dock_code"], "dock_type": sl["dock_type"],
                     "start": clock.hhmm(sl["slot_start_ts"]), "end": clock.hhmm(sl["slot_end_ts"]),
                     "past": sl["slot_end_ts"] <= now, "state": state, "shipment_id": who})
    return {"facility_id": facility_id, "as_of": now, "slots": rows}
