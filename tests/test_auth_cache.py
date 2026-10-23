"""API-key verification caching.

The cache exists because Argon2id on every request measured as ~370 ms — on the
hot path that is a self-inflicted denial of service. These tests pin both halves
of the trade: the cache must actually skip the work, and it must not weaken
anything.
"""

import pytest

from powerbase import security
from helpers import events_payload


@pytest.fixture()
def counting_verifier(monkeypatch):
    """Count real Argon2 verifications while still performing them."""
    calls = {"n": 0}
    real = security.verify_secret

    def counted(stored_hash, secret):
        calls["n"] += 1
        return real(stored_hash, secret)

    monkeypatch.setattr("powerbase.api.verify_secret", counted)
    return calls


def test_the_second_request_skips_argon2(env, counting_verifier):
    key = env.key("ingest")
    env.post("/v1/ingest/events", key, events_payload(batch_id="a"))
    first = counting_verifier["n"]
    assert first == 1

    for i in range(5):
        assert env.post("/v1/ingest/events", key, events_payload(batch_id=f"b{i}")).status_code == 202
    assert counting_verifier["n"] == first, "cached verifications should not re-run Argon2"


def test_a_bad_secret_is_never_cached(env, counting_verifier):
    token = env.key("ingest")
    prefix, key_id, _ = token.split("_")
    forged = f"{prefix}_{key_id}_{'b' * 64}"

    assert env.post("/v1/ingest/events", forged, events_payload()).status_code == 401
    assert env.post("/v1/ingest/events", forged, events_payload()).status_code == 401
    assert counting_verifier["n"] == 2, "a failed verification must not populate the cache"


def test_revocation_takes_effect_immediately_despite_the_cache(env, counting_verifier):
    from powerbase.provision import revoke_key

    token = env.key("ingest")
    assert env.post("/v1/ingest/events", token, events_payload(batch_id="a")).status_code == 202
    # The token is now cached...
    revoke_key(env.db, token.split("_")[1])
    # ...and must still be refused, because the key row is re-read every request.
    assert env.post("/v1/ingest/events", token, events_payload(batch_id="b")).status_code == 401


def test_scope_is_still_enforced_on_a_cached_key(env):
    read = env.key("read")
    headers = env.headers(read)
    assert env.client.get("/v1/query/events", headers=headers).status_code == 200
    # Same token, different requirement.
    assert env.post("/v1/ingest/events", read, events_payload()).status_code == 403


def test_cache_can_be_disabled(make_env, monkeypatch):
    env = make_env(auth_cache_seconds=0)
    calls = {"n": 0}
    real = security.verify_secret

    def counted(stored_hash, secret):
        calls["n"] += 1
        return real(stored_hash, secret)

    monkeypatch.setattr("powerbase.api.verify_secret", counted)
    key = env.key("ingest")
    for i in range(3):
        env.post("/v1/ingest/events", key, events_payload(batch_id=f"x{i}"))
    assert calls["n"] == 3
    assert env.app.state.verifications.enabled is False


def test_cache_expiry(monkeypatch):
    cache = security.VerificationCache(ttl_seconds=10)
    assert cache.get("k", "tok") is False
    cache.put("k", "tok")
    assert cache.get("k", "tok") is True

    # Move the clock past the TTL.
    real = security.time.monotonic
    monkeypatch.setattr(security.time, "monotonic", lambda: real() + 11)
    assert cache.get("k", "tok") is False


def test_cache_never_stores_the_plaintext_token():
    cache = security.VerificationCache(ttl_seconds=60)
    cache.put("k", "pb_supersecret")
    for key in cache._entries:
        assert "supersecret" not in str(key)
    assert cache.size() == 1


def test_cache_is_bounded():
    cache = security.VerificationCache(ttl_seconds=60, max_entries=4)
    for i in range(10):
        cache.put(f"k{i}", f"tok{i}")
    assert cache.size() <= 4


def test_different_tokens_are_not_interchangeable(env, counting_verifier):
    first = env.key("ingest")
    second = env.key("ingest")
    assert env.post("/v1/ingest/events", first, events_payload(batch_id="a")).status_code == 202
    assert env.post("/v1/ingest/events", second, events_payload(batch_id="b")).status_code == 202
    assert counting_verifier["n"] == 2
