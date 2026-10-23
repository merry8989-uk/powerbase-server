from helpers import crashes_payload


def test_ingest_crash_computes_fingerprint(env):
    key = env.key("ingest")
    r = env.post("/v1/ingest/crashes", key, crashes_payload())
    assert r.status_code == 202
    assert env.db.scalar("SELECT COUNT(*) FROM crashes") == 1
    assert env.db.scalar("SELECT length(fingerprint) FROM crashes") == 32
    assert env.db.scalar("SELECT handled FROM crashes") == 0


def test_fingerprint_groups_same_crash_across_line_numbers(env):
    key = env.key("ingest")
    stack_a = "java.lang.NullPointerException\n\tat com.example.Foo.bar(Foo.java:42)"
    stack_b = "java.lang.NullPointerException\n\tat com.example.Foo.bar(Foo.java:991)"
    env.post("/v1/ingest/crashes", key, crashes_payload(batch_id="c1", stacktrace=stack_a))
    env.post("/v1/ingest/crashes", key, crashes_payload(batch_id="c2", stacktrace=stack_b))

    fps = [row["fingerprint"] for row in env.db.query("SELECT fingerprint FROM crashes")]
    assert len(fps) == 2
    assert fps[0] == fps[1]


def test_fingerprint_differs_for_different_crash(env):
    key = env.key("ingest")
    env.post(
        "/v1/ingest/crashes",
        key,
        crashes_payload(batch_id="c1", stacktrace="at com.example.Foo.bar(Foo.java:1)"),
    )
    env.post(
        "/v1/ingest/crashes",
        key,
        crashes_payload(batch_id="c2", stacktrace="at com.example.Baz.qux(Baz.java:1)"),
    )
    fps = {row["fingerprint"] for row in env.db.query("SELECT fingerprint FROM crashes")}
    assert len(fps) == 2


def test_crash_stacktrace_redacted(env):
    key = env.key("ingest")
    payload = crashes_payload()
    payload["crashes"][0]["message"] = "failed for amit@example.com"
    env.post("/v1/ingest/crashes", key, payload)
    message = env.db.scalar("SELECT message FROM crashes")
    assert "amit@example.com" not in message
