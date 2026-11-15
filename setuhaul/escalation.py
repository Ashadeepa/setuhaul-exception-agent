"""Hand-off to a human. An escalation is done only when a person acknowledges it.

Lessons from the audit:
  G01  OM004 failed to an unknown address while the thread said ESCALATED.
  G02  Gurugram's night contact is a vacant role with no email.
So: route only to known, active contacts with an address; fall back down a list; if
nobody can receive it, say so (status FAILED) instead of pretending it was sent.
"""
from __future__ import annotations

import json
import sqlite3
import uuid

from . import capacity as cap, clock

REASONS = {
    "NO_FEASIBLE_SLOT": "No compatible slot can be offered automatically",
    "PRIORITY_CONFLICT": "Priority load would wait; needs a decision to move other bookings",
    "NEEDS_MANUAL_APPROVAL": "Only slots outside operating rules remain",
    "DRIVER_SAFETY": "Driver reports a safety issue",
    "DATA_CONFLICT": "Backend data contradicts itself or the driver",
    "DRIVER_REQUEST": "Driver asked for a person",
}

# Who receives which kind of escalation, in fallback order.
ROUTING = {
    "DRIVER_SAFETY": ["SHIFT_MANAGER", "GATE_SUPERVISOR", "WAREHOUSE_COORDINATOR"],
    "DEFAULT": ["SHIFT_MANAGER", "WAREHOUSE_COORDINATOR", "NIGHT_SHIFT_COORDINATOR", "GATE_SUPERVISOR"],
}


def escalate(con: sqlite3.Connection, *, driver_id: str, shipment_id: str | None, thread_id: str | None,
             reason_code: str, summary: str, driver_needs: str, transcript: list[dict], now: str) -> dict:
    facility = None
    context: dict = {}
    if shipment_id:
        snap = cap.shipment_snapshot(con, shipment_id, now)
        facility = snap["shipment"]["destination_facility_id"]
        context = {
            "shipment": {k: snap["shipment"][k] for k in (
                "shipment_id", "order_reference", "priority_code", "required_dock_type", "load_weight_kg",
                "expected_unload_min", "current_status", "customer_name")},
            "eta": snap["eta"],
            "yard_state": snap["yard_state"],
            "current_appointment": cap.check_current_appointment(con, shipment_id, now),
            "options_now": cap.find_options(con, shipment_id, now, actor="escalation"),
        }
    roles = list(ROUTING.get(reason_code, ROUTING["DEFAULT"]))
    contact, tried = None, []
    if facility:
        hours = con.execute("SELECT open_time, close_time FROM facilities WHERE facility_id=?", (facility,)).fetchone()
        t = clock.hhmm(now)
        if t >= hours["close_time"] or t < hours["open_time"]:  # outside facility hours: night desk first
            roles = ["NIGHT_SHIFT_COORDINATOR"] + [r for r in roles if r != "NIGHT_SHIFT_COORDINATOR"]
        for role in roles:
            row = con.execute("""SELECT * FROM facility_contacts WHERE facility_id=? AND contact_role=?
                                 AND active_flag=1""", (facility, role)).fetchone()
            if row is None:
                continue
            usable = bool(row["email"]) and not row["contact_name"].lower().startswith("vacant")
            tried.append({"contact_id": row["contact_id"], "role": role, "usable": usable})
            if usable:
                contact = row
                break
    esc_id = "ESC-" + uuid.uuid4().hex[:6].upper()
    record = {
        "escalation_id": esc_id,
        "raised_at": now,
        "reason": REASONS.get(reason_code, reason_code),
        "summary": summary,
        "driver_needs": driver_needs,
        "driver_id": driver_id,
        "routing_tried": tried,
        "context": context,
        "transcript_tail": transcript[-8:],
    }
    status = "QUEUED" if contact else "FAILED"
    con.execute("""INSERT INTO escalations(escalation_id, shipment_id, driver_id, thread_id, reason_code, summary,
                       record_json, assigned_contact_id, assigned_to, delivery_status, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (esc_id, shipment_id, driver_id, thread_id, reason_code, summary, json.dumps(record, default=str),
                 contact["contact_id"] if contact else None,
                 f"{contact['contact_name']} ({contact['contact_role']})" if contact else None, status, now))
    if thread_id and contact:
        con.execute("UPDATE chat_threads SET thread_status='ESCALATED' WHERE thread_id=?", (thread_id,))
    cap.log(con, "agent", "escalate", shipment_id, status, {"escalation_id": esc_id, "to": tried})
    if contact:
        return {"ok": True, "escalation_id": esc_id, "status": "QUEUED_AWAITING_ACKNOWLEDGEMENT",
                "assigned_to_role": contact["contact_role"],
                "note": "A person has been notified but has not acknowledged yet. Do not tell the driver the "
                        "problem is solved; tell them operations will contact them."}
    return {"ok": False, "escalation_id": esc_id, "status": "FAILED_NO_REACHABLE_CONTACT",
            "note": "Nobody could be notified. Tell the driver honestly and ask them to call the facility gate."}


def acknowledge(con: sqlite3.Connection, escalation_id: str, note: str, now: str) -> None:
    con.execute("""UPDATE escalations SET delivery_status='ACKNOWLEDGED', acknowledged_at=?, resolution_note=?
                   WHERE escalation_id=?""", (now, note, escalation_id))
    cap.log(con, "operations", "acknowledge_escalation", None, "ACKNOWLEDGED", {"escalation_id": escalation_id})
