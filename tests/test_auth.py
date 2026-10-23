from helpers import APP_ID, events_payload


def test_missing_authorization(env):
    r = env.client.post("/v1/ingest/events", json=events_payload())
    assert r.status_code == 401


def test_malformed_token(env):
    r = env.client.post(
        "/v1/ingest/events", json=events_payload(), headers=env.headers("not-a-token")
    )
    assert r.status_code == 401


def test_unknown_key(env):
    bogus = "pb_0000000000000000_" + "a" * 64
    r = env.post("/v1/ingest/events", bogus, events_payload())
    assert r.status_code == 401


def test_wrong_secret(env):
    token = env.key("ingest")
    prefix, key_id, _ = token.split("_")
    forged = f"{prefix}_{key_id}_{'b' * 64}"
    r = env.post("/v1/ingest/events", forged, events_payload())
    assert r.status_code == 401


def test_read_scope_cannot_ingest(env):
    read = env.key("read")
    r = env.post("/v1/ingest/events", read, events_payload())
    assert r.status_code == 403


def test_valid_key_is_accepted(env):
    key = env.key("ingest")
    r = env.post("/v1/ingest/events", key, events_payload())
    assert r.status_code == 202


def test_revoked_key_rejected(env):
    from powerbase.provision import revoke_key

    token = env.key("ingest")
    key_id = token.split("_")[1]
    assert revoke_key(env.db, key_id) is True
    r = env.post("/v1/ingest/events", token, events_payload())
    assert r.status_code == 401


def test_last_used_at_recorded(env):
    key = env.key("ingest")
    assert env.db.scalar("SELECT last_used_at FROM api_keys") is None
    env.post("/v1/ingest/events", key, events_payload())
    assert env.db.scalar("SELECT last_used_at FROM api_keys") is not None
