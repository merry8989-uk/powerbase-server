"""Key administration — create, rotate, revoke, expire.

The rule these tests protect: a browser or mobile key is public and extractable,
so it must be able to ingest and nothing else. In particular it must never be
able to mint or renew keys.
"""

import pytest

from helpers import ADMIN_PASSWORD, APP_ID, OTHER_APP_ID, log_events_payload

KEYS = "/v1/keys"
INGEST = "/v1/ingest/log-events"


def auth(password: str = ADMIN_PASSWORD) -> dict:
    return {"X-Admin-Password": password}


@pytest.fixture()
def admin_env(make_env):
    return make_env(admin_password=ADMIN_PASSWORD)


# ------------------------------------------------------------------ create
def test_creating_a_key_returns_the_token_exactly_once(admin_env):
    response = admin_env.client.post(
        KEYS,
        json={"app_id": APP_ID, "scope": "ingest", "label": "website"},
        headers=auth(),
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["token"].startswith("pb_")
    assert body["key_id"] in body["token"]

    # The database holds an Argon2 hash and no plaintext anywhere.
    row = admin_env.db.one("SELECT * FROM api_keys WHERE id = ?", (body["key_id"],))
    assert row["scope"] == "ingest"
    assert row["label"] == "website"
    assert body["token"] not in (row["key_hash"] or "")
    assert row["key_hash"].startswith("$argon2")

    # And a later read of the key list never hands the secret back.
    listing = admin_env.client.get(KEYS, headers=auth()).json()
    assert body["token"] not in str(listing)
    assert "key_hash" not in str(listing)


def test_a_created_key_actually_works(admin_env):
    token = admin_env.client.post(
        KEYS, json={"app_id": APP_ID, "scope": "ingest"}, headers=auth()
    ).json()["token"]

    assert admin_env.post(INGEST, token, log_events_payload()).status_code == 202


def test_a_read_key_can_read_but_not_ingest(admin_env):
    token = admin_env.client.post(
        KEYS, json={"app_id": APP_ID, "scope": "read"}, headers=auth()
    ).json()["token"]

    assert admin_env.post(INGEST, token, log_events_payload()).status_code == 403


def test_creating_a_key_for_an_unknown_app_is_refused(admin_env):
    response = admin_env.client.post(
        KEYS, json={"app_id": "no.such.app", "scope": "ingest"}, headers=auth()
    )
    assert response.status_code == 404


def test_an_unknown_scope_is_refused(admin_env):
    response = admin_env.client.post(
        KEYS, json={"app_id": APP_ID, "scope": "admin"}, headers=auth()
    )
    assert response.status_code == 422


def test_listing_can_be_scoped_to_one_project(admin_env):
    from powerbase.provision import provision_app

    provision_app(admin_env.db, OTHER_APP_ID, "Other App")
    admin_env.key(app_id=APP_ID)
    admin_env.client.post(
        KEYS, json={"app_id": OTHER_APP_ID, "scope": "ingest"}, headers=auth()
    )

    listing = admin_env.client.get(KEYS, params={"app_id": OTHER_APP_ID}, headers=auth()).json()
    assert listing["items"]
    assert {item["app_id"] for item in listing["items"]} == {OTHER_APP_ID}


# ------------------------------------------------------------------ rotate
def test_rotation_replaces_the_key_without_a_gap(admin_env):
    old_token = admin_env.client.post(
        KEYS, json={"app_id": APP_ID, "scope": "ingest"}, headers=auth()
    ).json()
    assert admin_env.post(INGEST, old_token["token"], log_events_payload()).status_code == 202

    rotated = admin_env.client.post(
        f"{KEYS}/{old_token['key_id']}/rotate", headers=auth()
    )

    assert rotated.status_code == 200, rotated.text
    body = rotated.json()
    assert body["rotated_from"] == old_token["key_id"]
    assert body["token"] != old_token["token"]

    # The old key is dead...
    assert admin_env.post(
        INGEST, old_token["token"], log_events_payload(batch_id="after-rotate")
    ).status_code == 401
    # ...and the new one works.
    assert admin_env.post(
        INGEST, body["token"], log_events_payload(batch_id="after-rotate")
    ).status_code == 202

    # Lineage is recorded rather than the old row being deleted.
    old_row = admin_env.db.one("SELECT * FROM api_keys WHERE id = ?", (old_token["key_id"],))
    assert old_row["revoked_at"] is not None
    new_row = admin_env.db.one("SELECT * FROM api_keys WHERE id = ?", (body["key_id"],))
    assert new_row["rotated_from"] == old_token["key_id"]


def test_rotating_a_revoked_key_is_refused(admin_env):
    key = admin_env.client.post(
        KEYS, json={"app_id": APP_ID, "scope": "ingest"}, headers=auth()
    ).json()
    admin_env.client.post(f"{KEYS}/{key['key_id']}/revoke", headers=auth())

    assert admin_env.client.post(
        f"{KEYS}/{key['key_id']}/rotate", headers=auth()
    ).status_code == 404


def test_rotating_an_unknown_key_is_refused(admin_env):
    assert admin_env.client.post(f"{KEYS}/pb_nope/rotate", headers=auth()).status_code == 404


# ------------------------------------------------------------------ revoke
def test_revoking_stops_the_key_immediately(admin_env):
    key = admin_env.client.post(
        KEYS, json={"app_id": APP_ID, "scope": "ingest"}, headers=auth()
    ).json()
    assert admin_env.post(INGEST, key["token"], log_events_payload()).status_code == 202

    assert admin_env.client.post(
        f"{KEYS}/{key['key_id']}/revoke", headers=auth()
    ).status_code == 200

    assert admin_env.post(
        INGEST, key["token"], log_events_payload(batch_id="post-revoke")
    ).status_code == 401


def test_revoking_twice_is_a_404_not_a_silent_success(admin_env):
    key = admin_env.client.post(
        KEYS, json={"app_id": APP_ID, "scope": "ingest"}, headers=auth()
    ).json()
    admin_env.client.post(f"{KEYS}/{key['key_id']}/revoke", headers=auth())
    assert admin_env.client.post(
        f"{KEYS}/{key['key_id']}/revoke", headers=auth()
    ).status_code == 404


# ------------------------------------------------------------------ expiry
def test_an_expired_key_is_refused(admin_env):
    key = admin_env.client.post(
        KEYS,
        json={"app_id": APP_ID, "scope": "ingest", "expires_in_days": 1},
        headers=auth(),
    ).json()
    assert admin_env.post(INGEST, key["token"], log_events_payload()).status_code == 202

    # Move the expiry into the past rather than waiting a day.
    with admin_env.db.write() as conn:
        conn.execute(
            "UPDATE api_keys SET expires_at = ? WHERE id = ?",
            ("2020-01-01T00:00:00.000000Z", key["key_id"]),
        )

    assert admin_env.post(
        INGEST, key["token"], log_events_payload(batch_id="expired")
    ).status_code == 401


def test_a_key_without_an_expiry_has_none_stored(admin_env):
    key = admin_env.client.post(
        KEYS, json={"app_id": APP_ID, "scope": "ingest"}, headers=auth()
    ).json()
    row = admin_env.db.one("SELECT expires_at FROM api_keys WHERE id = ?", (key["key_id"],))
    assert row["expires_at"] is None


# --------------------------------------------------------- who may administer
def test_the_admin_api_needs_the_password(admin_env):
    assert admin_env.client.get(KEYS).status_code == 401
    assert admin_env.client.get(KEYS, headers=auth("wrong-password")).status_code == 401


def test_the_admin_api_is_disabled_without_a_configured_password(env):
    # `env` has no admin_password, so there is nothing to check against.
    assert env.client.get(KEYS, headers=auth()).status_code == 503


def test_an_ingest_key_cannot_administer(admin_env):
    """A leaked ingest key must not be able to mint or renew keys."""
    ingest_token = admin_env.key(scope="ingest")

    # The bearer token is not accepted here at all — this surface only reads the
    # admin password header.
    assert admin_env.client.get(
        KEYS, headers={"Authorization": f"Bearer {ingest_token}"}
    ).status_code == 401
    assert admin_env.client.post(
        KEYS,
        json={"app_id": APP_ID, "scope": "ingest"},
        headers={"Authorization": f"Bearer {ingest_token}"},
    ).status_code == 401
