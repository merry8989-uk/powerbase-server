#!/usr/bin/env python3
"""Migration tooling.

    python scripts/migrate.py --status      # applied / pending / unknown
    python scripts/migrate.py --verify      # integrity + required tables + pending
    python scripts/migrate.py --apply       # apply anything pending
    python scripts/migrate.py --drill       # rollback drill, on a scratch copy

SQLite has no down-migrations, so the rollback mechanism for a schema change is
"restore the pre-migration backup". `--drill` proves that path works rather than
assuming it, and never touches the live database.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from powerbase.config import Settings  # noqa: E402
from powerbase.db import Database  # noqa: E402
from powerbase.migrations import (  # noqa: E402
    rollback_drill,
    run_migrations,
    status,
    verify,
)


def _show(title: str, payload) -> None:
    print(f"== {title}")
    print(json.dumps(payload, indent=2, default=str))
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description="Powerbase migrations")
    parser.add_argument("--db", default=None)
    parser.add_argument("--workdir", default="./.migration-drill")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--drill", action="store_true")
    args = parser.parse_args()

    settings = Settings.from_env()
    db_path = args.db or settings.db_path
    db = Database(db_path)

    if args.apply:
        applied = run_migrations(db, settings.migrations_dir)
        _show("apply", {"applied": applied or "none (already up to date)"})
        return 0

    if args.status:
        _show("migration status", status(db, settings.migrations_dir))
        return 0

    if args.verify:
        result = verify(db, settings.migrations_dir)
        _show("verify", result)
        return 0 if result["ok"] else 1

    if args.drill:
        result = rollback_drill(db_path, args.workdir)
        _show("rollback drill", result)
        return 0 if result["ok"] else 1

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
