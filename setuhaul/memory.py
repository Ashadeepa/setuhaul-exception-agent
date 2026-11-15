"""Conversation memory: what was said and what the driver told us.

Memory is context, never truth. Slots, holds and bookings live only in the database;
memory keeps the message history and the driver's stated constraints (which shipment,
earliest start, latest finish) so the agent does not ask twice.

Two stores share one interface:
  SqliteMemory  default; uses the agent_state table in the working database.
  UpstashMemory set UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN to use Upstash Redis
                over its REST API (no extra dependency). Keys expire after 24 hours.
"""
from __future__ import annotations

import json
import os
import sqlite3
import urllib.request
from typing import Protocol

EMPTY = {"messages": [], "facts": {}}


class Memory(Protocol):
    def load(self, thread_id: str) -> dict: ...
    def save(self, thread_id: str, state: dict) -> None: ...


class SqliteMemory:
    def __init__(self, con_factory):
        self._con = con_factory

    def load(self, thread_id: str) -> dict:
        con = self._con()
        try:
            row = con.execute("SELECT messages_json FROM agent_state WHERE thread_id=?", (thread_id,)).fetchone()
        finally:
            con.close()
        if not row:
            return json.loads(json.dumps(EMPTY))
        state = json.loads(row["messages_json"])
        state.setdefault("facts", {})
        return state

    def save(self, thread_id: str, state: dict) -> None:
        import datetime as dt
        con = self._con()
        try:
            con.execute("""INSERT INTO agent_state(thread_id, messages_json, updated_at) VALUES (?,?,?)
                           ON CONFLICT(thread_id) DO UPDATE SET messages_json=excluded.messages_json,
                           updated_at=excluded.updated_at""",
                        (thread_id, json.dumps(state, default=str), dt.datetime.now().isoformat()))
        finally:
            con.close()


class UpstashMemory:
    """Upstash Redis via its REST API: POST a Redis command as a JSON array."""

    TTL_SECONDS = 24 * 3600

    def __init__(self, url: str, token: str, prefix: str = "setuhaul:thread:"):
        self.url, self.token, self.prefix = url.rstrip("/"), token, prefix

    def _cmd(self, *args):
        req = urllib.request.Request(self.url, data=json.dumps(list(args)).encode(),
                                     headers={"Authorization": f"Bearer {self.token}",
                                              "Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read())
        if "error" in body:
            raise RuntimeError(f"Upstash error: {body['error']}")
        return body.get("result")

    def load(self, thread_id: str) -> dict:
        raw = self._cmd("GET", self.prefix + thread_id)
        if not raw:
            return json.loads(json.dumps(EMPTY))
        state = json.loads(raw)
        state.setdefault("facts", {})
        return state

    def save(self, thread_id: str, state: dict) -> None:
        self._cmd("SET", self.prefix + thread_id, json.dumps(state, default=str), "EX", self.TTL_SECONDS)


def make_memory(con_factory) -> Memory:
    url, token = os.environ.get("UPSTASH_REDIS_REST_URL"), os.environ.get("UPSTASH_REDIS_REST_TOKEN")
    if url and token:
        return UpstashMemory(url, token)
    return SqliteMemory(con_factory)


def describe(memory: Memory) -> str:
    return "upstash" if isinstance(memory, UpstashMemory) else "sqlite"
