"""Capacity: the numbers that tell you the instance is about to have a problem.

Nothing here is clever. It reads the file size, counts rows, works out how fast
they are arriving, and says so. The value is that it answers "is this going to
fill the disk" and "did last night's pass actually run" without anyone having to
go and look.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import Settings
from .db import Database

#: Tables that grow with ingestion, and the column that dates their rows.
GROWING = (
    ("events", "ts"),
    ("logs", "ts"),
    ("crashes", "ts"),
    ("log_events", "event_time"),
    ("spans", "start_time"),
    ("metric_points", "point_time"),
    ("sessions", "started_at"),
    ("ingest_batches", "received_at"),
)


def _size(path: str | Path) -> int | None:
    try:
        return os.path.getsize(path)
    except OSError:
        return None


def _age_hours(stamp: str | None, now: datetime) -> float | None:
    if not stamp:
        return None
    try:
        moment = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return round((now - moment).total_seconds() / 3600, 2)


def report(
    db: Database, settings: Settings, *, now: datetime | None = None, window_days: int = 7
) -> dict:
    moment = now or datetime.now(timezone.utc)
    since = (moment - timedelta(days=window_days)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    db_path = Path(settings.db_path)
    db_bytes = _size(db_path)
    wal_bytes = _size(str(db_path) + "-wal")
    shm_bytes = _size(str(db_path) + "-shm")

    tables: dict[str, Any] = {}
    total_rows = 0
    for table, column in GROWING:
        try:
            count = db.scalar(f"SELECT COUNT(*) FROM {table}") or 0
            recent = db.scalar(
                f"SELECT COUNT(*) FROM {table} WHERE {column} >= ?", (since,)
            ) or 0
            oldest = db.scalar(f"SELECT MIN({column}) FROM {table}")
            newest = db.scalar(f"SELECT MAX({column}) FROM {table}")
        except Exception:
            continue
        total_rows += count
        tables[table] = {
            "rows": count,
            "rows_in_window": recent,
            "rows_per_day": round(recent / window_days, 1),
            "oldest": oldest,
            "newest": newest,
        }

    # Growth in bytes per day, measured rather than assumed: a row of a log
    # event is not the same size as a row of a metric point.
    bytes_per_day = None
    if db_bytes and total_rows:
        recent_rows = sum(item["rows_in_window"] for item in tables.values())
        if recent_rows:
            bytes_per_day = round(db_bytes * (recent_rows / total_rows) / window_days, 0)

    page_count = db.scalar("PRAGMA page_count") or 0
    page_size = db.scalar("PRAGMA page_size") or 0
    freelist = db.scalar("PRAGMA freelist_count") or 0

    backup_dir = Path(settings.backup_dir)
    backups: list[dict] = []
    try:
        for path in sorted(backup_dir.glob("powerbase-*.db"), reverse=True)[:10]:
            stat = path.stat()
            backups.append(
                {
                    "name": path.name,
                    "bytes": stat.st_size,
                    "age_hours": round((moment.timestamp() - stat.st_mtime) / 3600, 1),
                }
            )
    except OSError:
        pass

    jobs = {
        row["job"]: dict(row)
        for row in db.query(
            """
            SELECT job, day, ran_at, ok, detail FROM job_runs
             WHERE id IN (SELECT MAX(id) FROM job_runs GROUP BY job)
            """
        )
    }

    retention = [
        dict(row) for row in db.query("SELECT id, retention_days FROM apps ORDER BY id")
    ]

    warnings: list[str] = []
    if not settings.maintenance_enabled:
        warnings.append(
            "the in-process scheduler is off; drive scripts/maintenance.py from cron "
            "or rollups and pruning will not happen"
        )
    last_rollup = jobs.get("rollup")
    if last_rollup is None:
        warnings.append("no rollup has ever run, so nothing is safe to prune yet")
    elif _age_hours(last_rollup.get("ran_at"), moment) or 0 > 48:
        warnings.append("the last rollup was more than two days ago")
    if not backups:
        warnings.append("no backups found")
    elif (backups[0]["age_hours"] or 0) > 48:
        warnings.append(f"the newest backup is {backups[0]['age_hours']}h old")
    dead = db.scalar("SELECT COUNT(*) FROM alert_deliveries WHERE status = 'dead'") or 0
    if dead:
        warnings.append(f"{dead} alert delivery/deliveries gave up and are marked dead")
    # Observability tables are the ones most likely to grow without a bound.
    for table in ("log_events", "spans", "metric_points"):
        if tables.get(table, {}).get("rows", 0) and not settings.maintenance_enabled:
            continue
    if tables.get("metric_points", {}).get("rows", 0) > 5_000_000:
        warnings.append("metric_points is very large; consider shortening its retention")

    return {
        "generated_at": moment.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "window_days": window_days,
        "database": {
            "path": str(db_path),
            "bytes": db_bytes,
            "wal_bytes": wal_bytes,
            "shm_bytes": shm_bytes,
            "pages": page_count,
            "page_size": page_size,
            "freelist_pages": freelist,
            "free_bytes": freelist * page_size if page_size else None,
            "rows": total_rows,
            "bytes_per_day": bytes_per_day,
        },
        "tables": tables,
        "backups": {"dir": str(backup_dir), "count": len(backups), "recent": backups},
        "jobs": jobs,
        "retention": retention,
        "warnings": warnings,
    }


def human_bytes(value: float | None) -> str:
    if value is None:
        return "—"
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"
