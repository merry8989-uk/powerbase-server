"""Log drains: forward what was ingested to somewhere else.

The reason to have this at all is that a Powerbase instance is rarely the only
place logs need to be. A team with an existing SIEM, data lake or long-term
archive should not have to choose between Powerbase and it.

**At-least-once, and it says so.** The cursor advances only after the target
accepts the batch, so a crash between the send and the cursor write re-sends.
Exactly-once across a network is not achievable without cooperation from the
target, and pretending otherwise would be worse than saying it plainly.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from .config import Settings
from .db import Database
from .exports import EXPORT_COLUMNS
from .migrations import utcnow_iso

SEVERITY_RANK = {"TRACE": 0, "DEBUG": 1, "INFO": 2, "WARN": 3, "ERROR": 4, "FATAL": 5}
DRAIN_COLUMNS = EXPORT_COLUMNS

MAX_BATCH = 5000


def _at_or_above(severity: str) -> list[str]:
    floor = SEVERITY_RANK.get(severity, 0)
    return [name for name, rank in SEVERITY_RANK.items() if rank >= floor]


def create_drain(
    db: Database,
    *,
    name: str,
    target: str,
    app_id: str | None = None,
    min_severity: str = "DEBUG",
    batch_size: int = 500,
    enabled: bool = True,
) -> dict:
    if not name.strip():
        raise ValueError("a drain needs a name")
    if not target.strip().startswith(("http://", "https://")):
        raise ValueError("target must be an http(s) URL")
    if min_severity not in SEVERITY_RANK:
        raise ValueError(f"min_severity must be one of {', '.join(SEVERITY_RANK)}")
    if app_id and db.one("SELECT id FROM apps WHERE id = ?", (app_id,)) is None:
        raise ValueError(f"unknown project: {app_id}")

    drain_id = uuid.uuid4().hex
    with db.write() as conn:
        conn.execute(
            """
            INSERT INTO log_drains
                (id, app_id, name, kind, target, enabled, batch_size, min_severity, created_at)
            VALUES (?, ?, ?, 'http', ?, ?, ?, ?, ?)
            """,
            (
                drain_id, app_id, name.strip(), target.strip(),
                1 if enabled else 0, max(1, min(int(batch_size), MAX_BATCH)),
                min_severity, utcnow_iso(),
            ),
        )
    return get_drain(db, drain_id)  # type: ignore[return-value]


def get_drain(db: Database, drain_id: str) -> dict | None:
    row = db.one("SELECT * FROM log_drains WHERE id = ?", (drain_id,))
    return dict(row) if row is not None else None


def list_drains(db: Database, app_id: str | None = None) -> list[dict]:
    if app_id:
        rows = db.query(
            "SELECT * FROM log_drains WHERE app_id IS NULL OR app_id = ? ORDER BY created_at",
            (app_id,),
        )
    else:
        rows = db.query("SELECT * FROM log_drains ORDER BY created_at")
    return [dict(row) for row in rows]


def delete_drain(db: Database, drain_id: str) -> bool:
    with db.write() as conn:
        cursor = conn.execute("DELETE FROM log_drains WHERE id = ?", (drain_id,))
    return cursor.rowcount > 0


def set_enabled(db: Database, drain_id: str, enabled: bool) -> dict | None:
    with db.write() as conn:
        conn.execute(
            "UPDATE log_drains SET enabled = ? WHERE id = ?", (1 if enabled else 0, drain_id)
        )
    return get_drain(db, drain_id)


def list_runs(db: Database, drain_id: str, limit: int = 50) -> list[dict]:
    rows = db.query(
        "SELECT * FROM drain_runs WHERE drain_id = ? ORDER BY id DESC LIMIT ?",
        (drain_id, max(1, min(int(limit), 200))),
    )
    return [dict(row) for row in rows]


# --------------------------------------------------------------------------- #
# Running
# --------------------------------------------------------------------------- #
def _next_batch(db: Database, drain: dict) -> list[dict]:
    """The next rows after the cursor, in keyset order.

    Keyset rather than offset, because the cursor has to survive new rows
    arriving while a drain is running.
    """
    clauses = ["severity IN (%s)" % ",".join("?" * len(_at_or_above(drain["min_severity"])))]
    params: list[Any] = list(_at_or_above(drain["min_severity"]))
    if drain["app_id"]:
        clauses.append("app_id = ?")
        params.append(drain["app_id"])
    if drain["cursor_time"] is not None and drain["cursor_id"] is not None:
        clauses.append("(event_time > ? OR (event_time = ? AND id > ?))")
        params.extend([drain["cursor_time"], drain["cursor_time"], drain["cursor_id"]])

    rows = db.query(
        f"""
        SELECT id, {', '.join(DRAIN_COLUMNS)} FROM log_events
         WHERE {' AND '.join(clauses)}
         ORDER BY event_time, id
         LIMIT ?
        """,
        tuple(params) + (int(drain["batch_size"]),),
    )
    return [dict(row) for row in rows]


def _post(target: str, payload: list[dict], timeout: float = 20.0, poster=None) -> dict:
    body = json.dumps({"records": payload}, separators=(",", ":"), default=str).encode("utf-8")
    request = urllib.request.Request(
        target,
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "powerbase-drain/1",
            "X-Powerbase-Records": str(len(payload)),
        },
        method="POST",
    )
    send: Callable = poster or urllib.request.urlopen
    try:
        with send(request, timeout=timeout) as response:
            if not (200 <= response.status < 300):
                raise RuntimeError(f"target returned HTTP {response.status}")
            return {"ok": True, "status": response.status}
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"target returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise RuntimeError(f"target unreachable: {exc}") from exc


def flush(
    db: Database,
    settings: Settings,
    *,
    drain_id: str | None = None,
    now: datetime | None = None,
    poster=None,
) -> dict:
    """Run every enabled drain once. Called from the scheduler and by hand."""
    moment = now or datetime.now(timezone.utc)
    stamp = moment.strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    if drain_id:
        drains = [d for d in [get_drain(db, drain_id)] if d is not None]
    else:
        drains = [d for d in list_drains(db) if d["enabled"]]

    results = []
    for drain in drains:
        batch = _next_batch(db, drain)
        if not batch:
            results.append({"drain": drain["name"], "id": drain["id"], "sent": 0, "ok": True})
            continue

        last = batch[-1]
        error = None
        try:
            _post(drain["target"], batch, poster=poster)
        except Exception as exc:  # noqa: BLE001 - any failure means "do not advance"
            error = str(exc)

        with db.write() as conn:
            conn.execute(
                """
                INSERT INTO drain_runs
                    (drain_id, started_at, finished_at, sent, ok, last_error,
                     cursor_time, cursor_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    drain["id"], stamp, stamp, len(batch), 0 if error else 1,
                    (error[:500] if error else None),
                    None if error else last["event_time"],
                    None if error else last["id"],
                ),
            )
            if not error:
                # The cursor moves only after the target accepted the batch, so
                # a failure is retried from where it stopped rather than skipped.
                conn.execute(
                    """
                    UPDATE log_drains
                       SET cursor_time = ?, cursor_id = ?, last_run_at = ?
                     WHERE id = ?
                    """,
                    (last["event_time"], last["id"], stamp, drain["id"]),
                )

        results.append(
            {
                "drain": drain["name"],
                "id": drain["id"],
                "sent": len(batch),
                "ok": error is None,
                "error": error,
            }
        )

    return {
        "ran": len(results),
        "sent": sum(item["sent"] for item in results),
        "failed": sum(1 for item in results if not item["ok"]),
        "results": results,
    }
