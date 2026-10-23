"""The audit trail.

Exports, key creation, rotation and revocation, and retention changes are
recorded here. The reason is practical rather than ceremonial: when someone asks
"who took a copy of this data, and when", an answer that does not exist is worse
than not having the feature.

**Append-only by construction.** Nothing in this codebase updates or deletes a
row in `audit_events`. That is the whole value of the table — an audit log the
application can edit is not an audit log.
"""

from __future__ import annotations

import json
from typing import Any

from .db import Database
from .migrations import utcnow_iso

#: Kept as a set of strings rather than an enum, so a new call site does not need
#: a migration. The ones that exist are listed here so they are greppable.
EXPORT_CREATED = "export.created"
EXPORT_DOWNLOADED = "export.downloaded"
EXPORT_DELETED = "export.deleted"
EXPORT_EXPIRED = "export.expired"
KEY_CREATED = "key.created"
KEY_ROTATED = "key.rotated"
KEY_REVOKED = "key.revoked"
RETENTION_CHANGED = "retention.changed"
PROJECT_CREATED = "project.created"
INCIDENT_OPENED = "incident.opened"
INCIDENT_ACKED = "incident.acknowledged"
INCIDENT_RESOLVED = "incident.resolved"
INCIDENT_REOPENED = "incident.reopened"
USER_INVITED = "user.invited"
USER_CREATED = "user.created"
USER_ROLE_CHANGED = "user.role_changed"
USER_DISABLED = "user.disabled"
USER_LOGIN = "user.login"
USER_LOGIN_FAILED = "user.login_failed"
USER_LOGOUT = "user.logout"
MEMBER_ADDED = "member.added"
MEMBER_REMOVED = "member.removed"

OUTCOMES = ("success", "denied", "failure")


def record_in(
    conn,
    event_type: str,
    *,
    outcome: str = "success",
    app_id: str | None = None,
    actor: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    request_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> int:
    """Write one audit row inside a caller's transaction.

    Used when the audit entry must land atomically with the thing it describes —
    an export that is recorded as created but does not exist would be worse than
    no record at all.
    """
    if outcome not in OUTCOMES:
        outcome = "failure"
    cursor = conn.execute(
        """
        INSERT INTO audit_events
            (event_time, app_id, actor, event_type, outcome,
             target_type, target_id, request_id, metadata)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            utcnow_iso(),
            app_id,
            actor,
            event_type,
            outcome,
            target_type,
            target_id,
            request_id,
            json.dumps(metadata or {}, separators=(",", ":"), default=str),
        ),
    )
    return int(cursor.lastrowid or 0)


def record(db: Database, event_type: str, **kwargs) -> int:
    """Write one audit row in its own transaction."""
    with db.write() as conn:
        return record_in(conn, event_type, **kwargs)


def list_events(
    db: Database,
    *,
    app_id: str | None = None,
    event_type: str | None = None,
    outcome: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict:
    """Newest first. Bounded, because this table only ever grows."""
    clauses = ["1"]
    params: list[Any] = []
    if app_id:
        clauses.append("app_id = ?")
        params.append(app_id)
    if event_type:
        clauses.append("event_type = ?")
        params.append(event_type)
    if outcome:
        clauses.append("outcome = ?")
        params.append(outcome)
    if since:
        clauses.append("event_time >= ?")
        params.append(since)
    if until:
        clauses.append("event_time < ?")
        params.append(until)

    where = " AND ".join(clauses)
    total = db.scalar(f"SELECT COUNT(*) FROM audit_events WHERE {where}", tuple(params)) or 0
    rows = db.query(
        f"""
        SELECT id, event_time, app_id, actor, event_type, outcome,
               target_type, target_id, request_id, metadata
          FROM audit_events
         WHERE {where}
         ORDER BY event_time DESC, id DESC
         LIMIT ? OFFSET ?
        """,
        tuple(params) + (max(1, min(int(limit), 500)), max(0, int(offset))),
    )
    return {"total": total, "count": len(rows), "offset": offset, "items": [_row(r) for r in rows]}


def _row(row) -> dict:
    out = dict(row)
    raw = out.get("metadata")
    if isinstance(raw, str):
        try:
            out["metadata"] = json.loads(raw)
        except json.JSONDecodeError:
            out["metadata"] = {}
    return out
