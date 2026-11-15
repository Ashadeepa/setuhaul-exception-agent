import pytest

from setuhaul import clock, db, llm


@pytest.fixture()
def con(tmp_path, monkeypatch):
    """Fresh working database at the 10:15 as-of time, clock frozen."""
    path = tmp_path / "test.db"
    monkeypatch.setenv("SETUHAUL_DB", str(path))
    monkeypatch.delenv("UPSTASH_REDIS_REST_URL", raising=False)
    monkeypatch.delenv("UPSTASH_REDIS_REST_TOKEN", raising=False)
    db.reset(path, frozen=True)
    c = db.connect(path)
    yield c
    c.close()
    llm.set_model(None)


@pytest.fixture()
def now(con):
    return clock.now(con)
