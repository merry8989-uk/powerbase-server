from helpers import session_end_payload, session_start_payload


def test_session_start_captures_device(env):
    key = env.key("ingest")
    r = env.post("/v1/ingest/session/start", key, session_start_payload())
    assert r.status_code == 202
    assert r.json()["session_id"] == "sess-1"

    session = env.db.one("SELECT * FROM sessions WHERE id = ?", ("sess-1",))
    assert session["started_at"] == "2026-10-22T09:00:00.000000Z"
    assert session["ended_at"] is None
    assert session["country"] == "IN"

    device = env.db.one("SELECT * FROM devices WHERE id = ?", ("dev-1",))
    assert device["model"] == "Pixel 8"
    assert device["manufacturer"] == "Google"
    assert device["locale"] == "en-IN"


def test_session_start_is_idempotent(env):
    key = env.key("ingest")
    env.post("/v1/ingest/session/start", key, session_start_payload())
    env.post("/v1/ingest/session/start", key, session_start_payload())
    assert env.db.scalar("SELECT COUNT(*) FROM sessions") == 1


def test_session_end_updates_row(env):
    key = env.key("ingest")
    env.post("/v1/ingest/session/start", key, session_start_payload())
    r = env.post("/v1/ingest/session/end", key, session_end_payload())
    assert r.status_code == 200
    assert r.json()["updated"] is True

    session = env.db.one("SELECT * FROM sessions WHERE id = ?", ("sess-1",))
    assert session["duration_ms"] == 61000
    assert session["end_reason"] == "background"
    assert session["ended_at"] == "2026-10-22T09:01:01.000000Z"


def test_session_end_unknown_returns_404(env):
    key = env.key("ingest")
    r = env.post("/v1/ingest/session/end", key, session_end_payload(session_id="does-not-exist"))
    assert r.status_code == 404
