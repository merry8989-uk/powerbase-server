"""Alert rules.

A self-hosted tool has no pager, so "alerting" means: evaluate a small set of
conditions over the data, put them in front of whoever opens the dashboard, and
optionally push them at a webhook.

Two deliberate choices:

* **Every rule is a pure function of the database plus `now`.** No background
  state, no "already fired" bookkeeping. Evaluating twice returns the same
  answer, which makes the whole thing testable and idempotent.
* **Rules refuse to fire on a tiny sample.** A crash-free rate of 0% computed
  from three sessions is noise, and an alert that cries wolf is worse than no
  alert at all — people learn to ignore it.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from . import rollups
from .config import Settings
from .db import Database

SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}


@dataclass
class Alert:
    rule: str
    severity: str
    title: str
    detail: str
    app_id: str | None = None
    value: float | None = None
    threshold: float | None = None
    since: str | None = None

    def to_dict(self) -> dict:
        return {
            "rule": self.rule,
            "severity": self.severity,
            "title": self.title,
            "detail": self.detail,
            "app_id": self.app_id,
            "value": self.value,
            "threshold": self.threshold,
            "since": self.since,
        }


@dataclass
class AlertReport:
    generated_at: str
    window_hours: int
    alerts: list[Alert] = field(default_factory=list)

    @property
    def worst(self) -> str | None:
        if not self.alerts:
            return None
        return sorted(self.alerts, key=lambda a: SEVERITY_ORDER.get(a.severity, 9))[0].severity

    @property
    def ok(self) -> bool:
        return not self.alerts

    def to_dict(self) -> dict:
        return {
            "generated_at": self.generated_at,
            "window_hours": self.window_hours,
            "ok": self.ok,
            "worst": self.worst,
            "counts": {
                severity: sum(1 for a in self.alerts if a.severity == severity)
                for severity in ("critical", "warning", "info")
            },
            "alerts": [a.to_dict() for a in self.alerts],
        }


# --------------------------------------------------------------------------- #
# Rules
# --------------------------------------------------------------------------- #

def _rate_alert(
    rule: str, value: float, warning: float, critical: float, title: str, detail: str,
    app_id: str, sample: int, minimum: int, since: str, *, higher_is_worse: bool,
) -> Alert | None:
    """Build a rate alert, or None.

    `higher_is_worse` is not a detail: a crash-free *rate* is bad when it falls,
    an error *rate* is bad when it rises. Using one comparison for both means
    the error-rate rule can never fire, which is exactly the kind of silence an
    alerting system must not have.
    """
    if sample < minimum:
        return None

    if higher_is_worse:
        if value >= critical:
            severity = "critical"
        elif value >= warning:
            severity = "warning"
        else:
            return None
    else:
        if value <= critical:
            severity = "critical"
        elif value <= warning:
            severity = "warning"
        else:
            return None

    return Alert(
        rule=rule, severity=severity, title=title, detail=detail,
        app_id=app_id, value=round(value, 4),
        threshold=critical if severity == "critical" else warning,
        since=since,
    )


def evaluate(db: Database, settings: Settings, now: datetime | None = None) -> AlertReport:
    """Evaluate every rule across every app. Pure: same inputs, same output."""
    moment = now or datetime.now(timezone.utc)
    window_hours = settings.alert_window_hours
    since = moment - timedelta(hours=window_hours)
    since_iso = since.strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    report = AlertReport(generated_at=moment.strftime("%Y-%m-%dT%H:%M:%S.%fZ"), window_hours=window_hours)
    if not settings.alerts_enabled:
        return report

    apps = db.query("SELECT id, name FROM apps ORDER BY id")

    for app in apps:
        app_id = app["id"]

        sessions = db.scalar(
            "SELECT COUNT(*) FROM sessions WHERE app_id = ? AND started_at >= ?", (app_id, since_iso)
        ) or 0
        crashed = db.scalar(
            "SELECT COUNT(DISTINCT session_id) FROM crashes "
            "WHERE app_id = ? AND ts >= ? AND session_id IS NOT NULL",
            (app_id, since_iso),
        ) or 0

        if sessions:
            crash_free = (sessions - min(crashed, sessions)) / sessions
            alert = _rate_alert(
                "crash_free_rate", crash_free,
                settings.crash_free_rate_warning, settings.crash_free_rate_critical,
                f"Crash-free rate is {crash_free * 100:.1f}%",
                f"{crashed} of {sessions} sessions in the last {window_hours}h hit a crash "
                f"(threshold {settings.crash_free_rate_critical * 100:.0f}%).",
                app_id, sessions, settings.min_samples_for_rates, since_iso,
                higher_is_worse=False,
            )
            if alert:
                report.alerts.append(alert)

        logs = db.scalar("SELECT COUNT(*) FROM logs WHERE app_id = ? AND ts >= ?", (app_id, since_iso)) or 0
        errors = db.scalar(
            "SELECT COUNT(*) FROM logs WHERE app_id = ? AND ts >= ? AND level IN ('ERROR', 'FATAL')",
            (app_id, since_iso),
        ) or 0
        if logs:
            error_rate = errors / logs
            alert = _rate_alert(
                "error_rate", error_rate,
                settings.error_rate_warning, settings.error_rate_critical,
                f"Error log rate is {error_rate * 100:.1f}%",
                f"{errors} of {logs} log lines in the last {window_hours}h were ERROR or FATAL "
                f"(threshold {settings.error_rate_critical * 100:.0f}%).",
                app_id, logs, settings.min_samples_for_rates, since_iso,
                higher_is_worse=True,
            )
            if alert:
                report.alerts.append(alert)

        report.alerts.extend(_silence_alert(db, settings, app_id, moment))

    report.alerts.extend(_job_alerts(db, settings, moment))
    report.alerts.sort(key=lambda a: (SEVERITY_ORDER.get(a.severity, 9), a.rule))
    return report


def _silence_alert(db: Database, settings: Settings, app_id: str, moment: datetime) -> list[Alert]:
    """No telemetry at all is the failure mode nobody notices."""
    last = db.scalar(
        "SELECT MAX(received_at) FROM ("
        "  SELECT received_at FROM events WHERE app_id = ? "
        "  UNION ALL SELECT received_at FROM logs WHERE app_id = ? "
        "  UNION ALL SELECT received_at FROM crashes WHERE app_id = ?)",
        (app_id, app_id, app_id),
    )
    if last is None:
        return []  # a brand-new app has nothing to be silent about

    try:
        last_moment = datetime.fromisoformat(last.replace("Z", "+00:00"))
    except ValueError:
        return []

    quiet_minutes = (moment - last_moment).total_seconds() / 60
    if quiet_minutes < settings.silent_minutes:
        return []

    return [
        Alert(
            rule="no_telemetry",
            severity="warning",
            title=f"No telemetry for {quiet_minutes / 60:.1f}h",
            detail=(
                f"The last record from this app arrived {quiet_minutes / 60:.1f}h ago "
                f"(threshold {settings.silent_minutes / 60:.0f}h). The SDK may be failing to upload."
            ),
            app_id=app_id,
            value=round(quiet_minutes, 1),
            threshold=float(settings.silent_minutes),
            since=last,
        )
    ]


def _job_alerts(db: Database, settings: Settings, moment: datetime) -> list[Alert]:
    alerts: list[Alert] = []

    for job in ("rollup", "prune", "backup"):
        run = rollups.last_run(db, job)
        if run is None:
            continue
        if not run["ok"]:
            alerts.append(
                Alert(
                    rule=f"job_failed:{job}",
                    severity="critical",
                    title=f"The {job} job failed",
                    detail=run.get("detail") or "no detail recorded",
                    since=run["ran_at"],
                )
            )

    first, last = rollups.coverage(db, app_id="")
    if last is not None:
        last_day = datetime.fromisoformat(last).replace(tzinfo=timezone.utc)
        age_days = (moment.date() - last_day.date()).days
        if age_days > settings.rollup_stale_days:
            alerts.append(
                Alert(
                    rule="rollup_stale",
                    severity="warning",
                    title=f"Rollups are {age_days} days behind",
                    detail=(
                        f"The newest rolled-up day is {last} (threshold "
                        f"{settings.rollup_stale_days} days). Retention pruning is deferred while this is true."
                    ),
                    value=float(age_days),
                    threshold=float(settings.rollup_stale_days),
                    since=last,
                )
            )

    return alerts


# --------------------------------------------------------------------------- #
# Notification
# --------------------------------------------------------------------------- #

def post_webhook(url: str, report: AlertReport, opener=None) -> dict:
    """POST the report as JSON. Never raises — a failed alert must not break a job."""
    payload = json.dumps(report.to_dict()).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "powerbase-alerts/1"},
        method="POST",
    )
    send = opener or urllib.request.urlopen
    try:
        with send(request, timeout=10) as response:
            return {"ok": True, "status": getattr(response, "status", None)}
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}
