"""Getting a notification out, and being honest when it did not go.

The old behaviour was one `POST` per evaluation: if it failed, the alert was
gone. That is the failure mode this module exists to remove. A notification is
now a row in `alert_deliveries` that is retried with backoff and, after the last
attempt, marked `dead` — visibly given up on, not silently lost.

Two channels, no dependencies:

* **webhook** — `urllib` with a timeout.
* **email** — `smtplib` from the standard library. No third-party client, and
  no dependency to keep patched.
"""

from __future__ import annotations

import json
import smtplib
import ssl
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from typing import Any, Callable

from .config import Settings
from .db import Database
from .migrations import utcnow_iso

#: Lower is more severe. Used to decide whether a channel wants this alert.
SEVERITY_RANK = {"critical": 0, "warning": 1, "info": 2}

MAX_ATTEMPTS = 5
BASE_BACKOFF_SECONDS = 60
MAX_BACKOFF_SECONDS = 3600


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# Channels
# --------------------------------------------------------------------------- #
def add_channel(
    db: Database,
    *,
    kind: str,
    target: str,
    app_id: str | None = None,
    min_severity: str = "info",
    enabled: bool = True,
) -> dict:
    """Register where notifications go. Idempotent on (app, kind, target)."""
    if kind not in ("webhook", "email"):
        raise ValueError("kind must be 'webhook' or 'email'")
    if min_severity not in SEVERITY_RANK:
        raise ValueError("min_severity must be info, warning or critical")
    if not target or not target.strip():
        raise ValueError("target is required")
    if app_id and db.one("SELECT id FROM apps WHERE id = ?", (app_id,)) is None:
        raise ValueError(f"unknown project: {app_id}")

    import uuid

    channel_id = uuid.uuid4().hex
    with db.write() as conn:
        existing = conn.execute(
            "SELECT id FROM alert_channels WHERE app_id IS ? AND kind = ? AND target = ?",
            (app_id, kind, target.strip()),
        ).fetchone()
        if existing is not None:
            conn.execute(
                "UPDATE alert_channels SET enabled = ?, min_severity = ? WHERE id = ?",
                (1 if enabled else 0, min_severity, existing["id"]),
            )
            channel_id = existing["id"]
        else:
            conn.execute(
                """
                INSERT INTO alert_channels
                    (id, app_id, kind, target, enabled, min_severity, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    channel_id, app_id, kind, target.strip(),
                    1 if enabled else 0, min_severity, utcnow_iso(),
                ),
            )
    return get_channel(db, channel_id)  # type: ignore[return-value]


def get_channel(db: Database, channel_id: str) -> dict | None:
    row = db.one("SELECT * FROM alert_channels WHERE id = ?", (channel_id,))
    return dict(row) if row is not None else None


def list_channels(db: Database, app_id: str | None = None) -> list[dict]:
    if app_id:
        rows = db.query(
            "SELECT * FROM alert_channels WHERE app_id IS NULL OR app_id = ? ORDER BY created_at",
            (app_id,),
        )
    else:
        rows = db.query("SELECT * FROM alert_channels ORDER BY created_at")
    return [dict(row) for row in rows]


def delete_channel(db: Database, channel_id: str) -> bool:
    with db.write() as conn:
        cursor = conn.execute("DELETE FROM alert_channels WHERE id = ?", (channel_id,))
    return cursor.rowcount > 0


def ensure_legacy_webhook(db: Database, settings: Settings) -> dict | None:
    """Register `POWERBASE_ALERT_WEBHOOK_URL` as an instance-wide channel.

    An install that predates channels keeps working without being reconfigured.
    Idempotent, so it is safe to call on every pass.
    """
    url = (settings.alert_webhook_url or "").strip()
    if not url:
        return None
    existing = db.one(
        "SELECT id FROM alert_channels WHERE app_id IS NULL AND kind = 'webhook' AND target = ?",
        (url,),
    )
    if existing is not None:
        return get_channel(db, existing["id"])
    try:
        return add_channel(db, kind="webhook", target=url, app_id=None, min_severity="info")
    except ValueError:
        return None


def channels_for(db: Database, app_id: str, severity: str) -> list[dict]:
    """Enabled channels that want an alert of this severity for this project."""
    wanted = SEVERITY_RANK.get(severity, 2)
    rows = db.query(
        "SELECT * FROM alert_channels WHERE enabled = 1 AND (app_id IS NULL OR app_id = ?)",
        (app_id,),
    )
    return [
        dict(row)
        for row in rows
        if wanted <= SEVERITY_RANK.get(row["min_severity"], 2)
    ]


# --------------------------------------------------------------------------- #
# Transports
# --------------------------------------------------------------------------- #
def post_webhook(url: str, payload: dict, timeout: float = 10.0, opener=None) -> dict:
    """One POST. Raises on failure so the caller can schedule a retry."""
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "powerbase-alerts/1"},
        method="POST",
    )
    send = opener or urllib.request.urlopen
    try:
        with send(request, timeout=timeout) as response:
            return {"ok": 200 <= response.status < 300, "status": response.status}
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"webhook returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise RuntimeError(f"webhook failed: {exc}") from exc


def send_email(
    settings: Settings,
    to: str,
    subject: str,
    body: str,
    timeout: float = 15.0,
    smtp_factory: Callable[..., Any] | None = None,
) -> dict:
    """One email, over SMTP from the standard library."""
    if not settings.smtp_host:
        raise RuntimeError("no SMTP host configured (POWERBASE_SMTP_HOST)")

    message = EmailMessage()
    message["From"] = settings.smtp_from or settings.smtp_user or "powerbase@localhost"
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)

    factory = smtp_factory or smtplib.SMTP
    try:
        with factory(settings.smtp_host, settings.smtp_port, timeout=timeout) as client:
            if settings.smtp_starttls:
                client.starttls(context=ssl.create_default_context())
            if settings.smtp_user:
                client.login(settings.smtp_user, settings.smtp_password or "")
            client.send_message(message)
    except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
        raise RuntimeError(f"email failed: {exc}") from exc
    return {"ok": True, "to": to}


# --------------------------------------------------------------------------- #
# The queue
# --------------------------------------------------------------------------- #
def enqueue(
    db: Database,
    *,
    incident_id: str,
    app_id: str,
    severity: str,
    event: str,
) -> list[int]:
    """Queue a delivery per matching channel. Returns the delivery ids."""
    created: list[int] = []
    stamp = utcnow_iso()
    for channel in channels_for(db, app_id, severity):
        with db.write() as conn:
            cursor = conn.execute(
                """
                INSERT INTO alert_deliveries
                    (incident_id, channel_id, kind, target, event, status,
                     next_attempt_at, created_at)
                VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (incident_id, channel["id"], channel["kind"], channel["target"], event, stamp, stamp),
            )
            created.append(int(cursor.lastrowid or 0))
    return created


def _backoff_seconds(attempts: int) -> int:
    return min(BASE_BACKOFF_SECONDS * 2 ** max(attempts - 1, 0), MAX_BACKOFF_SECONDS)


def flush(
    db: Database,
    settings: Settings,
    *,
    now: datetime | None = None,
    limit: int = 25,
    webhook_opener=None,
    smtp_factory=None,
) -> dict:
    """Attempt every due delivery, once.

    Called from the scheduler each pass and from the nightly run. Deliberately
    bounded: a stuck queue must not turn into an unbounded pass.
    """
    moment = now or datetime.now(timezone.utc)
    stamp = _iso(moment)

    due = db.query(
        """
        SELECT * FROM alert_deliveries
         WHERE status IN ('pending', 'failed')
           AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
         ORDER BY next_attempt_at, id
         LIMIT ?
        """,
        (stamp, max(1, min(int(limit), 200))),
    )

    sent = failed = dead = 0
    for delivery in due:
        attempts = int(delivery["attempts"]) + 1
        try:
            if delivery["kind"] == "webhook":
                incident = db.one(
                    "SELECT * FROM alert_incidents WHERE id = ?", (delivery["incident_id"],)
                )
                post_webhook(
                    delivery["target"],
                    {
                        "event": delivery["event"],
                        "incident": dict(incident) if incident else None,
                    },
                    opener=webhook_opener,
                )
            else:
                incident = db.one(
                    "SELECT * FROM alert_incidents WHERE id = ?", (delivery["incident_id"],)
                )
                title = incident["title"] if incident else "Powerbase alert"
                severity = incident["severity"] if incident else "info"
                body = (
                    f"{delivery['event'].upper()}: [{severity}] {title}\n\n"
                    f"{incident['detail'] if incident else ''}\n\n"
                    f"Project: {incident['app_id'] if incident else 'unknown'}\n"
                    f"First seen: {incident['first_seen_at'] if incident else ''}\n"
                )
                send_email(
                    settings,
                    delivery["target"],
                    f"[Powerbase {severity}] {title}",
                    body,
                    smtp_factory=smtp_factory,
                )
        except Exception as exc:  # noqa: BLE001 - any failure means "retry or give up"
            if attempts >= MAX_ATTEMPTS:
                with db.write() as conn:
                    conn.execute(
                        """
                        UPDATE alert_deliveries
                           SET status = 'dead', attempts = ?, last_error = ?
                         WHERE id = ?
                        """,
                        (attempts, str(exc)[:500], delivery["id"]),
                    )
                dead += 1
            else:
                retry_at = _iso(moment + timedelta(seconds=_backoff_seconds(attempts)))
                with db.write() as conn:
                    conn.execute(
                        """
                        UPDATE alert_deliveries
                           SET status = 'failed', attempts = ?, next_attempt_at = ?,
                               last_error = ?
                         WHERE id = ?
                        """,
                        (attempts, retry_at, str(exc)[:500], delivery["id"]),
                    )
                failed += 1
        else:
            with db.write() as conn:
                conn.execute(
                    """
                    UPDATE alert_deliveries
                       SET status = 'sent', attempts = ?, delivered_at = ?,
                           last_error = NULL
                     WHERE id = ?
                    """,
                    (attempts, stamp, delivery["id"]),
                )
            sent += 1

    return {"due": len(due), "sent": sent, "failed": failed, "dead": dead}


def retry_dead(db: Database, delivery_id: int) -> bool:
    """Put a dead delivery back in the queue. The operator's override."""
    with db.write() as conn:
        cursor = conn.execute(
            """
            UPDATE alert_deliveries
               SET status = 'pending', attempts = 0, next_attempt_at = ?, last_error = NULL
             WHERE id = ? AND status = 'dead'
            """,
            (utcnow_iso(), delivery_id),
        )
    return cursor.rowcount > 0


def list_deliveries(
    db: Database, incident_id: str | None = None, status: str | None = None, limit: int = 100
) -> list[dict]:
    clauses = ["1"]
    params: list[Any] = []
    if incident_id:
        clauses.append("incident_id = ?")
        params.append(incident_id)
    if status:
        clauses.append("status = ?")
        params.append(status)
    rows = db.query(
        f"""
        SELECT * FROM alert_deliveries
         WHERE {' AND '.join(clauses)}
         ORDER BY id DESC LIMIT ?
        """,
        tuple(params) + (max(1, min(int(limit), 500)),),
    )
    return [dict(row) for row in rows]


def queue_health(db: Database) -> dict:
    """What an operator needs to see: is anything stuck?"""
    rows = db.query("SELECT status, COUNT(*) AS n FROM alert_deliveries GROUP BY status")
    counts = {row["status"]: row["n"] for row in rows}
    dead = db.query(
        "SELECT id, target, attempts, last_error FROM alert_deliveries WHERE status = 'dead' "
        "ORDER BY id DESC LIMIT 10"
    )
    return {
        "counts": counts,
        "dead": [dict(row) for row in dead],
        "channels": len(list_channels(db)),
    }
