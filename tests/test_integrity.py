"""Integrity proof (deliverable 5): competing requests never double-book capacity.

Each round starts many threads, each with its own database connection, released at
the same instant by a barrier. Every thread tries to hold and confirm the same slot.
Pass condition: exactly one winner, exactly one claim, exactly one active booking.
"""
import threading

import pytest

from setuhaul import capacity as cap, clock, db
from setuhaul.api import run_race

CONTENDERS = ["SHP1003", "SHP1004", "SHP1006", "SHP1007", "SHP1009", "SHP1012", "SHP1018"]


def _active(con, slot):
    return con.execute("""SELECT COUNT(*) FROM appointments WHERE slot_id=?
                          AND appointment_status IN ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS')""",
                       (slot,)).fetchone()[0]


@pytest.mark.parametrize("round_", range(20))
def test_many_drivers_one_slot(con, round_):
    r = run_race("SLOT-JAI-006", CONTENDERS)
    assert len(r["winners"]) == 1, r
    assert r["claims_on_slot"] == 1 and r["active_appointments_on_slot"] == 1
    assert set(r["losers"].values()) <= {"SLOT_NO_LONGER_AVAILABLE"}
    assert not r["double_booked"]


def test_overlapping_multi_slot_requests(con):
    """SHP1014 needs D4 12:00+13:00 (75 min); SHP1006 wants D4 13:00 alone. Only one can have 13:00."""
    now = clock.now(con)
    results = {}
    barrier = threading.Barrier(2)

    def go(sid, slot):
        c = db.connect()
        barrier.wait()
        h = cap.hold_slot(c, sid, slot, now)
        results[sid] = cap.confirm_booking(c, h["hold_id"], now) if h.get("ok") else h
        c.close()

    ts = [threading.Thread(target=go, args=("SHP1014", "SLOT-JAI-047")),
          threading.Thread(target=go, args=("SHP1006", "SLOT-JAI-048"))]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sum(1 for r in results.values() if r.get("ok")) == 1
    owners = con.execute("SELECT shipment_id FROM slot_claims WHERE slot_id='SLOT-JAI-048'").fetchall()
    assert len(owners) == 1


def test_hold_by_one_driver_blocks_confirm_by_another(con):
    """The classic race from the brief: both read the slot as free, both say 'take it'."""
    now = clock.now(con)
    a = cap.find_options(con, "SHP1006", now)
    b = cap.find_options(con, "SHP1012", now)
    slot = a["options"][0]["first_slot_id"]
    assert slot == b["options"][0]["first_slot_id"]          # both saw the same free slot
    ha = cap.hold_slot(con, "SHP1006", slot, now)            # A says yes first
    hb = cap.hold_slot(con, "SHP1012", slot, now)            # B says yes a second later
    assert ha["ok"] and not hb["ok"] and hb["fresh_options"]  # B is told why and gets alternatives
    assert cap.confirm_booking(con, ha["hold_id"], now)["ok"]
    assert _active(con, slot) == 1


def test_database_backstop_rejects_second_claim(con):
    """Even if code above were wrong, the slot_claims primary key refuses a second claim."""
    import sqlite3
    con.execute("""INSERT INTO slot_claims VALUES ('SLOT-JAI-006','HOLD','X1','SHP1006','t')""")
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("""INSERT INTO slot_claims VALUES ('SLOT-JAI-006','HOLD','X2','SHP1012','t')""")
