"""Retention, compaction and backup.

Three jobs that only make sense together:

1. **Roll up** yesterday's raw rows into `metrics_daily` (`rollups.rollup_day`).
2. **Prune** raw rows past each app's `retention_days`.
3. **Back up** the file and verify the copy is actually restorable.

The ordering is not incidental — and there is a safety interlock. Pruning will
not delete a day that has not been rolled up yet, so a fresh install with a year
of raw history cannot silently destroy the aggregates it never built. On the
first run the prune is simply deferred until rollups cover the range.
"""

from __future__ import annotations

import shutil
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import rollups
from .db import Database
from .migrations import utcnow_iso
from .queries import quote_ident

# Pruned in this order. Child rows go first so the FK cascades from `sessions`
# have nothing left to remove, and `devices` goes last because a device is only
# removable once no session references it.
#: Housekeeping rows (alert deliveries, drain runs) are pruned on this window
#: when the caller does not supply one. Thirty days is long enough to answer
#: "did that alert ever go out" and short enough to not grow forever.
DEFAULT_OPERATIONAL_RETENTION_DAYS = 30

PRUNE_TARGETS: tuple[tuple[str, str], ...] = (
    ("events", "ts"),
    ("logs", "ts"),
    ("crashes", "ts"),
    ("ingest_batches", "received_at"),
    ("sessions", "started_at"),
    # Observability events. Safe to include because rollup_day now folds them
    # into metrics_daily for the same day it folds everything else, and the
    # window below never reaches back past the first rolled-up day.
    ("log_events", "event_time"),
    # Step 6's tables. They grow with ingestion exactly as log_events did,
    # and were left out of retention when they were added — which meant they
    # grew without a bound. The same rollup floor protects all of them.
    ("spans", "start_time"),
    ("metric_points", "point_time"),
)


def _day_start(day: date) -> str:
    return datetime(day.year, day.month, day.day, tzinfo=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )


def _record(
    conn, job: str, day: str | None, ok: bool, detail: str | None,
    at: datetime | None = None,
) -> None:
    conn.execute(
        "INSERT INTO job_runs (job, day, ran_at, ok, detail) VALUES (?, ?, ?, ?, ?)",
        (job, day, rollups.to_stamp(at), 1 if ok else 0, detail),
    )


# --------------------------------------------------------------------------- #
# Pruning
# --------------------------------------------------------------------------- #

def _prune_operational(conn, moment: datetime, days: int) -> dict:
    """Delete our own housekeeping records — never a notification still owed.

    These two tables are not folded into any rollup, so they are not under
    the floor that protects the observability tables, and they are pruned on
    their own window. What they do have is a rule that matters more than the
    window: a delivery that has not been sent yet is a notification somebody
    is still owed. Deleting one would drop an alert silently, which is the
    exact failure the delivery queue exists to prevent.
    """
    if days <= 0:
        return {"skipped": True, "alert_deliveries": 0, "drain_runs": 0}

    cutoff = (moment - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    # `sent` and `dead` are finished. `pending` and `failed` are not: they
    # are either due now or waiting on a retry.
    cursor = conn.execute(
        "DELETE FROM alert_deliveries WHERE status IN ('sent', 'dead') "
        "AND created_at < ?",
        (cutoff,),
    )
    deliveries = max(cursor.rowcount, 0)

    # A run still in progress has no finished_at, and is left alone.
    cursor = conn.execute(
        "DELETE FROM drain_runs WHERE finished_at IS NOT NULL AND started_at < ?",
        (cutoff,),
    )
    runs = max(cursor.rowcount, 0)

    return {
        "skipped": False,
        "alert_deliveries": deliveries,
        "drain_runs": runs,
        "window_days": days,
    }


def prune(
    db: Database,
    now: datetime | None = None,
    *,
    operational_days: int | None = None,
) -> dict:
    """Delete raw rows older than each app's retention window."""
    moment = now or datetime.now(timezone.utc)
    days = (
        DEFAULT_OPERATIONAL_RETENTION_DAYS
        if operational_days is None
        else int(operational_days)
    )

    # These do not depend on rollups, so they are pruned whether or not the
    # observability tables can be. An instance whose rollups have stopped
    # should not also accumulate unbounded housekeeping rows.
    with db.write() as conn:
        operational = _prune_operational(conn, moment, days)

    first_rolled, _last_rolled = rollups.coverage(db, app_id="")

    if first_rolled is None:
        detail = "deferred: no rollups yet, nothing is safe to prune"
        with db.write() as conn:
            _record(conn, "prune", None, True, detail, at=moment)
        return {
            "skipped": True, "reason": detail, "deleted": {}, "operational": operational
        }

    floor = date.fromisoformat(first_rolled)
    floor_iso = _day_start(floor)
    apps = db.query("SELECT id, retention_days FROM apps ORDER BY id")
    deleted: dict[str, dict[str, int]] = {}
    total = 0

    with db.write() as conn:
        for app in apps:
            cutoff_date = (moment - timedelta(days=int(app["retention_days"]))).date()

            # The deletion window is [floor, cutoff) — it needs BOTH bounds.
            # Rows older than the retention cutoff are the candidates, but rows
            # from before the first rolled-up day must survive regardless: those
            # days were never folded into metrics_daily, so deleting them would
            # destroy history that exists nowhere else.
            if cutoff_date <= floor:
                deleted[app["id"]] = {table: 0 for table, _ in PRUNE_TARGETS} | {"devices": 0}
                continue

            cutoff = _day_start(cutoff_date)
            counts: dict[str, int] = {}
            for table, column in PRUNE_TARGETS:
                cursor = conn.execute(
                    f"DELETE FROM {table} WHERE app_id = ? AND {column} >= ? AND {column} < ?",
                    (app["id"], floor_iso, cutoff),
                )
                counts[table] = max(cursor.rowcount, 0)

            cursor = conn.execute(
                """
                DELETE FROM devices
                 WHERE app_id = ?
                   AND last_seen >= ?
                   AND last_seen < ?
                   AND id NOT IN (SELECT device_id FROM sessions WHERE device_id IS NOT NULL)
                """,
                (app["id"], floor_iso, cutoff),
            )
            counts["devices"] = max(cursor.rowcount, 0)

            deleted[app["id"]] = counts
            total += sum(counts.values())

        _record(
            conn,
            "prune",
            None,
            True,
            f"{total} rows across {len(apps)} apps (window {floor.isoformat()} .. {moment.date().isoformat()})",
            at=moment,
        )

    return {
        "skipped": False,
        "cutoff_floor": floor.isoformat(),
        "deleted": deleted,
        "total": total,
        "operational": operational,
    }


# --------------------------------------------------------------------------- #
# Backup
# --------------------------------------------------------------------------- #

def _backup_path(directory: Path, moment: datetime) -> Path:
    return directory / f"powerbase-{moment.strftime('%Y%m%dT%H%M%SZ')}.db"


def verify_backup(path: str | Path) -> dict:
    """Open a backup and prove it is intact and readable.

    A backup nobody has ever opened is a hope, not a backup. This runs
    `integrity_check` and counts the core tables so a truncated or corrupt copy
    is caught at write time rather than at restore time.
    """
    path = Path(path)
    if not path.exists():
        return {"ok": False, "error": "file missing"}

    connection = sqlite3.connect(str(path))
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        counts = {}
        for table in ("apps", "devices", "sessions", "events", "logs", "crashes", "metrics_daily"):
            if table in tables:
                counts[table] = connection.execute(
                    f"SELECT COUNT(*) FROM {quote_ident(table)}"
                ).fetchone()[0]
        return {"ok": integrity == "ok", "integrity": integrity, "counts": counts}
    except sqlite3.Error as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        connection.close()


def list_backups(directory: str | Path) -> list[dict]:
    directory = Path(directory)
    if not directory.is_dir():
        return []
    found = []
    for path in sorted(directory.glob("powerbase-*.db"), reverse=True):
        stat = path.stat()
        found.append({"path": str(path), "name": path.name, "size_bytes": stat.st_size})
    return found


def backup(
    db: Database,
    directory: str | Path,
    keep: int = 7,
    now: datetime | None = None,
) -> dict:
    """Take an online backup, verify it, then rotate old copies out."""
    moment = now or datetime.now(timezone.utc)
    target_dir = Path(directory)
    target_dir.mkdir(parents=True, exist_ok=True)
    destination = _backup_path(target_dir, moment)

    # The backup API needs no open transaction, and the exclusive lock keeps a
    # writer from landing mid-copy.
    with db.exclusive() as source:
        sink = sqlite3.connect(str(destination))
        try:
            source.backup(sink)
        finally:
            sink.close()

    verification = verify_backup(destination)
    removed = _rotate(target_dir, keep)

    with db.write() as conn:
        _record(
            conn,
            "backup",
            None,
            verification["ok"],
            f"{destination.name} ({destination.stat().st_size} bytes)"
            + ("" if verification["ok"] else f" VERIFY FAILED: {verification}"),
            at=moment,
        )

    return {
        "path": str(destination),
        "size_bytes": destination.stat().st_size,
        "verified": verification["ok"],
        "verification": verification,
        "removed": removed,
    }


def _rotate(directory: Path, keep: int) -> list[str]:
    if keep < 1:
        return []
    existing = sorted(directory.glob("powerbase-*.db"), reverse=True)
    removed = []
    for path in existing[keep:]:
        try:
            path.unlink()
            removed.append(path.name)
        except OSError:
            pass
    return removed


def restore(backup_path: str | Path, db_path: str | Path) -> dict:
    """Copy a backup over the live database file.

    This is an **offline** operation: stop the server first. It takes a safety
    copy of the existing file (and removes any stale `-wal`/`-shm` sidecars)
    before overwriting, so a mistaken restore is still recoverable, but it
    cannot coordinate with a running process that holds the file open.
    """
    backup_path = Path(backup_path)
    db_path = Path(db_path)
    if not backup_path.exists():
        raise FileNotFoundError(backup_path)

    verification = verify_backup(backup_path)
    if not verification["ok"]:
        raise ValueError(f"backup failed verification: {verification}")

    safety = None
    if db_path.exists():
        safety = db_path.with_suffix(db_path.suffix + ".pre-restore")
        shutil.copy2(db_path, safety)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(db_path) + suffix)
            if sidecar.exists():
                sidecar.unlink()

    shutil.copy2(backup_path, db_path)
    return {"restored_from": str(backup_path), "safety_copy": str(safety) if safety else None}


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

def run_daily(
    db: Database,
    *,
    backup_dir: str | Path = "./backups",
    backup_keep: int = 7,
    vacuum: bool = True,
    now: datetime | None = None,
    operational_days: int | None = None,
) -> dict:
    """The nightly pass: roll up yesterday, prune, compact, back up."""
    moment = now or datetime.now(timezone.utc)
    day = rollups.previous_day(moment)

    report: dict = {"ran_at": moment.isoformat(), "day": day}

    report["rollup"] = rollups.rollup_day(db, day, now=moment)
    report["prune"] = prune(db, now=moment, operational_days=operational_days)

    if vacuum and not report["prune"]["skipped"]:
        db.vacuum()
        report["vacuum"] = "ok"

    report["backup"] = backup(db, backup_dir, keep=backup_keep, now=moment)

    # Export housekeeping rides along with the nightly pass: finish anything that
    # was deferred, and take expired results off disk. A row that says "expired"
    # while the CSV is still sitting there has not expired.
    from . import exports as exports_module

    report["exports"] = {
        "completed": exports_module.complete_queued(db, settings_for_exports(db, backup_dir)),
        "expired": exports_module.expire_jobs(db, settings_for_exports(db, backup_dir)),
    }
    return report


def settings_for_exports(db: Database, backup_dir):
    """The settings the export module needs, resolved the same way the server does.

    Kept here so a maintenance run from cron behaves exactly like one from the
    scheduler thread — the export directory must not depend on which one ran.
    """
    from .config import Settings

    settings = Settings.from_env()
    settings.backup_dir = str(backup_dir)
    return settings
