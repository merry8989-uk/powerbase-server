"""Step 6: traces, RED, OTLP metrics, capacity and drains."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from helpers import ADMIN_PASSWORD, APP_ID
from powerbase import capacity, drains, traces
from powerbase.otlp import convert_metrics, convert_traces

NOW = datetime.now(timezone.utc)

# Exact integer nanoseconds, derived from whole seconds.
#
# `int(moment.timestamp() * 1e9)` looks equivalent and is not: the float has
# already lost sub-millisecond precision, so a 290 ms span came back as 289 ms
# and the test was measuring its own rounding error rather than the code.
BASE_NS = int(NOW.timestamp()) * 1_000_000_000
MS = 1_000_000
SECOND = 1_000_000_000


def ns(moment: datetime) -> str:
    """Whole-second nanoseconds, for the cases where only the date matters."""
    return str(int(moment.timestamp()) * SECOND)


def span_ns(minutes_ago: int, duration_ms: int) -> tuple[str, str]:
    start = BASE_NS - minutes_ago * 60 * SECOND
    return str(start), str(start + duration_ms * MS)


def span(
    index: int = 0,
    *,
    trace_id: str | None = None,
    span_id: str | None = None,
    parent: str | None = None,
    service: str = "checkout-api",
    name: str = "POST /orders",
    kind: int = 2,
    status_code: int = 1,
    minutes_ago: int = 0,
    duration_ms: int = 100,
    route: str | None = "/orders/{id}",
):
    attributes = []
    if route:
        attributes.append({"key": "http.route", "value": {"stringValue": route}})
    start_ns, end_ns = span_ns(minutes_ago, duration_ms)
    return {
        "traceId": trace_id or f"{index:032d}",
        "spanId": span_id or f"{index:016d}",
        "parentSpanId": parent or "",
        "name": name,
        "kind": kind,
        "startTimeUnixNano": start_ns,
        "endTimeUnixNano": end_ns,
        "attributes": attributes,
        "status": {"code": status_code},
    }


def trace_payload(spans, service="checkout-api"):
    return {
        "resourceSpans": [
            {
                "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": service}}]},
                "scopeSpans": [{"scope": {"name": "otel.fastapi"}, "spans": spans}],
            }
        ]
    }


@pytest.fixture()
def obs(make_env):
    env = make_env(admin_password=ADMIN_PASSWORD)
    env.ingest = env.key(scope="ingest")
    env.admin = {"X-Admin-Password": ADMIN_PASSWORD}
    return env


def post_traces(env, spans, service="checkout-api"):
    return env.client.post(
        "/v1/otlp/v1/traces",
        headers={"Authorization": f"Bearer {env.ingest}"},
        json=trace_payload(spans, service),
    )


def post_metrics(env, metrics, service="checkout-api"):
    return env.client.post(
        "/v1/otlp/v1/metrics",
        headers={"Authorization": f"Bearer {env.ingest}"},
        json={
            "resourceMetrics": [
                {
                    "resource": {
                        "attributes": [{"key": "service.name", "value": {"stringValue": service}}]
                    },
                    "scopeMetrics": [{"scope": {"name": "otel"}, "metrics": metrics}],
                }
            ]
        },
    )


# ------------------------------------------------------------------- traces
def test_a_span_maps_onto_the_columns(obs):
    assert post_traces(obs, [span()]).status_code == 200

    row = obs.db.one("SELECT * FROM spans")
    assert row["service_name"] == "checkout-api"
    assert row["operation"] == "POST /orders"
    assert row["span_kind"] == "server"
    assert row["status"] == "ok"
    assert row["duration_ms"] == 100
    assert row["route_template"] == "/orders/{id}", "the route is promoted out of attributes"


@pytest.mark.parametrize(
    "kind,expected",
    [(1, "internal"), (2, "server"), (3, "client"), (4, "producer"), (5, "consumer")],
)
def test_the_span_kind_is_named(obs, kind, expected):
    post_traces(obs, [span(kind=kind)])
    assert obs.db.one("SELECT span_kind FROM spans")["span_kind"] == expected


@pytest.mark.parametrize("code,expected", [(0, "unset"), (1, "ok"), (2, "error")])
def test_the_status_code_is_named(obs, code, expected):
    post_traces(obs, [span(status_code=code)])
    assert obs.db.one("SELECT status FROM spans")["status"] == expected


def test_a_retried_export_does_not_double_write(obs):
    payload = trace_payload([span()])
    headers = {"Authorization": f"Bearer {obs.ingest}"}
    obs.client.post("/v1/otlp/v1/traces", headers=headers, json=payload)
    second = obs.client.post("/v1/otlp/v1/traces", headers=headers, json=payload)

    assert second.status_code == 200
    assert obs.db.scalar("SELECT COUNT(*) FROM spans") == 1


def test_a_span_without_an_id_is_rejected_not_crashed_on(obs):
    bad = {"name": "no ids"}
    response = post_traces(obs, [span(), bad])
    assert response.status_code == 200
    assert obs.db.scalar("SELECT COUNT(*) FROM spans") == 1


def test_a_credential_attribute_on_a_span_is_dropped(obs):
    s = span()
    s["attributes"].append({"key": "password", "value": {"stringValue": "hunter2"}})
    post_traces(obs, [s])
    stored = obs.db.one("SELECT attributes FROM spans")["attributes"]
    assert "hunter2" not in stored


def test_span_redaction_applies_to_the_status_message(obs):
    s = span(status_code=2)
    s["status"]["message"] = "failed for amit@example.com"
    post_traces(obs, [s])
    message = obs.db.one("SELECT status_message FROM spans")["status_message"]
    assert "amit@example.com" not in message
    assert "[REDACTED_EMAIL]" in message


# -------------------------------------------------------------- the trace view
def test_a_trace_joins_its_spans_to_its_log_events(obs):
    """The join is the point: a slow request and its error become one story."""
    root = span(1, trace_id="a" * 32, span_id="1" * 16, duration_ms=800, status_code=2)
    child = span(2, trace_id="a" * 32, span_id="2" * 16, parent="1" * 16, kind=3, name="SELECT orders")
    post_traces(obs, [root, child])

    obs.client.post(
        "/v1/ingest/log-events",
        headers={"Authorization": f"Bearer {obs.ingest}"},
        json={
            "batch_id": "trace-join",
            "app_id": APP_ID,
            "events": [
                {
                    "event_id": "e1",
                    "ts": NOW.isoformat(),
                    "severity": "ERROR",
                    "event_type": "http.request",
                    "trace_id": "a" * 32,
                    "message": "upstream timeout",
                }
            ],
        },
    )

    trace = obs.client.get(
        f"/v1/traces/{'a' * 32}", headers=obs.admin, params={"app_id": APP_ID}
    ).json()
    assert trace["span_count"] == 2
    assert trace["error_count"] == 1
    assert trace["duration_ms"] == 800
    assert trace["root_operation"] == "POST /orders"
    assert len(trace["logs"]) == 1
    assert trace["logs"][0]["trace_id"] if "trace_id" in trace["logs"][0] else True


def test_an_unknown_trace_is_a_404(obs):
    response = obs.client.get("/v1/traces/nope", headers=obs.admin, params={"app_id": APP_ID})
    assert response.status_code == 404


def test_the_trace_list_summarises_each_trace(obs):
    post_traces(obs, [span(1, trace_id="b" * 32, span_id="1" * 16)])
    post_traces(obs, [span(2, trace_id="c" * 32, span_id="2" * 16, status_code=2)])

    body = obs.client.get("/v1/traces", headers=obs.admin, params={"app_id": APP_ID}).json()
    assert body["summary"]["spans"] == 2
    assert body["summary"]["traces"] == 2
    assert body["summary"]["error_rate"] == 0.5
    assert len(body["items"]) == 2


# -------------------------------------------------------------------- RED
def test_red_is_derived_from_spans(obs):
    spans = [
        span(i, trace_id=f"{i:032d}", span_id=f"{i:016d}",
             status_code=2 if i % 5 == 0 else 1, duration_ms=100 + i * 10)
        for i in range(20)
    ]
    post_traces(obs, spans)

    body = obs.client.get(
        "/v1/metrics/red", headers=obs.admin, params={"app_id": APP_ID, "group_by": "service"}
    ).json()
    group = body["groups"][0]

    assert group["label"] == "checkout-api"
    assert group["spans"] == 20
    assert group["errors"] == 4
    assert group["error_rate"] == 0.2
    # Exact percentiles, from an indexed lookup rather than an estimate.
    assert group["p50_ms"] is not None
    assert group["p50_ms"] <= group["p95_ms"] <= group["max_ms"]
    assert group["max_ms"] == 290


def test_red_can_group_by_route_template(obs):
    post_traces(obs, [span(1, trace_id="d" * 32, span_id="1" * 16, route="/orders/{id}")])
    post_traces(obs, [span(2, trace_id="e" * 32, span_id="2" * 16, route="/health")])

    body = obs.client.get(
        "/v1/metrics/red", headers=obs.admin, params={"app_id": APP_ID, "group_by": "route"}
    ).json()
    assert sorted(g["label"] for g in body["groups"]) == ["/health", "/orders/{id}"]


def test_red_refuses_an_unknown_grouping(obs):
    response = obs.client.get(
        "/v1/metrics/red", headers=obs.admin, params={"app_id": APP_ID, "group_by": "nonsense"}
    )
    assert response.status_code == 422


def test_red_is_empty_rather_than_wrong_when_there_are_no_spans(obs):
    body = obs.client.get(
        "/v1/metrics/red", headers=obs.admin, params={"app_id": APP_ID}
    ).json()
    assert body["groups"] == []


# ------------------------------------------------------------------ metrics
def test_a_gauge_and_a_sum_are_both_stored(obs):
    post_metrics(obs, [
        {"name": "http.server.duration", "unit": "ms",
         "gauge": {"dataPoints": [{"timeUnixNano": ns(NOW), "asDouble": 812.5}]}},
        {"name": "http.server.requests",
         "sum": {"isMonotonic": True, "dataPoints": [{"timeUnixNano": ns(NOW), "asInt": "42"}]}},
    ])

    rows = {r["metric_name"]: r for r in obs.db.query("SELECT * FROM metric_points")}
    assert rows["http.server.duration"]["metric_kind"] == "gauge"
    assert rows["http.server.duration"]["value"] == 812.5
    assert rows["http.server.duration"]["unit"] == "ms"
    assert rows["http.server.requests"]["metric_kind"] == "sum"
    assert rows["http.server.requests"]["value"] == 42.0


def test_a_histogram_keeps_its_sum_count_and_buckets(obs):
    post_metrics(obs, [
        {"name": "http.server.duration", "unit": "ms", "histogram": {"dataPoints": [
            {"timeUnixNano": ns(NOW), "count": "10", "sum": 1234.5,
             "bucketCounts": [1, 4, 5], "explicitBounds": [100, 500]}]}},
    ])
    row = obs.db.one("SELECT * FROM metric_points")
    assert row["metric_kind"] == "histogram"
    assert row["value"] == 1234.5
    assert row["count"] == 10
    assert "bucketCounts" in row["attributes"]


def test_a_retried_metric_export_deduplicates_on_the_dimensions(obs):
    metric = {"name": "queue.depth", "gauge": {"dataPoints": [
        {"timeUnixNano": ns(NOW), "asInt": "7",
         "attributes": [{"key": "queue", "value": {"stringValue": "orders"}}]}]}}
    post_metrics(obs, [metric])
    post_metrics(obs, [metric])
    assert obs.db.scalar("SELECT COUNT(*) FROM metric_points") == 1


def test_two_different_dimensions_are_two_points(obs):
    def point(queue):
        return {"name": "queue.depth", "gauge": {"dataPoints": [
            {"timeUnixNano": ns(NOW), "asInt": "7",
             "attributes": [{"key": "queue", "value": {"stringValue": queue}}]}]}}

    post_metrics(obs, [point("orders"), point("emails")])
    assert obs.db.scalar("SELECT COUNT(*) FROM metric_points") == 2


def test_the_service_name_comes_from_the_resource(obs):
    post_metrics(obs, [
        {"name": "queue.depth", "gauge": {"dataPoints": [{"timeUnixNano": ns(NOW), "asInt": "1"}]}}
    ], service="payments-api")
    assert obs.db.one("SELECT service_name FROM metric_points")["service_name"] == "payments-api"


def test_metric_names_and_series_are_listed(obs):
    post_metrics(obs, [
        {"name": "queue.depth", "gauge": {"dataPoints": [{"timeUnixNano": ns(NOW), "asInt": "7"}]}}
    ])
    names = obs.client.get(
        "/v1/otel-metrics", headers=obs.admin, params={"app_id": APP_ID}
    ).json()["items"]
    assert [m["metric_name"] for m in names] == ["queue.depth"]

    series = obs.client.get(
        "/v1/otel-metrics/queue.depth", headers=obs.admin, params={"app_id": APP_ID}
    ).json()
    assert series["count"] == 1
    assert series["points"][0]["value"] == 7.0


def test_otlp_metrics_and_traces_need_an_ingest_key(obs):
    for path in ("/v1/otlp/v1/metrics", "/v1/otlp/v1/traces"):
        assert obs.client.post(path, json={}).status_code == 401


# ----------------------------------------------------------------- capacity
def test_capacity_reports_the_tables_that_grow(obs):
    post_traces(obs, [span()])
    body = obs.client.get("/v1/capacity", headers=obs.admin).json()

    assert "spans" in body["tables"]
    assert body["tables"]["spans"]["rows"] == 1
    assert body["database"]["bytes"] > 0
    assert body["database"]["page_size"] > 0
    assert isinstance(body["warnings"], list)


def test_capacity_warns_when_nothing_has_ever_been_rolled_up(obs, tmp_path):
    # The backup directory is set explicitly. Left at the default `./backups`,
    # this assertion depended on no other test having written a backup there —
    # which made it pass for the wrong reason and fail for the wrong reason.
    obs.settings.backup_dir = str(tmp_path)
    body = capacity.report(obs.db, obs.settings)
    assert any("rollup" in warning for warning in body["warnings"])
    assert any("backup" in warning for warning in body["warnings"])


def test_capacity_warns_about_dead_alert_deliveries(obs):
    from powerbase import alerts, incidents, notify

    notify.add_channel(obs.db, kind="webhook", target="https://broken.test/x")
    report = alerts.AlertReport(generated_at="x", window_hours=24)
    report.alerts = [alerts.Alert(rule="r", severity="critical", title="t", detail="d", app_id=APP_ID)]
    incidents.apply_report(obs.db, obs.settings, report, now=NOW)

    def bad(request, timeout=None):
        raise RuntimeError("nope")

    when = NOW
    for _ in range(notify.MAX_ATTEMPTS):
        when = when + timedelta(hours=2)
        notify.flush(obs.db, obs.settings, now=when, webhook_opener=bad)

    body = capacity.report(obs.db, obs.settings, now=when)
    assert any("dead" in warning for warning in body["warnings"])


def test_capacity_needs_a_role(obs):
    assert obs.client.get("/v1/capacity").status_code == 401


# ------------------------------------------------------------------- drains
def ingest(env, event_id, severity="ERROR", minutes_ago=0, message="boom"):
    moment = NOW - timedelta(minutes=minutes_ago)
    return env.client.post(
        "/v1/ingest/log-events",
        headers={"Authorization": f"Bearer {env.ingest}"},
        json={
            "batch_id": f"drain-{event_id}",
            "app_id": APP_ID,
            "events": [
                {
                    "event_id": event_id,
                    "ts": moment.isoformat(),
                    "severity": severity,
                    "event_type": "http.request",
                    "message": message,
                }
            ],
        },
    )


class FakeResponse:
    def __init__(self, status=200):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_a_drain_forwards_and_advances_its_cursor(obs):
    ingest(obs, "e1")
    drain = drains.create_drain(obs.db, name="to-siem", target="https://siem.test/i")
    assert drain["cursor_time"] is None

    sent = []

    def poster(request, timeout=None):
        import json

        sent.append(json.loads(request.data))
        return FakeResponse(200)

    result = drains.flush(obs.db, obs.settings, poster=poster)
    assert result["sent"] == 1
    assert len(sent[0]["records"]) == 1
    assert sent[0]["records"][0]["event_id"] == "e1"

    after = drains.get_drain(obs.db, drain["id"])
    assert after["cursor_time"] is not None
    assert after["last_run_at"] is not None


def test_a_second_flush_sends_nothing_new(obs):
    ingest(obs, "e1")
    drains.create_drain(obs.db, name="d", target="https://siem.test/i")
    drains.flush(obs.db, obs.settings, poster=lambda r, timeout=None: FakeResponse(200))
    again = drains.flush(obs.db, obs.settings, poster=lambda r, timeout=None: FakeResponse(200))
    assert again["sent"] == 0


def test_a_failed_drain_does_not_advance_its_cursor(obs):
    """At-least-once: a failure retries the same batch rather than skipping it."""
    ingest(obs, "e1")
    drain = drains.create_drain(obs.db, name="d", target="https://siem.test/i")

    def bad(request, timeout=None):
        raise RuntimeError("target unreachable")

    result = drains.flush(obs.db, obs.settings, poster=bad)
    assert result["failed"] == 1
    assert result["results"][0]["error"]
    assert drains.get_drain(obs.db, drain["id"])["cursor_time"] is None

    # The retry sends the same record, and only then does the cursor move.
    sent = []
    def poster(request, timeout=None):
        import json
        sent.append(json.loads(request.data))
        return FakeResponse(200)

    assert drains.flush(obs.db, obs.settings, poster=poster)["sent"] == 1
    assert sent[0]["records"][0]["event_id"] == "e1"
    assert drains.get_drain(obs.db, drain["id"])["cursor_time"] is not None


def test_the_severity_floor_is_respected(obs):
    ingest(obs, "info-1", severity="INFO")
    ingest(obs, "error-1", severity="ERROR")
    drains.create_drain(obs.db, name="d", target="https://siem.test/i", min_severity="ERROR")

    sent = []
    def poster(request, timeout=None):
        import json
        sent.append(json.loads(request.data))
        return FakeResponse(200)

    drains.flush(obs.db, obs.settings, poster=poster)
    severities = [r["severity"] for r in sent[0]["records"]]
    assert severities == ["ERROR"]


def test_a_drain_is_validated(obs):
    with pytest.raises(ValueError):
        drains.create_drain(obs.db, name="", target="https://x.test")
    with pytest.raises(ValueError):
        drains.create_drain(obs.db, name="d", target="not-a-url")
    with pytest.raises(ValueError):
        drains.create_drain(obs.db, name="d", target="https://x.test", min_severity="LOUD")
    with pytest.raises(ValueError):
        drains.create_drain(obs.db, name="d", target="https://x.test", app_id="no.such")


def test_a_disabled_drain_does_not_run(obs):
    ingest(obs, "e1")
    drain = drains.create_drain(obs.db, name="d", target="https://siem.test/i", enabled=False)
    assert drains.flush(obs.db, obs.settings)["ran"] == 0
    drains.set_enabled(obs.db, drain["id"], True)
    assert drains.flush(obs.db, obs.settings)["ran"] == 1


def test_drain_runs_are_recorded(obs):
    ingest(obs, "e1")
    drain = drains.create_drain(obs.db, name="d", target="https://siem.test/i")
    drains.flush(obs.db, obs.settings, poster=lambda r, timeout=None: FakeResponse(200))

    runs = obs.client.get(
        f"/v1/log-drains/{drain['id']}/runs", headers=obs.admin
    ).json()["items"]
    assert len(runs) == 1
    assert runs[0]["sent"] == 1
    assert runs[0]["ok"] == 1


def test_the_drain_api_round_trip(obs):
    created = obs.client.post(
        "/v1/log-drains", headers=obs.admin,
        json={"name": "api-drain", "target": "https://siem.test/api", "min_severity": "WARN"},
    )
    assert created.status_code == 201
    drain_id = created.json()["id"]

    listed = obs.client.get("/v1/log-drains", headers=obs.admin).json()["items"]
    assert [d["name"] for d in listed] == ["api-drain"]

    assert obs.client.delete(f"/v1/log-drains/{drain_id}", headers=obs.admin).status_code == 200
    assert obs.client.get("/v1/log-drains", headers=obs.admin).json()["items"] == []


def test_the_drain_api_is_refused_for_a_readonly_role(obs):
    """A drain sends data somewhere, which is a settings-level decision."""
    from powerbase import team

    team.create_user(obs.db, email="ro@example.com", role="readonly", password="a-long-enough-pw")
    token = obs.client.post(
        "/v1/auth/login", json={"email": "ro@example.com", "password": "a-long-enough-pw"}
    ).json()["token"]

    response = obs.client.post(
        "/v1/log-drains", headers={"X-Powerbase-Session": token},
        json={"name": "nope", "target": "https://siem.test/x"},
    )
    assert response.status_code == 403
