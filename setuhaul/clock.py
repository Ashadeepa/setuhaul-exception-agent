"""Simulated clock. The snapshot is from 4 August 2026, so 'now' is simulated.

The clock runs at real speed from a base time and can be moved forward from the ops
panel. Tests freeze it. All timestamps are ISO-8601 strings with the +05:30 offset,
which keeps them comparable as text, matching the seed data.
"""
from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))


def parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def fmt(dt: datetime) -> str:
    return dt.astimezone(IST).replace(microsecond=0).isoformat()


def add_min(ts: str, minutes: float) -> str:
    return fmt(parse(ts) + timedelta(minutes=minutes))


def minutes_between(a: str, b: str) -> float:
    return (parse(b) - parse(a)).total_seconds() / 60


def now(con: sqlite3.Connection) -> str:
    row = con.execute("SELECT base_sim, base_real, frozen FROM sim_clock WHERE id=1").fetchone()
    if row["frozen"]:
        return row["base_sim"]
    return fmt(parse(row["base_sim"]) + timedelta(seconds=time.time() - row["base_real"]))


def set_now(con: sqlite3.Connection, ts: str, frozen: bool | None = None) -> None:
    if frozen is None:
        frozen = bool(con.execute("SELECT frozen FROM sim_clock WHERE id=1").fetchone()["frozen"])
    con.execute("UPDATE sim_clock SET base_sim=?, base_real=?, frozen=? WHERE id=1",
                (fmt(parse(ts)), time.time(), 1 if frozen else 0))


def advance(con: sqlite3.Connection, minutes: float) -> str:
    new = add_min(now(con), minutes)
    set_now(con, new)
    return new


def hhmm(ts: str | None) -> str | None:
    return None if ts is None else parse(ts).strftime("%H:%M")
