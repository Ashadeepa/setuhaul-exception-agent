"""Database setup: copy the seed snapshot, add the prototype's own tables, repair known data issues.

Every change made to the seed data here is traced to a finding in the data audit
(docs/data_audit.sql). Nothing else in the seed is altered.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEED_DB = ROOT / "data" / "seed.db"
DEFAULT_DB = ROOT / "var" / "setuhaul.db"

DEFAULT_START = "2026-08-04T10:15:00+05:30"  # as-of time for the morning scenarios (audit A01)

SCHEMA = """
-- Every slot is claimed by at most one hold or one booking. The PRIMARY KEY is the
-- guarantee: two concurrent writers cannot both insert the same slot_id.
CREATE TABLE IF NOT EXISTS slot_claims (
    slot_id      TEXT PRIMARY KEY REFERENCES appointment_slots(slot_id),
    claim_kind   TEXT NOT NULL CHECK (claim_kind IN ('HOLD','APPOINTMENT')),
    claim_ref    TEXT NOT NULL,
    shipment_id  TEXT NOT NULL REFERENCES shipments(shipment_id),
    claimed_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_slot_claims_ref ON slot_claims(claim_ref);

CREATE TABLE IF NOT EXISTS slot_holds (
    hold_id      TEXT PRIMARY KEY,
    shipment_id  TEXT NOT NULL REFERENCES shipments(shipment_id),
    thread_id    TEXT,
    first_slot_id TEXT NOT NULL,
    slot_ids     TEXT NOT NULL,            -- comma-separated, consecutive on one dock
    created_at   TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    hold_status  TEXT NOT NULL CHECK (hold_status IN ('ACTIVE','CONVERTED','RELEASED','EXPIRED'))
);

-- Extra slots used by an appointment whose unload is longer than one slot (audit D02).
CREATE TABLE IF NOT EXISTS appointment_extra_slots (
    appointment_id TEXT NOT NULL REFERENCES appointments(appointment_id),
    slot_id        TEXT NOT NULL REFERENCES appointment_slots(slot_id),
    PRIMARY KEY (appointment_id, slot_id)
);

CREATE TABLE IF NOT EXISTS decision_log (
    log_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    logged_at   TEXT NOT NULL,
    sim_time    TEXT NOT NULL,
    actor       TEXT NOT NULL,
    action      TEXT NOT NULL,
    shipment_id TEXT,
    outcome     TEXT NOT NULL,
    detail_json TEXT
);

CREATE TABLE IF NOT EXISTS escalations (
    escalation_id   TEXT PRIMARY KEY,
    shipment_id     TEXT,
    driver_id       TEXT NOT NULL,
    thread_id       TEXT,
    reason_code     TEXT NOT NULL,
    summary         TEXT NOT NULL,
    record_json     TEXT NOT NULL,
    assigned_contact_id TEXT,
    assigned_to     TEXT,
    delivery_status TEXT NOT NULL CHECK (delivery_status IN ('QUEUED','DELIVERED','FAILED','ACKNOWLEDGED')),
    created_at      TEXT NOT NULL,
    acknowledged_at TEXT,
    resolution_note TEXT
);

CREATE TABLE IF NOT EXISTS agent_state (
    thread_id     TEXT PRIMARY KEY,
    messages_json TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sim_clock (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    base_sim   TEXT NOT NULL,
    base_real  REAL NOT NULL,
    frozen     INTEGER NOT NULL DEFAULT 0
);
"""


def db_path() -> Path:
    return Path(os.environ.get("SETUHAUL_DB", DEFAULT_DB))


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    con = sqlite3.connect(str(path or db_path()), timeout=15, isolation_level=None,
                          check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA busy_timeout = 15000")
    return con


def reset(path: Path | str | None = None, start: str = DEFAULT_START, frozen: bool = False) -> Path:
    """Rebuild the working database from the seed snapshot."""
    target = Path(path or db_path())
    target.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(target) + suffix)
        if p.exists():
            p.unlink()
    shutil.copy(SEED_DB, target)
    con = connect(target)
    con.execute("PRAGMA journal_mode = WAL")
    con.executescript(SCHEMA)
    _repair_seed(con, start)
    _seed_claims(con)
    import time
    con.execute("INSERT INTO sim_clock(id, base_sim, base_real, frozen) VALUES (1, ?, ?, ?)",
                (start, time.time(), 1 if frozen else 0))
    con.close()
    return target


def _repair_seed(con: sqlite3.Connection, start: str) -> None:
    """Undo rows that describe events after the as-of time (audit A02).

    APT1018 was marked NO_SHOW at 12:35. At a 10:15 as-of time that has not happened,
    so the booking is restored to CONFIRMED and current. If the simulation starts after
    12:35 the seed row is left as it is.
    """
    if start < "2026-08-04T12:35:00+05:30":
        con.execute("""UPDATE appointments
                       SET appointment_status='CONFIRMED', is_current=1,
                           updated_at='2026-08-01T12:35:00+05:30'
                       WHERE appointment_id='APT1018' AND appointment_status='NO_SHOW'""")
        con.execute("""UPDATE shipments SET updated_at='2026-08-04T09:00:00+05:30'
                       WHERE shipment_id='SHP1018'""")


def _seed_claims(con: sqlite3.Connection) -> None:
    """Every active booking in the seed claims its slot."""
    con.execute("""INSERT OR IGNORE INTO slot_claims(slot_id, claim_kind, claim_ref, shipment_id, claimed_at)
                   SELECT slot_id, 'APPOINTMENT', appointment_id, shipment_id, booked_at
                   FROM appointments
                   WHERE appointment_status IN ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS')""")
