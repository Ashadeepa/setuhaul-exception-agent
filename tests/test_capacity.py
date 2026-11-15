"""Feasibility and booking rules, each tied to a finding in the data audit."""
from setuhaul import capacity as cap, clock


def options(con, now, sid, **kw):
    r = cap.find_options(con, sid, now, limit=50, **kw)
    return r, [(o["dock_code"], o["start_hhmm"]) for o in r.get("options", [])]


def test_snapshot_starts_at_1015(now):
    assert now == "2026-08-04T10:15:00+05:30"


def test_future_no_show_is_undone(con):  # audit A02
    row = con.execute("SELECT appointment_status, is_current FROM appointments WHERE appointment_id='APT1018'").fetchone()
    assert (row["appointment_status"], row["is_current"]) == ("CONFIRMED", 1)


def test_unrecorded_eta_is_not_labelled_original_high(con, now):  # audit B01
    eta = cap.effective_eta(con, "SHP1009", now)
    assert eta["source"] == "UNRECORDED_REVISION" and eta["confidence"] != "HIGH"


def test_reefer_load_with_no_feasible_slot(con, now):  # SHP1015, ladder level 6
    r, opts = options(con, now, "SHP1015")
    assert r["status"] == "NO_FEASIBLE_SLOT" and opts == []


def test_reefer_load_only_gets_reefer_dock(con, now):
    _, opts = options(con, now, "SHP1010")
    assert opts and {d for d, _ in opts} == {"D5"}


def test_heavy_load_only_gets_heavy_dock(con, now):
    _, opts = options(con, now, "SHP1016")
    assert opts and {d for d, _ in opts} == {"D6"}
    assert opts[0] == ("D6", "12:30")  # 11:00-12:30 is SHP1011's


def test_weight_limit_excludes_light_docks(con, now):  # audit D01
    _, opts = options(con, now, "SHP1014")  # 21,000 kg
    assert opts and not {d for d, _ in opts} & {"D1", "D3"}


def test_long_unload_takes_two_consecutive_slots(con, now):  # audit D02
    r, _ = options(con, now, "SHP1014")  # 75 minutes
    first = r["options"][0]
    assert len(first["slot_ids"]) == 2
    assert clock.minutes_between(first["start"], first["end"]) >= 75


def test_breakdown_blocks_dock_until_repaired(con, now):  # audit E01
    _, opts = options(con, now, "SHP1006")
    d3 = [t for d, t in opts if d == "D3"]
    assert d3 and min(d3) >= "13:00"


def test_overrunning_truck_blocks_next_slot(con, now):  # audit E02
    board = {s["slot_id"]: s["state"] for s in cap.board(con, "FAC-JAI-01", now)["slots"]}
    assert board["SLOT-JAI-017"] == "OCCUPIED_OVERRUN"


def test_options_respect_truck_arrival(con, now):
    r, opts = options(con, now, "SHP1006")  # ETA 11:20 + 15 min gate
    assert all(t >= "11:35" for _, t in opts)


def test_driver_deadline_is_applied(con, now):
    _, opts = options(con, now, "SHP1012", latest_finish="2026-08-04T13:30:00+05:30")
    assert opts and all(t <= "12:30" for _, t in opts)


def test_last_start_rule_needs_approval(con, now):  # audit H01
    r, opts = options(con, now, "SHP1006", earliest_acceptable="2026-08-04T20:30:00+05:30")
    assert all(t <= "21:00" for _, t in opts)


def test_low_confidence_eta_must_be_clarified(con, now):  # audit B02
    r = cap.find_options(con, "SHP1013", now)
    assert r["code"] == "ETA_UNCONFIRMED"


def test_cancelled_shipment_gets_nothing(con, now):  # audit P5
    assert cap.find_options(con, "SHP1019", now)["code"] == "SHIPMENT_CANCELLED"
    assert cap.hold_slot(con, "SHP1019", "SLOT-JAI-006", now)["code"] == "SHIPMENT_CANCELLED"


def test_missed_current_booking_is_detected(con, now):  # audit D03
    assert not cap.check_current_appointment(con, "SHP1006", now)["still_feasible"]
    assert "SLOT_MISSED_NOT_DOCKED" in cap.check_current_appointment(con, "SHP1003", now)["reasons"]
    assert cap.check_current_appointment(con, "SHP1010", now)["still_feasible"]


def test_hold_confirm_rebooks_atomically_and_links_history(con, now):  # audit F02, F03
    h = cap.hold_slot(con, "SHP1006", "SLOT-JAI-047", now)
    assert h["ok"]
    c = cap.confirm_booking(con, h["hold_id"], now)
    assert c["ok"] and c["replaced_appointment_id"] == "APT1006"
    rows = {r["appointment_id"]: (r["appointment_status"], r["is_current"]) for r in con.execute(
        "SELECT * FROM appointments WHERE shipment_id='SHP1006'")}
    assert rows["APT1006"] == ("CANCELLED", 0) and rows[c["appointment_id"]] == ("CONFIRMED", 1)
    # the old slot is free again, the new one is claimed by the new booking
    assert con.execute("SELECT 1 FROM slot_claims WHERE slot_id='SLOT-JAI-045'").fetchone() is None
    claim = con.execute("SELECT * FROM slot_claims WHERE slot_id='SLOT-JAI-047'").fetchone()
    assert claim["claim_kind"] == "APPOINTMENT" and claim["claim_ref"] == c["appointment_id"]


def test_held_slot_is_invisible_to_others_and_refusal_offers_alternatives(con, now):
    assert cap.hold_slot(con, "SHP1006", "SLOT-JAI-047", now)["ok"]
    _, opts = options(con, now, "SHP1012")
    assert ("D4", "12:00") not in opts
    r = cap.hold_slot(con, "SHP1012", "SLOT-JAI-047", now)
    assert r["code"] == "SLOT_NO_LONGER_AVAILABLE" and r["fresh_options"]


def test_hold_expires(con, now):
    h = cap.hold_slot(con, "SHP1006", "SLOT-JAI-047", now)
    later = clock.add_min(now, cap.HOLD_MINUTES + 1)
    assert cap.confirm_booking(con, h["hold_id"], later)["code"] == "HOLD_NOT_ACTIVE"
    assert cap.hold_slot(con, "SHP1012", "SLOT-JAI-047", later)["ok"]


def test_new_hold_replaces_previous_hold_of_same_shipment(con, now):
    h1 = cap.hold_slot(con, "SHP1006", "SLOT-JAI-047", now)
    h2 = cap.hold_slot(con, "SHP1006", "SLOT-JAI-006", now)
    assert h2["ok"]
    assert cap.confirm_booking(con, h1["hold_id"], now)["code"] == "HOLD_NOT_ACTIVE"
    assert con.execute("SELECT 1 FROM slot_claims WHERE slot_id='SLOT-JAI-047'").fetchone() is None


def test_release_frees_slot(con, now):
    h = cap.hold_slot(con, "SHP1006", "SLOT-JAI-047", now)
    cap.release_hold(con, h["hold_id"], now)
    assert cap.hold_slot(con, "SHP1012", "SLOT-JAI-047", now)["ok"]


def test_cannot_hold_incompatible_slot(con, now):  # audit P2-P4, P7
    assert cap.hold_slot(con, "SHP1014", "SLOT-JAI-006", now)["reason"] == "OVER_DOCK_WEIGHT_LIMIT"
    assert cap.hold_slot(con, "SHP1012", "SLOT-JAI-032", now)["code"] == "SLOT_NOT_FEASIBLE"
    ggn = con.execute("SELECT slot_id FROM appointment_slots WHERE facility_id='FAC-GGN-01' LIMIT 1").fetchone()[0]
    assert cap.hold_slot(con, "SHP1012", ggn, now)["reason"] == "WRONG_FACILITY"
