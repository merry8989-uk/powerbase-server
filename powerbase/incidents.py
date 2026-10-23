"""Incidents.

A rule firing is not an event, it is a **state**. Evaluating every few minutes
and firing the webhook each time meant one broken thing produced a dozen
identical notifications, and an operator learns to ignore those. An incident is
opened once, reminded on a cooldown, and resolved when the rule stops firing.

The three things that make it usable:

* **One open incident per (project, rule).** Enforced by a partial unique index,
  not by a check-then-insert race.
* **A cooldown.** A continuing problem reminds you on a schedule you chose
  rather than on the scheduler's.
* **Acknowledgement is respected.** An acked incident stays quiet until it
  resolves, because the point of acking is to stop being told.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from . import audit, notify
from .config import Settings
from .db import Database
from .migrations import utcnow_iso

OPEN_STATUSES = ("firing", "acknowledged")


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def dedup_key(app_id: str | None, rule: str) -> str:
    """One incident per project per rule. Project-less alerts share a namespace."""
    return f"{app_id or '-'}:{rule}"


def get_incident(db: Database, incident_id: str) -> dict | None:
    row = db.one("SELECT * FROM alert_incidents WHERE id = ?", (incident_id,))
    return _row(row) if row is not None else None


def list_incidents(
    db: Database,
    *,
    app_id: str | None = None,
    status: str | None = None,
    severity: str | None = None,
    limit: int = 100,
) -> list[dict]:
    clauses = ["1"]
    params: list[Any] = []
    if app_id:
        clauses.append("app_id = ?")
        params.append(app_id)
    if status:
        clauses.append("status = ?")
        params.append(status)
    if severity:
        clauses.append("severity = ?")
        params.append(severity)

    rows = db.query(
        f"""
        SELECT * FROM alert_incidents
         WHERE {' AND '.join(clauses)}
         ORDER BY
           CASE status WHEN 'firing' THEN 0 WHEN 'acknowledged' THEN 1 ELSE 2 END,
           CASE severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END,
           last_seen_at DESC
         LIMIT ?
        """,
        tuple(params) + (max(1, min(int(limit), 500)),),
    )
    return [_row(row) for row in rows]


def _row(row) -> dict:
    out = dict(row)
    raw = out.get("context")
    if isinstance(raw, str):
        try:
            out["context"] = json.loads(raw)
        except json.JSONDecodeError:
            out["context"] = {}
    return out


def summary(db: Database) -> dict:
    """What the banner needs: is anything on fire, and how badly."""
    rows = db.query(
        """
        SELECT status, severity, COUNT(*) AS n FROM alert_incidents
         WHERE status IN ('firing', 'acknowledged')
         GROUP BY status, severity
        """
    )
    by_status: dict[str, int] = {"firing": 0, "acknowledged": 0}
    by_severity: dict[str, int] = {"critical": 0, "warning": 0, "info": 0}
    for row in rows:
        by_status[row["status"]] = by_status.get(row["status"], 0) + row["n"]
        by_severity[row["severity"]] = by_severity.get(row["severity"], 0) + row["n"]

    worst = None
    for candidate in ("critical", "warning", "info"):
        if by_severity.get(candidate):
            worst = candidate
            break
    return {
        "open": by_status["firing"] + by_status["acknowledged"],
        "firing": by_status["firing"],
        "acknowledged": by_status["acknowledged"],
        "by_severity": by_severity,
        "worst": worst,
    }


# --------------------------------------------------------------------------- #
# The lifecycle
# --------------------------------------------------------------------------- #
def apply_report(
    db: Database,
    settings: Settings,
    report,
    *,
    now: datetime | None = None,
) -> dict:
    """Fold an evaluation into the incident table.

    Opens, reminds and resolves. Returns what it did, so the caller can log it
    rather than guess.
    """
    result = {"opened": 0, "updated": 0, "resolved": 0, "queued": 0, "skipped": False}

    # Alerts being switched off is not evidence that anything recovered, so
    # nothing is resolved on this path.
    if not settings.alerts_enabled:
        result["skipped"] = True
        return result

    moment = now or datetime.now(timezone.utc)
    stamp = _iso(moment)
    cooldown = timedelta(minutes=settings.alert_cooldown_minutes)
    seen: set[str] = set()

    for alert in report.alerts:
        key = dedup_key(alert.app_id, alert.rule)
        seen.add(key)
        existing = db.one(
            "SELECT * FROM alert_incidents WHERE dedup_key = ? AND status IN ('firing','acknowledged')",
            (key,),
        )

        if existing is None:
            incident_id = uuid.uuid4().hex
            with db.write() as conn:
                conn.execute(
                    """
                    INSERT INTO alert_incidents
                        (id, dedup_key, app_id, rule, severity, title, detail, context,
                         status, occurrences, first_seen_at, last_seen_at,
                         last_notified_at, notify_count)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'firing', 1, ?, ?, ?, 0)
                    """,
                    (
                        incident_id, key, alert.app_id, alert.rule, alert.severity,
                        alert.title, alert.detail,
                        json.dumps(
                            {"value": alert.value, "threshold": alert.threshold, "since": alert.since},
                            default=str,
                        ),
                        stamp, stamp, stamp,
                    ),
                )
                audit.record_in(
                    conn, audit.INCIDENT_OPENED, app_id=alert.app_id, actor="alerts",
                    target_type="incident", target_id=incident_id,
                    metadata={"rule": alert.rule, "severity": alert.severity},
                )
            queued = notify.enqueue(
                db, incident_id=incident_id, app_id=alert.app_id or "",
                severity=alert.severity, event="opened",
            )
            _mark_notified(db, incident_id, stamp, len(queued) > 0)
            result["opened"] += 1
            result["queued"] += len(queued)
            continue

        # It was already open. Refresh what it says, and count the sighting.
        escalated = notify.SEVERITY_RANK.get(alert.severity, 2) < notify.SEVERITY_RANK.get(
            existing["severity"], 2
        )
        with db.write() as conn:
            conn.execute(
                """
                UPDATE alert_incidents
                   SET last_seen_at = ?, occurrences = occurrences + 1,
                       severity = ?, title = ?, detail = ?, context = ?
                 WHERE id = ?
                """,
                (
                    stamp, alert.severity, alert.title, alert.detail,
                    json.dumps(
                        {"value": alert.value, "threshold": alert.threshold, "since": alert.since},
                        default=str,
                    ),
                    existing["id"],
                ),
            )
        result["updated"] += 1

        # Acknowledged means "I know". Stay quiet until it resolves.
        if existing["status"] == "acknowledged":
            continue

        last_notified = _parse(existing["last_notified_at"])
        due = last_notified is None or (moment - last_notified) >= cooldown
        if due or escalated:
            queued = notify.enqueue(
                db, incident_id=existing["id"], app_id=alert.app_id or "",
                severity=alert.severity, event="reminder",
            )
            _mark_notified(db, existing["id"], stamp, len(queued) > 0)
            result["queued"] += len(queued)

    # Anything open whose rule did not fire this time has recovered.
    for row in db.query(
        "SELECT * FROM alert_incidents WHERE status IN ('firing','acknowledged')"
    ):
        if row["dedup_key"] in seen:
            continue
        with db.write() as conn:
            conn.execute(
                """
                UPDATE alert_incidents
                   SET status = 'resolved', resolved_at = ?, resolved_by = 'alerts'
                 WHERE id = ?
                """,
                (stamp, row["id"]),
            )
            audit.record_in(
                conn, audit.INCIDENT_RESOLVED, app_id=row["app_id"], actor="alerts",
                target_type="incident", target_id=row["id"],
                metadata={"rule": row["rule"], "occurrences": row["occurrences"]},
            )
        queued = notify.enqueue(
            db, incident_id=row["id"], app_id=row["app_id"] or "",
            severity=row["severity"], event="resolved",
        )
        result["queued"] += len(queued)
        result["resolved"] += 1

    return result


def _mark_notified(db: Database, incident_id: str, stamp: str, sent: bool) -> None:
    if not sent:
        return
    with db.write() as conn:
        conn.execute(
            """
            UPDATE alert_incidents
               SET last_notified_at = ?, notify_count = notify_count + 1
             WHERE id = ?
            """,
            (stamp, incident_id),
        )


def ack(db: Database, incident_id: str, actor: str, now: datetime | None = None) -> dict | None:
    """Stop being told about this one until it resolves."""
    incident = get_incident(db, incident_id)
    if incident is None:
        return None
    if incident["status"] != "firing":
        return incident
    with db.write() as conn:
        conn.execute(
            "UPDATE alert_incidents SET status = 'acknowledged', acked_by = ?, acked_at = ? "
            "WHERE id = ?",
            (actor, _iso(now or datetime.now(timezone.utc)), incident_id),
        )
        audit.record_in(
            conn, audit.INCIDENT_ACKED, app_id=incident["app_id"], actor=actor,
            target_type="incident", target_id=incident_id,
            metadata={"rule": incident["rule"]},
        )
    return get_incident(db, incident_id)


def resolve(db: Database, incident_id: str, actor: str, now: datetime | None = None) -> dict | None:
    """Close it by hand, without waiting for the rule to stop firing."""
    incident = get_incident(db, incident_id)
    if incident is None:
        return None
    if incident["status"] == "resolved":
        return incident
    with db.write() as conn:
        conn.execute(
            "UPDATE alert_incidents SET status = 'resolved', resolved_at = ?, resolved_by = ? "
            "WHERE id = ?",
            (_iso(now or datetime.now(timezone.utc)), actor, incident_id),
        )
        audit.record_in(
            conn, audit.INCIDENT_RESOLVED, app_id=incident["app_id"], actor=actor,
            target_type="incident", target_id=incident_id,
            metadata={"rule": incident["rule"], "by_hand": True},
        )
    return get_incident(db, incident_id)


def reopen(db: Database, incident_id: str, actor: str) -> dict | None:
    """Undo a resolve that was wrong."""
    incident = get_incident(db, incident_id)
    if incident is None or incident["status"] != "resolved":
        return incident
    with db.write() as conn:
        conn.execute(
            """
            UPDATE alert_incidents
               SET status = 'firing', resolved_at = NULL, resolved_by = NULL,
                   acked_by = NULL, acked_at = NULL, last_seen_at = ?
             WHERE id = ?
            """,
            (utcnow_iso(), incident_id),
        )
        audit.record_in(
            conn, audit.INCIDENT_REOPENED, app_id=incident["app_id"], actor=actor,
            target_type="incident", target_id=incident_id,
        )
    return get_incident(db, incident_id)
