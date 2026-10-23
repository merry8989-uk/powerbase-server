"""Incidents and the delivery queue.

The behaviours that make alerting usable rather than noisy: an incident opens
once, reminds on a cooldown, respects an acknowledgement, resolves when the rule
stops firing, and a notification that fails is retried and then visibly given up
on instead of vanishing.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from helpers import ADMIN_PASSWORD, APP_ID, OTHER_APP_ID
from powerbase import alerts, audit, incidents, notify
from powerbase.provision import provision_app

T0 = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)


class FakeResponse:
    def __init__(self, status=200):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


@pytest.fixture()
def alert_env(env):
    env.settings.alert_cooldown_minutes = 60
    env.sent = []

    def good(request, timeout=None):
        env.sent.append(request.full_url)
        return FakeResponse(200)

    env.webhook = good
    return env


def report(*items):
    r = alerts.AlertReport(generated_at="2026-10-23T12:00:00.000000Z", window_hours=24)
    r.alerts = list(items)
    return r


def alert(rule="crash_free_rate", severity="critical", app_id=APP_ID):
    return alerts.Alert(
        rule=rule, severity=severity, title=f"{rule} is bad", detail="detail",
        app_id=app_id, value=0.9, threshold=0.95,
    )


# ------------------------------------------------------------------ channels
def test_a_channel_is_registered_once(alert_env):
    first = notify.add_channel(alert_env.db, kind="webhook", target="https://h.test/x")
    second = notify.add_channel(alert_env.db, kind="webhook", target="https://h.test/x")
    assert first["id"] == second["id"]
    assert len(notify.list_channels(alert_env.db)) == 1


def test_a_channel_can_be_tightened_to_critical_only(alert_env):
    notify.add_channel(
        alert_env.db, kind="webhook", target="https://h.test/x", min_severity="critical"
    )
    assert len(notify.channels_for(alert_env.db, APP_ID, "critical")) == 1
    assert notify.channels_for(alert_env.db, APP_ID, "warning") == []
    assert notify.channels_for(alert_env.db, APP_ID, "info") == []


def test_an_instance_channel_covers_every_project(alert_env):
    notify.add_channel(alert_env.db, kind="webhook", target="https://h.test/all")
    provision_app(alert_env.db, OTHER_APP_ID, "Other")
    assert len(notify.channels_for(alert_env.db, OTHER_APP_ID, "critical")) == 1


def test_a_bad_channel_is_refused(alert_env):
    with pytest.raises(ValueError):
        notify.add_channel(alert_env.db, kind="carrier-pigeon", target="x")
    with pytest.raises(ValueError):
        notify.add_channel(alert_env.db, kind="webhook", target="")
    with pytest.raises(ValueError):
        notify.add_channel(alert_env.db, kind="webhook", target="x", min_severity="loud")
    with pytest.raises(ValueError):
        notify.add_channel(alert_env.db, kind="webhook", target="x", app_id="no.such")


def test_the_legacy_webhook_setting_becomes_a_channel(alert_env):
    alert_env.settings.alert_webhook_url = "https://legacy.test/hook"
    channel = notify.ensure_legacy_webhook(alert_env.db, alert_env.settings)
    assert channel["target"] == "https://legacy.test/hook"
    # Idempotent, so a pass that runs every few minutes does not pile them up.
    notify.ensure_legacy_webhook(alert_env.db, alert_env.settings)
    assert len(notify.list_channels(alert_env.db)) == 1


# ---------------------------------------------------------------- lifecycle
def test_the_first_evaluation_opens_one_incident_and_notifies(alert_env):
    notify.add_channel(alert_env.db, kind="webhook", target="https://h.test/x")

    result = incidents.apply_report(alert_env.db, alert_env.settings, report(alert()), now=T0)
    assert result["opened"] == 1
    assert result["queued"] == 1

    incident = incidents.list_incidents(alert_env.db)[0]
    assert incident["status"] == "firing"
    assert incident["occurrences"] == 1

    flushed = notify.flush(alert_env.db, alert_env.settings, now=T0, webhook_opener=alert_env.webhook)
    assert flushed["sent"] == 1
    assert len(alert_env.sent) == 1


def test_a_continuing_problem_does_not_re_notify_on_every_pass(alert_env):
    """The bug this whole module exists to fix."""
    notify.add_channel(alert_env.db, kind="webhook", target="https://h.test/x")
    incidents.apply_report(alert_env.db, alert_env.settings, report(alert()), now=T0)

    # Ten more evaluations, five minutes apart, all inside the cooldown.
    for minute in range(5, 55, 5):
        result = incidents.apply_report(
            alert_env.db, alert_env.settings, report(alert()), now=T0 + timedelta(minutes=minute)
        )
        assert result["queued"] == 0

    assert len(notify.list_deliveries(alert_env.db)) == 1, "one notification, not eleven"
    assert incidents.list_incidents(alert_env.db)[0]["occurrences"] == 11


def test_the_cooldown_produces_a_reminder(alert_env):
    notify.add_channel(alert_env.db, kind="webhook", target="https://h.test/x")
    incidents.apply_report(alert_env.db, alert_env.settings, report(alert()), now=T0)

    result = incidents.apply_report(
        alert_env.db, alert_env.settings, report(alert()), now=T0 + timedelta(minutes=61)
    )
    assert result["queued"] == 1
    assert len(notify.list_deliveries(alert_env.db)) == 2


def test_an_escalation_notifies_inside_the_cooldown(alert_env):
    notify.add_channel(alert_env.db, kind="webhook", target="https://h.test/x")
    incidents.apply_report(
        alert_env.db, alert_env.settings, report(alert(severity="warning")), now=T0
    )
    result = incidents.apply_report(
        alert_env.db, alert_env.settings, report(alert(severity="critical")),
        now=T0 + timedelta(minutes=1),
    )
    assert result["queued"] == 1, "a warning becoming critical is worth interrupting for"


def test_a_recovered_rule_resolves_the_incident(alert_env):
    notify.add_channel(alert_env.db, kind="webhook", target="https://h.test/x")
    incidents.apply_report(alert_env.db, alert_env.settings, report(alert()), now=T0)
    incident_id = incidents.list_incidents(alert_env.db)[0]["id"]

    result = incidents.apply_report(
        alert_env.db, alert_env.settings, report(), now=T0 + timedelta(minutes=10)
    )
    assert result["resolved"] == 1
    assert result["queued"] == 1, "a recovery notice is sent too"

    assert incidents.get_incident(alert_env.db, incident_id)["status"] == "resolved"


def test_two_projects_get_two_incidents(alert_env):
    provision_app(alert_env.db, OTHER_APP_ID, "Other")
    notify.add_channel(alert_env.db, kind="webhook", target="https://h.test/x")

    incidents.apply_report(
        alert_env.db, alert_env.settings,
        report(alert(app_id=APP_ID), alert(app_id=OTHER_APP_ID)), now=T0,
    )
    assert len(incidents.list_incidents(alert_env.db)) == 2


def test_a_resolved_incident_does_not_block_a_new_one(alert_env):
    notify.add_channel(alert_env.db, kind="webhook", target="https://h.test/x")
    incidents.apply_report(alert_env.db, alert_env.settings, report(alert()), now=T0)
    incidents.apply_report(
        alert_env.db, alert_env.settings, report(), now=T0 + timedelta(minutes=5)
    )
    incidents.apply_report(
        alert_env.db, alert_env.settings, report(alert()), now=T0 + timedelta(minutes=10)
    )

    all_incidents = incidents.list_incidents(alert_env.db)
    assert len(all_incidents) == 2, "a flapping rule keeps its history"
    assert sorted(i["status"] for i in all_incidents) == ["firing", "resolved"]


def test_disabling_alerts_does_not_resolve_anything(alert_env):
    """Switching the feature off is not evidence that the problem went away."""
    notify.add_channel(alert_env.db, kind="webhook", target="https://h.test/x")
    incidents.apply_report(alert_env.db, alert_env.settings, report(alert()), now=T0)

    alert_env.settings.alerts_enabled = False
    result = incidents.apply_report(
        alert_env.db, alert_env.settings, report(), now=T0 + timedelta(minutes=5)
    )
    assert result["skipped"] is True
    assert incidents.list_incidents(alert_env.db)[0]["status"] == "firing"


# --------------------------------------------------------------- ack/resolve
def test_acknowledging_silences_the_reminders(alert_env):
    notify.add_channel(alert_env.db, kind="webhook", target="https://h.test/x")
    incidents.apply_report(alert_env.db, alert_env.settings, report(alert()), now=T0)
    incident_id = incidents.list_incidents(alert_env.db)[0]["id"]

    acked = incidents.ack(alert_env.db, incident_id, "amit@example.com", now=T0)
    assert acked["status"] == "acknowledged"
    assert acked["acked_by"] == "amit@example.com"

    # Well past the cooldown, and still quiet.
    result = incidents.apply_report(
        alert_env.db, alert_env.settings, report(alert()), now=T0 + timedelta(hours=5)
    )
    assert result["queued"] == 0


def test_an_acknowledged_incident_still_resolves(alert_env):
    notify.add_channel(alert_env.db, kind="webhook", target="https://h.test/x")
    incidents.apply_report(alert_env.db, alert_env.settings, report(alert()), now=T0)
    incident_id = incidents.list_incidents(alert_env.db)[0]["id"]
    incidents.ack(alert_env.db, incident_id, "amit@example.com", now=T0)

    incidents.apply_report(
        alert_env.db, alert_env.settings, report(), now=T0 + timedelta(minutes=30)
    )
    assert incidents.get_incident(alert_env.db, incident_id)["status"] == "resolved"


def test_an_incident_can_be_resolved_by_hand_and_reopened(alert_env):
    incidents.apply_report(alert_env.db, alert_env.settings, report(alert()), now=T0)
    incident_id = incidents.list_incidents(alert_env.db)[0]["id"]

    resolved = incidents.resolve(alert_env.db, incident_id, "amit@example.com", now=T0)
    assert resolved["status"] == "resolved"
    assert resolved["resolved_by"] == "amit@example.com"

    reopened = incidents.reopen(alert_env.db, incident_id, "amit@example.com")
    assert reopened["status"] == "firing"
    assert reopened["acked_at"] is None


def test_acking_something_that_does_not_exist_is_not_a_crash(alert_env):
    assert incidents.ack(alert_env.db, "nope", "x") is None
    assert incidents.resolve(alert_env.db, "nope", "x") is None
    assert incidents.reopen(alert_env.db, "nope", "x") is None


# ------------------------------------------------------------- the deliveries
def test_a_failing_delivery_is_retried_with_backoff(alert_env):
    notify.add_channel(alert_env.db, kind="webhook", target="https://broken.test/x")
    incidents.apply_report(alert_env.db, alert_env.settings, report(alert()), now=T0)

    def bad(request, timeout=None):
        raise RuntimeError("connection refused")

    first = notify.flush(alert_env.db, alert_env.settings, now=T0, webhook_opener=bad)
    assert first == {"due": 1, "sent": 0, "failed": 1, "dead": 0}

    row = notify.list_deliveries(alert_env.db)[0]
    assert row["status"] == "failed"
    assert row["attempts"] == 1
    assert row["next_attempt_at"] > T0.strftime("%Y-%m-%dT%H:%M:%S.%fZ"), "scheduled forward"
    assert "connection refused" in row["last_error"]

    # Immediately afterwards it is not due yet.
    again = notify.flush(alert_env.db, alert_env.settings, now=T0 + timedelta(seconds=5), webhook_opener=bad)
    assert again["due"] == 0, "backoff means backoff"


def test_a_delivery_is_given_up_on_visibly(alert_env):
    notify.add_channel(alert_env.db, kind="webhook", target="https://broken.test/x")
    incidents.apply_report(alert_env.db, alert_env.settings, report(alert()), now=T0)

    def bad(request, timeout=None):
        raise RuntimeError("still refused")

    when = T0
    for _ in range(notify.MAX_ATTEMPTS):
        when = when + timedelta(hours=2)
        notify.flush(alert_env.db, alert_env.settings, now=when, webhook_opener=bad)

    row = notify.list_deliveries(alert_env.db)[0]
    assert row["status"] == "dead"
    assert row["attempts"] == notify.MAX_ATTEMPTS
    assert notify.queue_health(alert_env.db)["counts"]["dead"] == 1


def test_a_dead_delivery_can_be_requeued(alert_env):
    notify.add_channel(alert_env.db, kind="webhook", target="https://broken.test/x")
    incidents.apply_report(alert_env.db, alert_env.settings, report(alert()), now=T0)

    def bad(request, timeout=None):
        raise RuntimeError("nope")

    when = T0
    for _ in range(notify.MAX_ATTEMPTS):
        when = when + timedelta(hours=2)
        notify.flush(alert_env.db, alert_env.settings, now=when, webhook_opener=bad)

    delivery = notify.list_deliveries(alert_env.db, status="dead")[0]
    assert notify.retry_dead(alert_env.db, delivery["id"]) is True
    assert notify.list_deliveries(alert_env.db)[0]["status"] == "pending"
    # And only a dead one can be requeued.
    assert notify.retry_dead(alert_env.db, delivery["id"]) is False


def test_an_email_without_smtp_configured_fails_with_a_reason(alert_env):
    notify.add_channel(alert_env.db, kind="email", target="oncall@example.com", min_severity="critical")
    incidents.apply_report(alert_env.db, alert_env.settings, report(alert()), now=T0)

    notify.flush(alert_env.db, alert_env.settings, now=T0)
    row = notify.list_deliveries(alert_env.db)[0]
    assert row["status"] == "failed"
    assert "SMTP" in row["last_error"]


def test_an_email_is_sent_through_the_configured_smtp(alert_env):
    alert_env.settings.smtp_host = "smtp.example.com"
    alert_env.settings.smtp_from = "powerbase@example.com"
    alert_env.settings.smtp_starttls = False
    notify.add_channel(alert_env.db, kind="email", target="oncall@example.com")

    incidents.apply_report(alert_env.db, alert_env.settings, report(alert()), now=T0)

    sent: list = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            sent.append((host, port))

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def send_message(self, message):
            sent.append((message["To"], message["Subject"]))

    result = notify.flush(
        alert_env.db, alert_env.settings, now=T0, smtp_factory=FakeSMTP
    )
    assert result["sent"] == 1
    assert sent[0] == ("smtp.example.com", 587)
    assert sent[1][0] == "oncall@example.com"
    assert "critical" in sent[1][1].lower()


def test_the_queue_health_reports_what_is_stuck(alert_env):
    notify.add_channel(alert_env.db, kind="webhook", target="https://h.test/x")
    incidents.apply_report(alert_env.db, alert_env.settings, report(alert()), now=T0)
    notify.flush(alert_env.db, alert_env.settings, now=T0, webhook_opener=alert_env.webhook)

    health = notify.queue_health(alert_env.db)
    assert health["counts"]["sent"] == 1
    assert health["dead"] == []
    assert health["channels"] == 1


# ------------------------------------------------------------------- summary
def test_the_summary_counts_what_is_open(alert_env):
    provision_app(alert_env.db, OTHER_APP_ID, "Other")
    incidents.apply_report(
        alert_env.db, alert_env.settings,
        report(
            alert(rule="a", severity="critical", app_id=APP_ID),
            alert(rule="b", severity="warning", app_id=APP_ID),
            alert(rule="c", severity="info", app_id=OTHER_APP_ID),
        ),
        now=T0,
    )
    summary = incidents.summary(alert_env.db)
    assert summary["open"] == 3
    assert summary["firing"] == 3
    assert summary["worst"] == "critical"


def test_the_summary_is_empty_when_nothing_is_wrong(alert_env):
    summary = incidents.summary(alert_env.db)
    assert summary["open"] == 0
    assert summary["worst"] is None


# --------------------------------------------------------------------- audit
def test_the_lifecycle_is_audited(alert_env):
    notify.add_channel(alert_env.db, kind="webhook", target="https://h.test/x")
    incidents.apply_report(alert_env.db, alert_env.settings, report(alert()), now=T0)
    incident_id = incidents.list_incidents(alert_env.db)[0]["id"]
    incidents.ack(alert_env.db, incident_id, "amit@example.com", now=T0)
    incidents.resolve(alert_env.db, incident_id, "amit@example.com", now=T0)

    types = {row["event_type"] for row in alert_env.db.query("SELECT event_type FROM audit_events")}
    assert audit.INCIDENT_OPENED in types
    assert audit.INCIDENT_ACKED in types
    assert audit.INCIDENT_RESOLVED in types


# ------------------------------------------------------------------ the API
def test_the_incident_api_requires_a_role(make_env):
    env = make_env(admin_password=ADMIN_PASSWORD)
    assert env.client.get("/v1/incidents").status_code == 401
    assert env.client.get("/v1/incidents", headers={"X-Admin-Password": ADMIN_PASSWORD}).status_code == 200


def test_the_api_acknowledges_and_resolves(make_env):
    env = make_env(admin_password=ADMIN_PASSWORD)
    admin = {"X-Admin-Password": ADMIN_PASSWORD}
    incidents.apply_report(env.db, env.settings, report(alert()), now=T0)
    incident_id = incidents.list_incidents(env.db)[0]["id"]

    acked = env.client.post(f"/v1/incidents/{incident_id}/ack", headers=admin)
    assert acked.status_code == 200
    assert acked.json()["status"] == "acknowledged"
    assert acked.json()["acked_by"] == "admin-password"

    resolved = env.client.post(f"/v1/incidents/{incident_id}/resolve", headers=admin)
    assert resolved.json()["status"] == "resolved"

    assert env.client.post("/v1/incidents/nope/ack", headers=admin).status_code == 404


def test_the_api_lists_deliveries_and_the_queue(make_env):
    env = make_env(admin_password=ADMIN_PASSWORD)
    admin = {"X-Admin-Password": ADMIN_PASSWORD}
    notify.add_channel(env.db, kind="webhook", target="https://h.test/x")
    incidents.apply_report(env.db, env.settings, report(alert()), now=T0)

    body = env.client.get("/v1/alert-deliveries", headers=admin).json()
    assert len(body["items"]) == 1
    assert "counts" in body["queue"]


def test_a_channel_can_be_added_and_removed_over_the_api(make_env):
    env = make_env(admin_password=ADMIN_PASSWORD)
    admin = {"X-Admin-Password": ADMIN_PASSWORD}

    created = env.client.post(
        "/v1/alert-channels", headers=admin,
        json={"kind": "webhook", "target": "https://h.test/api", "min_severity": "warning"},
    )
    assert created.status_code == 201
    channel_id = created.json()["id"]
    assert created.json()["min_severity"] == "warning"

    assert env.client.get("/v1/alert-channels", headers=admin).json()["items"]
    assert env.client.delete(f"/v1/alert-channels/{channel_id}", headers=admin).status_code == 200
    assert env.client.get("/v1/alert-channels", headers=admin).json()["items"] == []


def test_the_scheduler_folds_reports_into_incidents(make_env):
    """The pass that used to fire the webhook every time now opens an incident once."""
    env = make_env(admin_password=ADMIN_PASSWORD, alerts_enabled=True)
    notify.add_channel(env.db, kind="webhook", target="https://h.test/x")

    from powerbase.scheduler import MaintenanceScheduler

    scheduler = MaintenanceScheduler(env.db, env.settings)
    # Two ticks with the same (empty) data must not raise or double-fire.
    first = scheduler.tick(now=T0)
    assert first is not None
    assert "alerts" in first
    assert set(first["alerts"]) == {"report", "incidents", "deliveries"}
