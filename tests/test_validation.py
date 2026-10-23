from helpers import APP_ID, events_payload, logs_payload


def test_empty_events_list_rejected(env):
    key = env.key("ingest")
    payload = events_payload()
    payload["events"] = []
    r = env.post("/v1/ingest/events", key, payload)
    assert r.status_code == 422


def test_missing_required_field_rejected(env):
    key = env.key("ingest")
    payload = events_payload()
    del payload["events"][0]["name"]
    r = env.post("/v1/ingest/events", key, payload)
    assert r.status_code == 422


def test_bad_log_level_rejected(env):
    key = env.key("ingest")
    payload = logs_payload()
    payload["logs"][0]["level"] = "TRACE"
    r = env.post("/v1/ingest/logs", key, payload)
    assert r.status_code == 422


def test_app_id_must_match_key(env):
    key = env.key("ingest")
    payload = events_payload(app_id="com.other.app")
    r = env.post("/v1/ingest/events", key, payload)
    assert r.status_code == 403
    assert env.db.scalar("SELECT COUNT(*) FROM events") == 0


def test_batch_over_limit_rejected(make_env):
    env = make_env(max_batch_size=2)
    key = env.key("ingest")
    r = env.post("/v1/ingest/events", key, events_payload(n=3))
    assert r.status_code == 413


def test_batch_at_limit_accepted(make_env):
    env = make_env(max_batch_size=2)
    key = env.key("ingest")
    r = env.post("/v1/ingest/events", key, events_payload(n=2))
    assert r.status_code == 202


def test_unknown_fields_are_ignored(env):
    key = env.key("ingest")
    payload = events_payload()
    payload["events"][0]["future_field"] = "ignored"
    payload["unknown_top_level"] = 1
    r = env.post("/v1/ingest/events", key, payload)
    assert r.status_code == 202


def test_default_app_id_constant_matches(env):
    assert APP_ID == "com.example.shop"
