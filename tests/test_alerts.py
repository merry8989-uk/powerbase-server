"""Alert rules.

Two things to keep in mind about the fixtures:

* the alert window defaults to 24 h, while `seed_dataset` writes records whose
  `ts` is 2-5 days old — so the rule tests use a wide window on purpose;
* `received_at` is the time the server *accepted* the row, so seeded data always
  looks freshly ingested. That is the right semantic for "is the SDK still
  uploading?", and it means the silence rule needs its own setup.
"""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from powerbase import alerts, rollups
from helpers import ADMIN_PASSWORD, APP_ID

NOW = datetime.now(timezone.utc)


def rules(report):
    return {alert.rule for alert in report.alerts}


def alert_for(report, rule):
    return next((a for a in report.alerts if a.rule == rule), None)


# ------------------------------------------------------------ the happy path

def test_a_small_recent_dataset_stays_quiet(seeded_env):
    # 4 sessions is below the 20-sample floor, and `received_at` is fresh, so
    # nothing fires. An alert on a 4-session sample would be noise.
    report = alerts.evaluate(seeded_env.db, seeded_env.settings, now=NOW)
    assert report.ok
    assert report.alerts == []


def test_a_clean_app_raises_nothing(env):
    report = alerts.evaluate(env.db, env.settings, now=NOW)
    assert report.ok and report.alerts == []


def test_small_samples_do_not_alert(alert_env):
    wide = replace(alert_env.settings, min_samples_for_rates=50)
    report = alerts.evaluate(alert_env.db, wide, now=NOW)
    assert "crash_free_rate" not in rules(report)
    assert "error_rate" not in rules(report)


# ------------------------------------------------------------ crash-free rate

def test_crash_free_rate_fires(alert_env):
    report = alerts.evaluate(alert_env.db, alert_env.settings, now=NOW)
    alert = alert_for(report, "crash_free_rate")
    assert alert is not None
    assert alert.value == 0.5           # 2 of 4 sessions crashed
    assert alert.app_id == APP_ID
    assert "2 of 4 sessions" in alert.detail


def test_crash_free_rate_severity(alert_env):
    def severity(**overrides):
        settings = replace(alert_env.settings, 
            error_rate_warning=1.0, error_rate_critical=1.0, **overrides
        )
        found = alert_for(alerts.evaluate(alert_env.db, settings, now=NOW), "crash_free_rate")
        return found.severity if found else None

    assert severity(crash_free_rate_critical=0.9) == "critical"
    assert severity(crash_free_rate_critical=0.4, crash_free_rate_warning=0.99) == "warning"
    assert severity(crash_free_rate_critical=0.1, crash_free_rate_warning=0.2) is None


# ---------------------------------------------------------------- error rate

def test_error_rate_fires(alert_env):
    report = alerts.evaluate(alert_env.db, alert_env.settings, now=NOW)
    alert = alert_for(report, "error_rate")
    assert alert is not None
    assert alert.value == 0.5           # 2 of 4 log lines are ERROR
    assert alert.app_id == APP_ID


def test_error_rate_respects_the_threshold(alert_env):
    relaxed = replace(alert_env.settings, error_rate_warning=0.9, error_rate_critical=0.95)
    report = alerts.evaluate(alert_env.db, relaxed, now=NOW)
    assert "error_rate" not in rules(report)


# ------------------------------------------------------------------- silence

def test_silence_rule_fires_when_ingestion_stops(alert_env):
    # Looking at the database a month later: nothing has been received since.
    later = NOW + timedelta(days=30)
    report = alerts.evaluate(alert_env.db, alert_env.settings, now=later)
    alert = alert_for(report, "no_telemetry")
    assert alert is not None
    assert alert.severity == "warning"
    assert alert.value > 24 * 60


def test_silence_rule_ignores_an_app_with_no_history(env):
    report = alerts.evaluate(env.db, env.settings, now=NOW + timedelta(days=30))
    assert "no_telemetry" not in rules(report)


def test_recent_ingestion_does_not_trigger_silence(seeded_env):
    report = alerts.evaluate(seeded_env.db, seeded_env.settings, now=NOW)
    assert "no_telemetry" not in rules(report)


# ------------------------------------------------------------ maintenance jobs

def test_failed_job_fires(seeded_env):
    with seeded_env.db.write() as conn:
        conn.execute(
            "INSERT INTO job_runs (job, day, ran_at, ok, detail) VALUES (?, ?, ?, ?, ?)",
            ("backup", None, NOW.strftime("%Y-%m-%dT%H:%M:%S.%fZ"), 0, "disk full"),
        )
    report = alerts.evaluate(seeded_env.db, seeded_env.settings, now=NOW)
    alert = alert_for(report, "job_failed:backup")
    assert alert is not None
    assert alert.severity == "critical"
    assert "disk full" in alert.detail


def test_stale_rollup_fires(seeded_env):
    old_day = (NOW - timedelta(days=10)).strftime("%Y-%m-%d")
    rollups.rollup_day(seeded_env.db, old_day, now=NOW - timedelta(days=10))
    report = alerts.evaluate(seeded_env.db, seeded_env.settings, now=NOW)
    alert = alert_for(report, "rollup_stale")
    assert alert is not None
    assert alert.value == 10
    # And it explains the consequence, not just the fact.
    assert "pruning is deferred" in alert.detail


def test_fresh_rollups_do_not_fire(seeded_env):
    rollups.rollup_day(seeded_env.db, (NOW - timedelta(days=1)).strftime("%Y-%m-%d"), now=NOW)
    report = alerts.evaluate(seeded_env.db, seeded_env.settings, now=NOW)
    assert "rollup_stale" not in rules(report)


# ------------------------------------------------------------------- general

def test_evaluation_is_pure(seeded_env):
    first = alerts.evaluate(seeded_env.db, seeded_env.settings, now=NOW).to_dict()
    second = alerts.evaluate(seeded_env.db, seeded_env.settings, now=NOW).to_dict()
    assert first == second


def test_alerts_can_be_switched_off(seeded_env):
    off = replace(seeded_env.settings, alerts_enabled=False)
    report = alerts.evaluate(seeded_env.db, off, now=NOW)
    assert report.ok and report.alerts == []


def test_critical_sorts_before_warning(seeded_env):
    with seeded_env.db.write() as conn:
        conn.execute(
            "INSERT INTO job_runs (job, day, ran_at, ok, detail) VALUES (?, ?, ?, ?, ?)",
            ("prune", None, NOW.strftime("%Y-%m-%dT%H:%M:%S.%fZ"), 0, "locked"),
        )
    report = alerts.evaluate(seeded_env.db, seeded_env.settings, now=NOW)
    assert report.worst == "critical"
    assert report.alerts[0].severity == "critical"


def test_report_serialises(alert_env):
    body = alerts.evaluate(alert_env.db, alert_env.settings, now=NOW).to_dict()
    assert body["ok"] is False
    assert body["worst"] == "critical"
    assert body["counts"]["critical"] >= 1
    assert {"rule", "severity", "title", "detail", "app_id"} <= set(body["alerts"][0])


# ------------------------------------------------------------------- webhook

class FakeResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_webhook_posts_the_report(alert_env):
    captured = {}

    def opener(request, timeout=None):
        captured["url"] = request.full_url
        captured["body"] = request.data
        captured["type"] = request.headers.get("Content-type")
        return FakeResponse()

    report = alerts.evaluate(alert_env.db, alert_env.settings, now=NOW)
    result = alerts.post_webhook("https://hooks.example.com/x", report, opener=opener)

    assert result["ok"] is True
    assert captured["url"] == "https://hooks.example.com/x"
    assert captured["type"] == "application/json"
    assert b"crash_free_rate" in captured["body"]


def test_webhook_failure_is_swallowed(alert_env):
    def opener(request, timeout=None):
        raise OSError("connection refused")

    report = alerts.evaluate(alert_env.db, alert_env.settings, now=NOW)
    result = alerts.post_webhook("https://hooks.example.com/x", report, opener=opener)
    assert result["ok"] is False
    assert "connection refused" in result["error"]


# ----------------------------------------------------------------------- API

def test_alerts_endpoint(alert_env):
    headers = alert_env.headers(alert_env.key("read"))
    body = alert_env.client.get("/v1/alerts", headers=headers).json()
    assert body["ok"] is False
    assert body["worst"] == "critical"
    assert any(a["rule"] == "crash_free_rate" for a in body["alerts"])


def test_alerts_endpoint_requires_a_read_key(seeded_env):
    assert seeded_env.client.get("/v1/alerts").status_code == 401
    ingest = seeded_env.headers(seeded_env.key("ingest"))
    assert seeded_env.client.get("/v1/alerts", headers=ingest).status_code == 403


def test_alerts_endpoint_is_scoped_to_the_key_app(other_app):
    from helpers import OTHER_APP_ID

    headers = other_app.headers(other_app.key("read"))
    body = other_app.client.get("/v1/alerts", headers=headers).json()
    assert not any(a["app_id"] == OTHER_APP_ID for a in body["alerts"])


def test_alerts_endpoint_accepts_a_window(alert_env):
    headers = alert_env.headers(alert_env.key("read"))
    body = alert_env.client.get("/v1/alerts?window_hours=1", headers=headers).json()
    assert body["window_hours"] == 1
    # One hour is too narrow to see the seeded data, so nothing fires.
    assert body["alerts"] == []


# ----------------------------------------------------------------- dashboard

def test_dashboard_shows_a_banner(alert_env):
    alert_env.client.post("/login", data={"password": ADMIN_PASSWORD, "next": "/"})
    html = alert_env.client.get("/").text
    assert "alert-banner" in html
    assert "Details" in html


def test_dashboard_has_no_banner_when_healthy(seeded_env):
    seeded_env.client.post("/login", data={"password": ADMIN_PASSWORD, "next": "/"})
    assert "alert-banner" not in seeded_env.client.get("/").text


def test_alerts_page_lists_the_rules(alert_env):
    alert_env.client.post("/login", data={"password": ADMIN_PASSWORD, "next": "/"})
    html = alert_env.client.get("/alerts").text
    assert "crash_free_rate" in html
    assert "Active alerts" in html


def test_alerts_page_is_quiet_when_healthy(make_env):
    env = make_env(admin_password=ADMIN_PASSWORD)
    env.client.post("/login", data={"password": ADMIN_PASSWORD, "next": "/"})
    html = env.client.get("/alerts").text
    assert "Nothing is firing" in html
    assert "minimum sample size" not in html  # that phrase is in the copy, not a bug
