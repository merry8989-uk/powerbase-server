from helpers import logs_payload


def test_ingest_logs(env):
    key = env.key("ingest")
    r = env.post("/v1/ingest/logs", key, logs_payload())
    assert r.status_code == 202
    assert r.json()["accepted"] == 1
    assert env.db.scalar("SELECT COUNT(*) FROM logs") == 1

    row = env.db.one("SELECT * FROM logs")
    assert row["level"] == "ERROR"
    assert row["tag"] == "CheckoutRepo"


def test_log_message_is_redacted(env):
    key = env.key("ingest")
    env.post("/v1/ingest/logs", key, logs_payload())
    message = env.db.scalar("SELECT message FROM logs")
    assert "amit@example.com" not in message
    assert "[REDACTED_EMAIL]" in message


def test_full_text_search_finds_redacted_message(env):
    key = env.key("ingest")
    env.post("/v1/ingest/logs", key, logs_payload())
    hits = env.db.query("SELECT rowid FROM logs_fts WHERE logs_fts MATCH ?", ("payment",))
    assert len(hits) == 1


def test_fts_stays_in_sync_after_delete(env):
    key = env.key("ingest")
    env.post("/v1/ingest/logs", key, logs_payload())
    with env.db.write() as conn:
        conn.execute("DELETE FROM logs")
    hits = env.db.query("SELECT rowid FROM logs_fts WHERE logs_fts MATCH ?", ("payment",))
    assert hits == []
