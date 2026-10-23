#!/usr/bin/env python3
"""Run maintenance by hand, or from cron.

    python scripts/maintenance.py --status
    python scripts/maintenance.py --daily                 # roll up, prune, back up
    python scripts/maintenance.py --rollup --days 7       # backfill a week
    python scripts/maintenance.py --prune --vacuum
    python scripts/maintenance.py --backup --keep 14
    python scripts/maintenance.py --verify backups/powerbase-20261022T010000Z.db
    python scripts/maintenance.py --restore backups/... --db ./powerbase.db
    python scripts/maintenance.py --list-backups

If you drive maintenance from cron, set POWERBASE_MAINTENANCE_ENABLED=0 so the
server's own scheduler does not also run it.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from powerbase import maintenance, rollups  # noqa: E402
from powerbase.config import Settings  # noqa: E402
from powerbase.db import Database  # noqa: E402
from powerbase.migrations import run_migrations  # noqa: E402


def _show(title: str, payload) -> None:
    print(f"== {title}")
    print(json.dumps(payload, indent=2, default=str))
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description="Powerbase maintenance")
    parser.add_argument("--db", default=None, help="path to the SQLite file")
    parser.add_argument("--backup-dir", default=None)
    parser.add_argument("--keep", type=int, default=None, help="backups to retain")

    parser.add_argument("--status", action="store_true", help="show recent job runs")
    parser.add_argument("--daily", action="store_true", help="roll up + prune + back up")
    parser.add_argument("--rollup", action="store_true", help="fold a day into metrics_daily")
    parser.add_argument("--days", type=int, default=1, help="days to backfill (with --rollup)")
    parser.add_argument("--day", default=None, help="a specific YYYY-MM-DD to roll up")
    parser.add_argument("--prune", action="store_true")
    parser.add_argument("--vacuum", action="store_true")
    parser.add_argument("--backup", action="store_true")
    parser.add_argument("--list-backups", action="store_true")
    parser.add_argument("--verify", default=None, help="verify a backup file")
    parser.add_argument("--restore", default=None, help="restore a backup file")
    args = parser.parse_args()

    settings = Settings.from_env()
    db_path = args.db or settings.db_path
    backup_dir = args.backup_dir or settings.backup_dir
    keep = args.keep if args.keep is not None else settings.backup_keep

    if args.verify:
        result = maintenance.verify_backup(args.verify)
        _show(f"verify {args.verify}", result)
        return 0 if result["ok"] else 1

    if args.restore:
        result = maintenance.restore(args.restore, db_path)
        _show("restore", result)
        return 0

    if args.list_backups:
        _show(f"backups in {backup_dir}", maintenance.list_backups(backup_dir))
        return 0

    db = Database(db_path)
    run_migrations(db, settings.migrations_dir)

    if args.status:
        _show("recent job runs", rollups.recent_runs(db, limit=20))
        first, last = rollups.coverage(db, app_id="")
        _show("rollup coverage", {"first_day": first, "last_day": last})
        return 0

    if args.daily:
        report = maintenance.run_daily(db, backup_dir=backup_dir, backup_keep=keep,
                                       vacuum=settings.vacuum_after_prune,
                                       operational_days=settings.operational_retention_days)
        _show("daily pass", report)
        return 0

    did_something = False

    if args.rollup:
        did_something = True
        if args.day:
            _show(f"rollup {args.day}", rollups.rollup_day(db, args.day))
        else:
            _show(f"rollup last {args.days} day(s)", rollups.backfill(db, args.days))

    if args.prune:
        did_something = True
        _show("prune", maintenance.prune(db))

    if args.vacuum:
        did_something = True
        db.vacuum()
        _show("vacuum", {"status": "ok"})

    if args.backup:
        did_something = True
        _show("backup", maintenance.backup(db, backup_dir, keep=keep))

    if not did_something:
        parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
