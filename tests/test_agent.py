"""Agent loop with a scripted model in place of Bedrock: tool scoping, dedupe, escalation, memory."""
import json

import pytest

from setuhaul import agent, db, escalation, llm, memory as mem, tools


def start(con, driver):
    return agent.start_thread(con, driver)


def test_full_rebooking_conversation(con):
    tid = start(con, "DRV006")
    m = llm.ScriptedModel([("tool", "get_my_shipments", {}),
                           ("tool", "find_slot_options", {"shipment_id": "SHP1006"}),
                           ("text", "1. D4 12:00")])
    r = agent.handle_message(tid, "Traffic after Shahpura. Reaching around 11:20. Any slot after 12?", model=m)
    assert [c["tool"] for c in r["tool_calls"]] == ["get_my_shipments", "find_slot_options"]
    slot = r["tool_calls"][1]["result"]["options"][0]["first_slot_id"]

    m = llm.ScriptedModel([("tool", "hold_slot", {"shipment_id": "SHP1006", "slot_id": slot}), ("text", "Held")])
    hold_id = agent.handle_message(tid, "first one", model=m)["tool_calls"][0]["result"]["hold_id"]

    m = llm.ScriptedModel([("tool", "confirm_booking", {"hold_id": hold_id}), ("text", "Booked")])
    r = agent.handle_message(tid, "yes book it", model=m)
    assert r["tool_calls"][0]["result"]["ok"]
    assert con.execute("SELECT shipment_id FROM chat_threads WHERE thread_id=?", (tid,)).fetchone()[0] == "SHP1006"


def test_driver_cannot_touch_another_drivers_shipment(con):
    tid = start(con, "DRV006")
    m = llm.ScriptedModel([("tool", "find_slot_options", {"shipment_id": "SHP1014"}), ("text", "no")])
    r = agent.handle_message(tid, "options for SHP1014", model=m)
    assert not r["tool_calls"][0]["ok"] and "not assigned" in r["tool_calls"][0]["result"]["error"]


def test_duplicate_message_is_answered_once(con):  # audit G03
    tid = start(con, "DRV006")
    agent.handle_message(tid, "Reaching 11:20, any slot?", model=llm.ScriptedModel([("text", "Checking")]))
    r = agent.handle_message(tid, "reaching 11:20 any slot", model=llm.ScriptedModel([]))
    assert r["duplicate"] and r["reply"] == "Checking"
    other = start(con, "DRV006")  # a retry that arrives in a new thread is caught too
    assert agent.handle_message(other, "Reaching 11:20, any slot?", model=llm.ScriptedModel([]))["duplicate"]


def test_model_failure_does_not_poison_retry(con):
    class Broken:
        def converse(self, **_):
            raise llm.ModelUnavailable("down")

    tid = start(con, "DRV006")
    with pytest.raises(llm.ModelUnavailable):
        agent.handle_message(tid, "late by 20", model=Broken())
    r = agent.handle_message(tid, "late by 20", model=llm.ScriptedModel([("text", "ok")]))
    assert not r["duplicate"]


def test_ambiguous_shipment_hints_from_backend(con):  # audit G05
    tid = start(con, "DRV004")
    r = agent.handle_message(tid, "I will be late by 45 minutes.",
                             model=llm.ScriptedModel([("tool", "get_my_shipments", {}), ("text", "Which one?")]))
    ships = {s["shipment_id"]: s for s in r["tool_calls"][0]["result"]["shipments"]}
    assert ships["SHP1004"]["truck_position"] == "IN_YARD" and ships["SHP1004"]["hint"]
    assert ships["SHP1020"]["hint"] is None


def test_constraints_are_remembered_and_applied(con):  # audit B03
    tid = start(con, "DRV012")
    m = llm.ScriptedModel([("tool", "set_driver_constraints",
                            {"shipment_id": "SHP1012", "latest_finish": "13:30",
                             "meaning": "Driver confirmed 13:30 is latest gate-out"}),
                           ("tool", "find_slot_options", {"shipment_id": "SHP1012"}),
                           ("text", "ok")])
    r = agent.handle_message(tid, "1:30 is when I must leave", model=m)
    assert all(o["end_hhmm"] <= "13:30" for o in r["tool_calls"][1]["result"]["options"])
    state = mem.SqliteMemory(db.connect).load(tid)
    assert state["facts"]["constraints"]["SHP1012"]["latest_finish"].endswith("13:30:00+05:30")


def test_no_slot_escalates_to_a_reachable_person(con):  # audit G01
    tid = start(con, "DRV015")
    m = llm.ScriptedModel([("tool", "find_slot_options", {"shipment_id": "SHP1015"}),
                           ("tool", "escalate_to_operations",
                            {"shipment_id": "SHP1015", "reason_code": "NO_FEASIBLE_SLOT",
                             "summary": "Reefer dock D5 in maintenance after ETA.",
                             "driver_needs": "Unload dairy tonight"}),
                           ("text", "Operations will call you")])
    r = agent.handle_message(tid, "ETA 6:30, can the reefer unload tonight?", model=m)
    assert r["tool_calls"][0]["result"]["status"] == "NO_FEASIBLE_SLOT"
    esc = r["tool_calls"][1]["result"]
    assert esc["status"] == "QUEUED_AWAITING_ACKNOWLEDGEMENT"
    row = con.execute("SELECT * FROM escalations").fetchone()
    record = json.loads(row["record_json"])
    assert row["assigned_contact_id"] == "CON003"                  # shift manager, has an email
    assert record["context"]["options_now"]["status"] == "NO_FEASIBLE_SLOT"
    assert record["transcript_tail"]


def test_escalation_skips_vacant_contact(con):  # audit G02
    night = "2026-08-04T21:30:00+05:30"  # Gurugram closes at 21:00, so the night desk is tried first
    escalation.escalate(con, driver_id="DRV006", shipment_id="SHP1021", thread_id=None,
                        reason_code="DRIVER_REQUEST", summary="s", driver_needs="n", transcript=[], now=night)
    row = con.execute("SELECT assigned_contact_id, record_json FROM escalations").fetchone()
    assert row["assigned_contact_id"] == "CON004"
    tried = json.loads(row["record_json"])["routing_tried"]
    assert any(t["contact_id"] == "CON005" and not t["usable"] for t in tried)


def test_tool_config_is_valid_for_bedrock():
    cfg = tools.bedrock_tool_config()
    names = [t["toolSpec"]["name"] for t in cfg["tools"]]
    assert len(names) == len(set(names)) == 9
    for t in cfg["tools"]:
        assert t["toolSpec"]["inputSchema"]["json"]["type"] == "object"


def test_upstash_memory_speaks_redis_rest(monkeypatch):
    calls = []

    class Resp:
        def __init__(self, body): self.body = body
        def read(self): return json.dumps(self.body).encode()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    store = {}

    def fake_urlopen(req, timeout):
        cmd = json.loads(req.data)
        calls.append((req.full_url, req.headers.get("Authorization"), cmd[0]))
        if cmd[0] == "SET":
            store[cmd[1]] = cmd[2]
            return Resp({"result": "OK"})
        return Resp({"result": store.get(cmd[1])})

    monkeypatch.setattr(mem.urllib.request, "urlopen", fake_urlopen)
    m = mem.UpstashMemory("https://example.upstash.io/", "tok")
    m.save("T1", {"messages": [{"role": "user"}], "facts": {"a": 1}})
    assert m.load("T1")["facts"] == {"a": 1}
    assert calls[0] == ("https://example.upstash.io", "Bearer tok", "SET")
