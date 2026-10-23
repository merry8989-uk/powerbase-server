from helpers import events_payload, logs_payload


def test_duplicate_event_batch_is_ignored(env):
    key = env.key("ingest")
    payload = events_payload(batch_id="dup-1", n=2)

    first = env.post("/v1/ingest/events", key, payload)
    assert first.status_code == 202
    assert first.json()["accepted"] == 2

    second = env.post("/v1/ingest/events", key, payload)
    assert second.status_code == 200
    assert second.json()["duplicate"] is True
    assert second.json()["accepted"] == 0

    assert env.db.scalar("SELECT COUNT(*) FROM events") == 2
    assert env.db.scalar("SELECT COUNT(*) FROM ingest_batches") == 1


def test_distinct_batches_both_written(env):
    key = env.key("ingest")
    env.post("/v1/ingest/events", key, events_payload(batch_id="a", n=1))
    env.post("/v1/ingest/events", key, events_payload(batch_id="b", n=1))
    assert env.db.scalar("SELECT COUNT(*) FROM events") == 2
    assert env.db.scalar("SELECT COUNT(*) FROM ingest_batches") == 2


def test_duplicate_across_kinds(env):
    key = env.key("ingest")
    env.post("/v1/ingest/events", key, events_payload(batch_id="shared", n=1))
    # Same id used for a logs batch is still treated as a duplicate — batch ids
    # are globally unique, which is what the SDK guarantees.
    r = env.post("/v1/ingest/logs", key, logs_payload(batch_id="shared"))
    assert r.status_code == 200
    assert r.json()["duplicate"] is True
    assert env.db.scalar("SELECT COUNT(*) FROM logs") == 0
