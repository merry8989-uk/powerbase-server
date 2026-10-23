import json

from helpers import events_payload


def test_ingest_events_creates_rows_and_context(env):
    key = env.key("ingest")
    r = env.post("/v1/ingest/events", key, events_payload(n=3))
    assert r.status_code == 202
    body = r.json()
    assert body["accepted"] == 3
    assert body["duplicate"] is False
    assert body["kind"] == "events"

    assert env.db.scalar("SELECT COUNT(*) FROM events") == 3
    # device and session stubs are auto-created
    assert env.db.scalar("SELECT COUNT(*) FROM devices") == 1
    assert env.db.scalar("SELECT COUNT(*) FROM sessions") == 1

    row = env.db.one("SELECT * FROM events LIMIT 1")
    assert row["app_id"] == "com.example.shop"
    assert row["name"] == "screen_view"
    assert row["ts"] == "2026-10-22T09:00:00.000000Z"
    assert row["received_at"].endswith("Z")


def test_event_properties_are_redacted(env):
    key = env.key("ingest")
    payload = events_payload(n=1)
    payload["events"][0]["properties"] = {"email": "amit@example.com", "count": 3}
    r = env.post("/v1/ingest/events", key, payload)
    assert r.status_code == 202

    stored = env.db.scalar("SELECT properties FROM events")
    assert "amit@example.com" not in stored
    assert "[REDACTED_EMAIL]" in stored
    # non-PII values survive untouched
    assert json.loads(stored)["count"] == 3


def test_device_last_seen_updated_on_repeat(env):
    key = env.key("ingest")
    env.post("/v1/ingest/events", key, events_payload(batch_id="b1", n=1))
    first = env.db.scalar("SELECT last_seen FROM devices")
    payload = events_payload(batch_id="b2", n=1)
    payload["events"][0]["ts"] = "2026-10-22T11:00:00Z"
    env.post("/v1/ingest/events", key, payload)
    assert env.db.scalar("SELECT COUNT(*) FROM devices") == 1
    assert env.db.scalar("SELECT last_seen FROM devices") >= first
