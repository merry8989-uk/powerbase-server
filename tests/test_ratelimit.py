from helpers import events_payload


def test_burst_then_throttle(make_env):
    # Tiny burst, negligible refill: the first few succeed, the rest are 429.
    env = make_env(rate_burst=3, rate_per_second=0.0001)
    key = env.key("ingest")

    codes = [
        env.post("/v1/ingest/events", key, events_payload(batch_id=f"rl-{i}")).status_code
        for i in range(6)
    ]
    assert codes.count(202) == 3
    assert 429 in codes


def test_429_carries_retry_after(make_env):
    env = make_env(rate_burst=1, rate_per_second=0.0001)
    key = env.key("ingest")
    env.post("/v1/ingest/events", key, events_payload(batch_id="a"))
    r = env.post("/v1/ingest/events", key, events_payload(batch_id="b"))
    assert r.status_code == 429
    assert "retry-after" in {k.lower() for k in r.headers}


def test_rate_limit_is_per_key(make_env):
    env = make_env(rate_burst=1, rate_per_second=0.0001)
    key_a = env.key("ingest")
    key_b = env.key("ingest")

    assert env.post("/v1/ingest/events", key_a, events_payload(batch_id="a")).status_code == 202
    # key_b has its own bucket and is unaffected by key_a's usage
    assert env.post("/v1/ingest/events", key_b, events_payload(batch_id="b")).status_code == 202
