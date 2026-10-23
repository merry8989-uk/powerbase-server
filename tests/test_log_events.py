"""Observability ingestion — POST /v1/ingest/log-events.

These are the Phase 1 exit criteria from the roadmap: cross-project ingestion
fails, invalid and oversized events are refused, and secrets never reach
storage.
"""

from helpers import APP_ID, OTHER_APP_ID, events_payload, log_event, log_events_payload

INGEST = "/v1/ingest/log-events"


def test_a_batch_is_stored_with_every_field(env):
    token = env.key()
    response = env.post(INGEST, token, log_events_payload())

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["accepted"] == 1
    assert body["duplicate"] is False

    row = env.db.one("SELECT * FROM log_events WHERE app_id = ?", (APP_ID,))
    assert row["event_id"] == "evt-1"
    assert row["source"] == "server_sdk"
    assert row["severity"] == "ERROR"
    assert row["event_type"] == "http.request"
    assert row["service_name"] == "checkout-api"
    assert row["environment"] == "production"
    assert row["app_version"] == "1.4.0"
    assert row["release_id"] == "rel-2026-10-22"
    assert row["request_id"] == "req-1"
    assert row["trace_id"] == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert row["span_id"] == "00f067aa0ba902b7"
    assert row["route_template"] == "/orders/{id}"
    assert row["http_method"] == "POST"
    assert row["status_code"] == 500
    assert row["duration_ms"] == 812
    assert row["error_code"] == "UPSTREAM_TIMEOUT"
    assert row["error_fingerprint"] == "fp-checkout-1"
    assert row["sdk_version"] == "0.1.0"
    # Timestamps are normalised to the same fixed-width UTC format as everywhere
    # else, so a string comparison against them is a time comparison.
    assert row["event_time"] == "2026-10-22T09:00:00.000000Z"
    assert row["redaction_version"] == 1


def test_attributes_are_redacted_and_credential_keys_are_dropped(env):
    token = env.key()
    response = env.post(
        INGEST,
        token,
        log_events_payload(
            events=[
                log_event(
                    attributes={
                        "region": "ap-south-1",
                        "password": "hunter2",
                        "apiKey": "sk-live-abcdef",
                    }
                )
            ]
        ),
    )

    assert response.status_code == 202
    assert response.json()["dropped_attributes"] == 2

    row = env.db.one("SELECT message, attributes FROM log_events")
    # Values are scrubbed...
    assert "amit@example.com" not in (row["message"] or "")
    # ...and keys that name a credential are gone entirely, value and all.
    assert "hunter2" not in row["attributes"]
    assert "sk-live-abcdef" not in row["attributes"]
    assert "password" not in row["attributes"]
    assert "apiKey" not in row["attributes"]
    assert "ap-south-1" in row["attributes"]


def test_a_retried_batch_is_a_no_op(env):
    token = env.key()
    payload = log_events_payload()

    first = env.post(INGEST, token, payload)
    second = env.post(INGEST, token, payload)

    assert first.status_code == 202
    # 200 rather than 202: nothing new was accepted.
    assert second.status_code == 200
    assert second.json()["duplicate"] is True
    assert second.json()["accepted"] == 0
    assert env.db.scalar("SELECT COUNT(*) FROM log_events") == 1


def test_an_event_already_seen_in_another_batch_is_not_stored_twice(env):
    token = env.key()
    env.post(INGEST, token, log_events_payload(batch_id="b1", events=[log_event(event_id="e1")]))

    response = env.post(
        INGEST, token, log_events_payload(batch_id="b2", events=[log_event(event_id="e1")])
    )

    assert response.status_code == 202
    assert response.json()["accepted"] == 0
    assert response.json()["skipped_duplicates"] == 1
    assert env.db.scalar("SELECT COUNT(*) FROM log_events") == 1


def test_cross_project_ingestion_is_refused(env):
    token = env.key(app_id=APP_ID)
    response = env.post(INGEST, token, log_events_payload(app_id=OTHER_APP_ID))

    assert response.status_code == 403
    assert env.db.scalar("SELECT COUNT(*) FROM log_events") == 0


def test_a_read_key_cannot_ingest(env):
    read_token = env.key(scope="read")
    response = env.post(INGEST, read_token, log_events_payload())
    assert response.status_code == 403


def test_a_request_without_a_token_is_refused(env):
    response = env.client.post(INGEST, json=log_events_payload())
    assert response.status_code == 401


def test_an_oversized_batch_is_refused(make_env):
    env = make_env(max_batch_size=2)
    token = env.key()
    payload = log_events_payload(
        events=[log_event(event_id=f"e{i}") for i in range(3)]
    )
    response = env.post(INGEST, token, payload)

    assert response.status_code == 413
    assert env.db.scalar("SELECT COUNT(*) FROM log_events") == 0


def test_invalid_enum_and_range_values_are_rejected(env):
    token = env.key()

    cases = [
        {"severity": "LOUD"},
        {"source": "carrier_pigeon"},
        {"http_method": "YEET"},
        {"status_code": 999},
        {"status_code": 42},
        {"duration_ms": -1},
    ]
    for overrides in cases:
        response = env.post(INGEST, token, log_events_payload(events=[log_event(**overrides)]))
        assert response.status_code == 422, f"{overrides} was accepted"
    assert env.db.scalar("SELECT COUNT(*) FROM log_events") == 0


def test_an_empty_batch_is_rejected(env):
    token = env.key()
    assert env.post(INGEST, token, log_events_payload(events=[])).status_code == 422


def test_the_two_ingestion_models_do_not_accept_each_others_bodies(env):
    """`events` is analytics-shaped and `log_events` is observability-shaped.

    They are two models, not two versions of one, so neither path takes the
    other's payload. That is what lets the shipped Android SDK keep working
    while websites get a shape that actually fits them.
    """
    token = env.key()

    assert env.post("/v1/ingest/events", token, events_payload()).status_code == 202
    # An analytics event has no event_type, so the observability path refuses it.
    assert env.post(INGEST, token, events_payload()).status_code == 422
    # And an observability event has no device_id, so the analytics path does too.
    assert env.post("/v1/ingest/events", token, log_events_payload()).status_code == 422


def test_one_project_cannot_suppress_another_projects_batch(make_env):
    """The ledger is keyed by (project, batch id), not by batch id alone.

    With a global key, a key for one project could send another project's batch
    id first and have that project's genuine batch discarded as a duplicate.
    That is a cross-tenant interference, not just an annoyance.
    """
    from helpers import OTHER_APP_ID
    from powerbase.provision import provision_app

    env = make_env()
    provision_app(env.db, OTHER_APP_ID, "Other App")
    attacker = env.key(scope="ingest", app_id=APP_ID)
    victim = env.key(scope="ingest", app_id=OTHER_APP_ID)

    shared_batch_id = "the-same-id-for-both"

    # The attacker sends first, using the id the victim will use.
    assert env.post(
        INGEST, attacker, log_events_payload(batch_id=shared_batch_id, app_id=APP_ID)
    ).status_code == 202

    # The victim's batch must still land.
    response = env.post(
        INGEST, victim, log_events_payload(batch_id=shared_batch_id, app_id=OTHER_APP_ID)
    )
    assert response.status_code == 202
    assert response.json()["accepted"] == 1
    assert env.db.scalar(
        "SELECT COUNT(*) FROM log_events WHERE app_id = ?", (OTHER_APP_ID,)
    ) == 1


def test_a_retry_within_one_project_is_still_a_no_op(make_env):
    env = make_env()
    token = env.key(scope="ingest")
    payload = log_events_payload(batch_id="retry-me")

    assert env.post(INGEST, token, payload).status_code == 202
    second = env.post(INGEST, token, payload)
    assert second.status_code == 200
    assert second.json()["duplicate"] is True
    assert env.db.scalar("SELECT COUNT(*) FROM log_events") == 1
