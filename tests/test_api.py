from fastapi.testclient import TestClient

from setuhaul.api import app


def test_api_board_race_and_missing_model(con, monkeypatch):
    monkeypatch.delenv("BEDROCK_MODEL_ID", raising=False)
    with TestClient(app) as c:
        assert c.get("/api/ops/state").json()["now_hhmm"] == "10:15"
        r = c.post("/api/ops/race", json={"slot_id": "SLOT-JAI-006"}).json()
        assert len(r["winners"]) == 1 and not r["double_booked"]
        tid = c.post("/api/threads", json={"driver_id": "DRV006"}).json()["thread_id"]
        resp = c.post(f"/api/threads/{tid}/messages", json={"text": "late"})
        assert resp.status_code == 503 and "BEDROCK_MODEL_ID" in resp.json()["detail"]
        assert c.get("/").status_code == 200
