"""Liveness, readiness, and the operator's view of an instance.

Two audiences, two endpoints, deliberately different:

* ``/healthz`` is public and says almost nothing. A load balancer or an uptime
  monitor needs a cheap yes/no and nothing else — anything more is a free
  inventory of your deployment for whoever finds the URL.
* ``/v1/status`` is the operator's view: versions, sizes, row counts, the last
  thing that arrived, and how the nightly jobs went. It needs the admin
  password, because that is exactly the information you do not want public.

The public HTML page at ``/status`` is registered in the dashboard module,
where the templates live; it shows the same small set as ``/healthz``.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from fastapi import Depends, HTTPException, status

from .config import Settings
from .db import Database
from .version import __version__

#: Process start, for uptime. Set at import, which is when the process starts.
STARTED_AT = time.time()


def uptime_seconds() -> float:
    return time.time() - STARTED_AT


def human_uptime(seconds: float | None = None) -> str:
    total = int(seconds if seconds is not None else uptime_seconds())
    days, rest = divmod(total, 86_400)
    hours, rest = divmod(rest, 3_600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _db_reachable(db: Database) -> tuple[bool, str | None]:
    try:
        db.scalar("SELECT 1")
        return True, None
    except Exception as exc:  # pragma: no cover - only on a broken database
        return False, f"{type(exc).__name__}: {exc}"


def _file_size(path: str) -> int | None:
    try:
        return os.path.getsize(path)
    except OSError:
        return None


def instance_summary(db: Database, settings: Settings) -> dict:
    """Everything an operator wants to see, in one call.

    Counts are read straight from the tables. On a very large database this
    would need to come from the rollups instead; at the scale this is built for,
    a COUNT on an indexed table is milliseconds.
    """
    reachable, error = _db_reachable(db)

    from . import deployment

    summary: dict = {
        "service": "powerbase",
        "version": __version__,
        # Whether this instance is the shape it is built for, and what changes if
        # it is not. Visible here so an operator does not have to find it in the
        # logs — and because a rate limit that silently multiplies is exactly the
        # kind of thing that never gets looked at until it matters.
        "deployment": deployment.summarise(settings),
        "uptime_seconds": round(uptime_seconds(), 1),
        "uptime": human_uptime(),
        "database": {
            "path": settings.db_path,
            "reachable": reachable,
            "error": error,
            "size_bytes": _file_size(settings.db_path),
            "journal_mode": None,
        },
        "totals": {},
        "ingest": {},
        "maintenance": {},
        "backups": {},
        "settings": {
            "redaction_enabled": settings.redaction_enabled,
            "retention_days_default": None,
            "alerts_enabled": settings.alerts_enabled,
            "maintenance_enabled": settings.maintenance_enabled,
            "dashboard_enabled": settings.dashboard_enabled,
            "rate_per_second": settings.rate_per_second,
            "project_rate_per_second": settings.project_rate_per_second,
            "max_batch_size": settings.max_batch_size,
            "log_events_max_attributes": settings.log_events_max_attributes,
        },
    }

    if not reachable:
        return summary

    summary["database"]["journal_mode"] = db.scalar("PRAGMA journal_mode")

    for table in ("apps", "devices", "sessions", "events", "logs", "crashes", "log_events"):
        try:
            summary["totals"][table] = db.scalar(f"SELECT COUNT(*) FROM {table}") or 0
        except Exception:
            summary["totals"][table] = None

    summary["totals"]["api_keys"] = db.scalar("SELECT COUNT(*) FROM api_keys") or 0
    summary["totals"]["api_keys_revoked"] = (
        db.scalar("SELECT COUNT(*) FROM api_keys WHERE revoked_at IS NOT NULL") or 0
    )

    # The last thing that actually arrived, which is the number that tells you
    # whether ingestion is working right now.
    for label, table, column in (
        ("last_batch_at", "ingest_batches", "received_at"),
        ("last_log_event_at", "log_events", "received_at"),
        ("last_event_at", "events", "received_at"),
    ):
        try:
            summary["ingest"][label] = db.scalar(f"SELECT MAX({column}) FROM {table}")
        except Exception:
            summary["ingest"][label] = None

    summary["ingest"]["batches"] = db.scalar("SELECT COUNT(*) FROM ingest_batches") or 0

    # The maintenance ledger, so "did last night's pass run?" is answerable.
    try:
        rows = db.query(
            """
            SELECT job, day, ran_at, ok, detail FROM job_runs
            ORDER BY ran_at DESC LIMIT 10
            """
        )
        summary["maintenance"]["recent"] = [dict(row) for row in rows]
        summary["maintenance"]["last_ok_at"] = db.scalar(
            "SELECT MAX(ran_at) FROM job_runs WHERE ok = 1"
        )
    except Exception:
        summary["maintenance"]["recent"] = []
        summary["maintenance"]["last_ok_at"] = None

    backup_dir = Path(settings.backup_dir)
    try:
        backups = sorted(
            (p for p in backup_dir.glob("*.db") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        summary["backups"] = {
            "dir": str(backup_dir),
            "count": len(backups),
            "latest": backups[0].name if backups else None,
            "latest_at": (
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(backups[0].stat().st_mtime))
                if backups
                else None
            ),
            "latest_bytes": backups[0].stat().st_size if backups else None,
        }
    except Exception:
        summary["backups"] = {"dir": str(backup_dir), "count": 0, "latest": None}

    return summary


def register_status_routes(app, db: Database, settings: Settings, require_admin) -> None:
    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict:
        """Liveness. Cheap, public, and says nothing about the deployment.

        Deliberately not `/v1/health`: that one reports the version and the
        journal mode, which is fine for an operator and unnecessary for a load
        balancer.
        """
        reachable, _ = _db_reachable(db)
        if not reachable:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "database unavailable")
        return {"status": "ok"}

    @app.get("/v1/status", tags=["status"])
    def instance_status(_: None = Depends(require_admin)) -> dict:
        return instance_summary(db, settings)

    @app.get("/v1/status/version", include_in_schema=False)
    def version_only() -> dict:
        """Public and trivially small, for the updater and for support."""
        return {"service": "powerbase", "version": __version__}
