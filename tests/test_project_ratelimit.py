"""Per-project rate limiting.

The per-key bucket stops one key from hammering the server. It does not stop one
project from minting many keys and spending everyone else's headroom, which is
what the second bucket is for.
"""

from helpers import APP_ID, OTHER_APP_ID, log_events_payload
from powerbase.provision import provision_app

INGEST = "/v1/ingest/log-events"

# A key bucket far larger than the project bucket, so anything that trips is
# definitely the project limiter and not the key limiter.
WIDE_OPEN = {"rate_burst": 100_000, "rate_per_second": 100_000.0}
TIGHT_PROJECT = {"project_rate_burst": 2, "project_rate_per_second": 0.0001}


def test_two_keys_of_one_project_share_a_bucket(make_env):
    env = make_env(**WIDE_OPEN, **TIGHT_PROJECT)
    first = env.key(app_id=APP_ID)
    second = env.key(app_id=APP_ID)

    assert env.post(INGEST, first, log_events_payload(batch_id="b1")).status_code == 202
    # A different key, same project: it draws from the same budget.
    assert env.post(INGEST, second, log_events_payload(batch_id="b2")).status_code == 202

    over = env.post(INGEST, first, log_events_payload(batch_id="b3"))
    assert over.status_code == 429
    assert "project rate limit" in over.json()["detail"]
    assert over.headers.get("Retry-After")


def test_a_project_at_its_limit_does_not_affect_another_project(make_env):
    env = make_env(**WIDE_OPEN, project_rate_burst=1, project_rate_per_second=0.0001)
    provision_app(env.db, OTHER_APP_ID, "Other App")

    noisy = env.key(app_id=APP_ID)
    quiet = env.key(app_id=OTHER_APP_ID)

    assert env.post(INGEST, noisy, log_events_payload(batch_id="n1")).status_code == 202
    assert env.post(INGEST, noisy, log_events_payload(batch_id="n2")).status_code == 429
    # The other project still has its own budget untouched.
    assert env.post(
        INGEST, quiet, log_events_payload(batch_id="q1", app_id=OTHER_APP_ID)
    ).status_code == 202


def test_the_key_bucket_still_fires_on_its_own(make_env):
    """The two limiters are independent; a wide project bucket does not disable
    the per-key one."""
    env = make_env(
        rate_burst=1,
        rate_per_second=0.0001,
        project_rate_burst=100_000,
        project_rate_per_second=100_000.0,
    )
    token = env.key(app_id=APP_ID)

    assert env.post(INGEST, token, log_events_payload(batch_id="k1")).status_code == 202
    over = env.post(INGEST, token, log_events_payload(batch_id="k2"))
    assert over.status_code == 429
    assert "project" not in over.json()["detail"]
