"""Transactional batch writers.

Every public function here takes the whole batch and either commits it entirely
or not at all. The idempotency ledger is written inside the same transaction as
the rows, so a retry can never double-write.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3

from .db import Database
from .migrations import utcnow_iso
from .models import (
    CrashesBatch,
    EventsBatch,
    LogEventsBatch,
    LogsBatch,
    SessionEndIn,
    SessionStartIn,
    DeviceInfo,
    to_iso,
)
from . import projection
from .attributes import sanitize_attributes
from .redaction import redact_text, redact_value

_HEX = re.compile(r"0x[0-9a-fA-F]+")
_NUM = re.compile(r"\d+")
_WS = re.compile(r"\s+")


def _json(value) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, default=str)


def fingerprint(kind: str, message: str | None, stacktrace: str | None) -> str:
    """Group identical crashes: strip addresses, numbers and whitespace, hash."""
    base = f"{stacktrace or ''}\n{message or ''}"
    base = _HEX.sub("0xADDR", base)
    base = _NUM.sub("N", base)
    base = _WS.sub(" ", base).strip()
    return hashlib.sha256(f"{kind}:{base}".encode("utf-8")).hexdigest()[:32]


# ------------------------------------------------------------ context rows
def ensure_device(
    conn: sqlite3.Connection,
    app_id: str,
    device_id: str,
    ts: str,
    info: DeviceInfo | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO devices
            (id, app_id, model, manufacturer, os_version, app_version, sdk_version, locale, first_seen, last_seen)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            last_seen    = excluded.last_seen,
            model        = COALESCE(excluded.model, devices.model),
            manufacturer = COALESCE(excluded.manufacturer, devices.manufacturer),
            os_version   = COALESCE(excluded.os_version, devices.os_version),
            app_version  = COALESCE(excluded.app_version, devices.app_version),
            sdk_version  = COALESCE(excluded.sdk_version, devices.sdk_version),
            locale       = COALESCE(excluded.locale, devices.locale)
        """,
        (
            device_id,
            app_id,
            info.model if info else None,
            info.manufacturer if info else None,
            info.os_version if info else None,
            info.app_version if info else None,
            info.sdk_version if info else None,
            info.locale if info else None,
            ts,
            ts,
        ),
    )


def ensure_session(
    conn: sqlite3.Connection,
    app_id: str,
    device_id: str,
    session_id: str | None,
    ts: str,
) -> None:
    """Create a stub session if an event arrives before its session/start."""
    if not session_id:
        return
    conn.execute(
        "INSERT OR IGNORE INTO sessions (id, app_id, device_id, started_at) VALUES (?, ?, ?, ?)",
        (session_id, app_id, device_id, ts),
    )


def _begin_batch(
    conn: sqlite3.Connection, batch_id: str, app_id: str, kind: str, count: int
) -> bool:
    """Record the batch. Returns False when it was already seen (a retry)."""
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO ingest_batches (batch_id, app_id, kind, received_at, event_count)
        VALUES (?, ?, ?, ?, ?)
        """,
        (batch_id, app_id, kind, utcnow_iso(), count),
    )
    return cur.rowcount == 1


# ------------------------------------------------------------------ writes
def _insert_projected(conn, app_id: str, values: dict, redact: bool) -> None:
    """Write the diagnostics-log projection of an analytics row.

    Called inside the source row's own transaction, so the two cannot diverge:
    either both land or neither does. See `powerbase/projection.py` for why this
    exists and what it costs.

    A plain INSERT, not `OR IGNORE`. The batch ledger already refuses a replayed
    batch, so a duplicate `event_id` here would mean something is wrong, and a
    collision that fails loudly is worth more than one that is swallowed.
    """
    conn.execute(
        """
        INSERT INTO log_events
            (event_id, app_id, event_time, received_at, source, severity, event_type,
             service_name, environment, app_version, platform_version,
             request_id, trace_id, span_id, route_template, http_method,
             status_code, duration_ms, error_code, error_fingerprint, message,
             attributes, redaction_version, sdk_version, category,
             source_type, source_id)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            values["event_id"], app_id, values["event_time"], utcnow_iso(),
            values["source"],
            values["severity"], values["event_type"], values.get("service_name"),
            values.get("environment"), values.get("app_version"),
            values.get("platform_version"), values.get("request_id"),
            values.get("trace_id"), values.get("span_id"),
            values.get("route_template"), values.get("http_method"),
            values.get("status_code"), values.get("duration_ms"),
            values.get("error_code"), values.get("error_fingerprint"),
            values.get("message"), _json(values.get("attributes") or {}),
            REDACTION_VERSION if redact else 0, values.get("sdk_version"),
            values.get("category"), values.get("source_type"),
            values.get("source_id"),
        ),
    )


def write_events(db: Database, app_id: str, batch: EventsBatch, redact: bool) -> dict:
    with db.write() as conn:
        if not _begin_batch(conn, batch.batch_id, app_id, "events", len(batch.events)):
            return {"duplicate": True, "accepted": 0}
        for event in batch.events:
            ts = to_iso(event.ts)
            ensure_device(conn, app_id, event.device_id, ts)
            ensure_session(conn, app_id, event.device_id, event.session_id, ts)
            props = redact_value(event.properties) if redact else event.properties
            cur = conn.execute(
                """
                INSERT INTO events
                    (app_id, device_id, session_id, ts, received_at, name, category, screen, properties, sdk_version)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    app_id,
                    event.device_id,
                    event.session_id,
                    ts,
                    utcnow_iso(),
                    event.name,
                    event.category,
                    event.screen,
                    _json(props),
                    event.sdk_version or batch.sdk_version,
                ),
            )
            # The explorer should see this too. A product event is not the same
            # thing as an HTTP request, but it is the same kind of thing.
            _insert_projected(
                conn, app_id,
                projection.from_event(
                    event, row_id=cur.lastrowid, app_id=app_id, ts=ts, properties=props
                ),
                redact,
            )
        return {"duplicate": False, "accepted": len(batch.events)}


def write_logs(db: Database, app_id: str, batch: LogsBatch, redact: bool) -> dict:
    with db.write() as conn:
        if not _begin_batch(conn, batch.batch_id, app_id, "logs", len(batch.logs)):
            return {"duplicate": True, "accepted": 0}
        for entry in batch.logs:
            ts = to_iso(entry.ts)
            ensure_device(conn, app_id, entry.device_id, ts)
            ensure_session(conn, app_id, entry.device_id, entry.session_id, ts)
            message = redact_text(entry.message) if redact else entry.message
            stack = redact_text(entry.stacktrace) if (redact and entry.stacktrace) else entry.stacktrace
            context = redact_value(entry.context) if redact else entry.context
            cur = conn.execute(
                """
                INSERT INTO logs
                    (app_id, device_id, session_id, ts, received_at, level, tag, message, stacktrace, context, sdk_version)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    app_id,
                    entry.device_id,
                    entry.session_id,
                    ts,
                    utcnow_iso(),
                    entry.level,
                    entry.tag,
                    message,
                    stack,
                    _json(context),
                    entry.sdk_version or batch.sdk_version,
                ),
            )
            _insert_projected(
                conn, app_id,
                projection.from_log(
                    entry, row_id=cur.lastrowid, app_id=app_id, ts=ts,
                    message=message, context=context,
                ),
                redact,
            )
        return {"duplicate": False, "accepted": len(batch.logs)}


def write_crashes(db: Database, app_id: str, batch: CrashesBatch, redact: bool) -> dict:
    with db.write() as conn:
        if not _begin_batch(conn, batch.batch_id, app_id, "crashes", len(batch.crashes)):
            return {"duplicate": True, "accepted": 0}
        for crash in batch.crashes:
            ts = to_iso(crash.ts)
            ensure_device(conn, app_id, crash.device_id, ts)
            ensure_session(conn, app_id, crash.device_id, crash.session_id, ts)
            message = redact_text(crash.message) if (redact and crash.message) else crash.message
            stack = redact_text(crash.stacktrace) if (redact and crash.stacktrace) else crash.stacktrace
            crash_fingerprint = fingerprint(crash.kind, message, stack)
            cur = conn.execute(
                """
                INSERT INTO crashes
                    (app_id, device_id, session_id, ts, received_at, kind, message, stacktrace, fingerprint, handled, app_version, os_version)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    app_id,
                    crash.device_id,
                    crash.session_id,
                    ts,
                    utcnow_iso(),
                    crash.kind,
                    message,
                    stack,
                    crash_fingerprint,
                    1 if crash.handled else 0,
                    crash.app_version,
                    crash.os_version,
                ),
            )
            # A crash is the diagnostic event. Before this it was the one thing
            # the Logs Explorer could not show.
            _insert_projected(
                conn, app_id,
                projection.from_crash(
                    crash, row_id=cur.lastrowid, app_id=app_id, ts=ts,
                    message=message, stacktrace=stack, fingerprint=crash_fingerprint,
                ),
                redact,
            )
        return {"duplicate": False, "accepted": len(batch.crashes)}


# ---------------------------------------------------------------- sessions
def start_session(db: Database, app_id: str, payload: SessionStartIn) -> dict:
    ts = to_iso(payload.started_at)
    with db.write() as conn:
        ensure_device(conn, app_id, payload.device_id, ts, payload.device)
        conn.execute(
            """
            INSERT INTO sessions
                (id, app_id, device_id, user_ref, started_at, app_version, os_version, country)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                started_at  = excluded.started_at,
                app_version = COALESCE(excluded.app_version, sessions.app_version),
                os_version  = COALESCE(excluded.os_version, sessions.os_version),
                country     = COALESCE(excluded.country, sessions.country),
                user_ref    = COALESCE(excluded.user_ref, sessions.user_ref)
            """,
            (
                payload.session_id,
                app_id,
                payload.device_id,
                payload.user_ref,
                ts,
                payload.app_version,
                payload.os_version,
                payload.country,
            ),
        )
        return {"session_id": payload.session_id}


def end_session(db: Database, app_id: str, payload: SessionEndIn) -> dict:
    with db.write() as conn:
        cur = conn.execute(
            """
            UPDATE sessions
               SET ended_at = ?, duration_ms = ?, end_reason = ?
             WHERE id = ? AND app_id = ?
            """,
            (to_iso(payload.ended_at), payload.duration_ms, payload.end_reason, payload.session_id, app_id),
        )
        return {"updated": cur.rowcount == 1}


#: Written into every log_events row so a stored row can always be traced to the
#: redaction rules that produced it. Bump when the rules change.
REDACTION_VERSION = 1


def write_log_events(
    db: Database,
    app_id: str,
    batch: LogEventsBatch,
    redact: bool,
    max_attributes: int = 64,
) -> dict:
    """Store an observability batch.

    Two layers of idempotency: the batch ledger (a retried batch is a no-op) and
    a per-event unique key (a batch retried with one extra event stores only the
    new one). Dropped attribute keys are counted and reported back, so an
    integrator can see that a field naming a credential was refused.
    """
    with db.write() as conn:
        if not _begin_batch(conn, batch.batch_id, app_id, "log_events", len(batch.events)):
            return {"duplicate": True, "accepted": 0, "skipped_duplicates": 0, "dropped_attributes": 0}

        accepted = 0
        skipped = 0
        dropped_total = 0
        received_at = utcnow_iso()

        for event in batch.events:
            attributes, dropped = sanitize_attributes(
                event.attributes, max_keys=max_attributes
            )
            dropped_total += dropped
            if redact:
                attributes = redact_value(attributes)
                message = redact_text(event.message) if event.message else None
            else:
                message = event.message

            cur = conn.execute(
                """
                INSERT OR IGNORE INTO log_events
                    (event_id, app_id, event_time, received_at, source, severity,
                     event_type, service_name, environment, app_version, release_id,
                     request_id, trace_id, span_id, route_template, http_method,
                     status_code, duration_ms, error_code, error_fingerprint,
                     message, attributes, redaction_version, sdk_version,
                     category, source_type, source_id, platform_version)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    app_id,
                    to_iso(event.ts),
                    received_at,
                    event.source,
                    event.severity,
                    event.event_type,
                    event.service_name,
                    event.environment,
                    event.app_version,
                    event.release_id,
                    event.request_id,
                    event.trace_id,
                    event.span_id,
                    event.route_template,
                    event.http_method,
                    event.status_code,
                    event.duration_ms,
                    event.error_code,
                    event.error_fingerprint,
                    message,
                    _json(attributes),
                    REDACTION_VERSION if redact else 0,
                    event.sdk_version or batch.sdk_version,
                    event.category,
                    event.source_type,
                    event.source_id,
                    event.platform_version,
                ),
            )
            if cur.rowcount == 1:
                accepted += 1
            else:
                skipped += 1

        return {
            "duplicate": False,
            "accepted": accepted,
            "skipped_duplicates": skipped,
            "dropped_attributes": dropped_total,
        }
