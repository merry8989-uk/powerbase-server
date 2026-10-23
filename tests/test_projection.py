"""Analytics events and crashes, projected into the diagnostics log.

The Logs Explorer reads `log_events`. Before this, a crash or a product
analytics event went only into its own table and was **invisible to the
explorer** — the one screen built to answer "what happened here". These tests
hold the projection to three promises:

* it happens, in the same transaction, so the two cannot diverge;
* it carries the redaction the source writer already applied;
* it is not a move — the source table still gets its row.
"""

from __future__ import annotations

import pytest

from helpers import APP_ID


@pytest.fixture()
def pj(make_env):
    env = make_env()
    env.ingest = env.key(scope="ingest")
    env.read = env.key(scope="read")
    env.h = {"Authorization": f"Bearer {env.ingest}"}
    env.r = {"Authorization": f"Bearer {env.read}"}
    return env


def crash(env, **over):
    body = {
        "ts": "2026-10-23T10:00:00Z",
        "device_id": "dev1",
        "session_id": "sess1",
        "kind": "java",
        "handled": False,
        "message": "upstream timeout for amit@example.com",
        "stacktrace": (
            "java.lang.RuntimeException: boom\n"
            "\tat com.example.shop.Checkout.apply(Checkout.kt:88)"
        ),
        "app_version": "1.4.0",
        "os_version": "Android 14",
    }
    body.update(over)
    return env.client.post(
        "/v1/ingest/crashes", headers=env.h,
        json={"batch_id": f"c-{over.get('ts', 'x')}", "app_id": APP_ID, "crashes": [body]},
    )


def analytics_event(env, **over):
    body = {
        "ts": "2026-10-23T10:01:00Z",
        "device_id": "dev1",
        "session_id": "sess1",
        "name": "order_placed",
        "category": "commerce",
        "screen": "/checkout",
        "properties": {"total": 1999, "currency": "INR"},
    }
    body.update(over)
    return env.client.post(
        "/v1/ingest/events", headers=env.h,
        json={"batch_id": f"e-{over.get('ts', 'x')}", "app_id": APP_ID, "events": [body]},
    )


def analytics_log(env, **over):
    body = {
        "ts": "2026-10-23T10:02:00Z",
        "device_id": "dev1",
        "session_id": "sess1",
        "level": "WARN",
        "tag": "sync",
        "message": "retrying sync",
        "context": {"attempt": 2},
    }
    body.update(over)
    return env.client.post(
        "/v1/ingest/logs", headers=env.h,
        json={"batch_id": f"l-{over.get('ts', 'x')}", "app_id": APP_ID, "logs": [body]},
    )


def projected(env, event_type):
    """Raw rows, with `attributes` decoded.

    A raw SELECT gives back the stored JSON text; the query layer is what turns
    it into a dict. Decoding here keeps the assertions readable.
    """
    import json

    rows = []
    for row in env.db.query(
        "SELECT * FROM log_events WHERE app_id = ? AND event_type = ?", (APP_ID, event_type)
    ):
        item = dict(row)
        item["attributes"] = json.loads(item["attributes"]) if item["attributes"] else {}
        rows.append(item)
    return rows


# ------------------------------------------------------------------- crashes
def test_a_crash_appears_in_the_diagnostics_log(pj):
    assert crash(pj).status_code == 202
    rows = projected(pj, "crash")
    assert len(rows) == 1
    row = rows[0]
    assert row["severity"] == "FATAL", "an unhandled crash is what the user saw"
    assert row["error_code"] == "java"
    assert row["app_version"] == "1.4.0"
    assert row["platform_version"] == "Android 14"
    assert row["category"] == "crash"


def test_a_handled_crash_is_not_the_same_severity(pj):
    crash(pj, handled=True, ts="2026-10-23T11:00:00Z")
    assert projected(pj, "crash")[0]["severity"] == "ERROR"


def test_the_projection_reuses_the_crash_fingerprint(pj):
    """So a crash group and its log events agree on identity."""
    crash(pj)
    source = pj.db.one("SELECT fingerprint FROM crashes")
    assert projected(pj, "crash")[0]["error_fingerprint"] == source["fingerprint"]


def test_the_first_stack_frame_travels_with_it(pj):
    crash(pj)
    attributes = projected(pj, "crash")[0]["attributes"]
    # The value, not the key: `in` on a dict checks keys, which is what this
    # assertion got wrong the first time it was written.
    assert "Checkout.kt:88" in attributes["stack_head"]
    assert attributes["device_id"] == "dev1"
    assert attributes["session_id"] == "sess1"
    assert attributes["handled"] is False


def test_the_source_row_is_still_written(pj):
    """A projection, not a move. The crash page must keep working."""
    crash(pj)
    assert pj.db.scalar("SELECT COUNT(*) FROM crashes") == 1
    assert pj.db.scalar("SELECT COUNT(*) FROM log_events") == 1


# ---------------------------------------------------------------- redaction
def test_redaction_is_carried_through_to_the_projection(pj):
    """Using the raw values would put in the explorer exactly what the crash
    page hides."""
    crash(pj)
    assert "amit@example.com" not in projected(pj, "crash")[0]["message"]
    assert "[REDACTED_EMAIL]" in projected(pj, "crash")[0]["message"]


# ---------------------------------------------------------- analytics events
def test_an_analytics_event_appears_as_a_log_event(pj):
    assert analytics_event(pj).status_code == 202
    row = projected(pj, "analytics.event")[0]
    assert row["severity"] == "INFO"
    assert row["message"] == "order_placed"
    assert row["category"] == "commerce"
    assert row["route_template"] == "/checkout", "the screen is the nearest thing to a route"


def test_the_event_properties_travel_as_attributes(pj):
    analytics_event(pj)
    attributes = projected(pj, "analytics.event")[0]["attributes"]
    assert attributes["total"] == 1999
    assert attributes["currency"] == "INR"
    assert attributes["device_id"] == "dev1"


def test_an_analytics_log_appears_with_its_own_level(pj):
    assert analytics_log(pj).status_code == 202
    row = projected(pj, "analytics.log")[0]
    assert row["severity"] == "WARN"
    assert row["message"] == "retrying sync"
    assert row["attributes"]["tag"] == "sync"
    assert row["attributes"]["attempt"] == 2


# ---------------------------------------------------------------- idempotency
def test_a_replayed_batch_does_not_project_twice(pj):
    """The batch ledger refuses the replay, so the projection does not run."""
    crash(pj)
    crash(pj)
    assert pj.db.scalar("SELECT COUNT(*) FROM crashes") == 1
    assert pj.db.scalar("SELECT COUNT(*) FROM log_events") == 1


def test_two_crashes_are_two_log_events(pj):
    crash(pj, ts="2026-10-23T10:00:00Z")
    crash(pj, ts="2026-10-23T12:00:00Z", message="a different failure")
    assert len(projected(pj, "crash")) == 2


# ---------------------------------------------------------------- the explorer
def test_the_explorer_can_find_a_crash_by_type(pj):
    crash(pj)
    body = pj.client.get(
        "/v1/query/log-events", headers=pj.r,
        params={"app_id": APP_ID, "event_type": "crash"},
    ).json()
    assert body["count"] == 1


def test_the_explorer_can_find_crashes_by_severity(pj):
    crash(pj)
    analytics_event(pj)
    body = pj.client.get(
        "/v1/query/log-events", headers=pj.r,
        params={"app_id": APP_ID, "severity": "FATAL"},
    ).json()
    assert body["count"] == 1, "the crash, and not the analytics event"


def test_all_three_kinds_land_in_one_place(pj):
    crash(pj)
    analytics_event(pj)
    analytics_log(pj)
    body = pj.client.get(
        "/v1/query/log-events", headers=pj.r, params={"app_id": APP_ID}
    ).json()
    assert body["count"] == 3
    assert sorted(row["event_type"] for row in body["items"]) == [
        "analytics.event", "analytics.log", "crash"
    ]


# ------------------------------------------------------------------ the module
def test_the_projected_event_id_is_derived_not_random():
    """So a collision would be a bug rather than a coin flip."""
    from powerbase import projection

    assert projection.event_id_for("crash", "app", 7) == "crash:app:7"
    assert projection.event_id_for("crash", "app", 7) == projection.event_id_for("crash", "app", 7)
    assert projection.event_id_for("crash", "app", 7) != projection.event_id_for("crash", "app", 8)


def test_the_analytics_source_is_named_once():
    from powerbase import projection

    assert projection.ANALYTICS_SOURCE == "android_sdk"
    assert projection.CRASH_SEVERITY == {True: "ERROR", False: "FATAL"}
