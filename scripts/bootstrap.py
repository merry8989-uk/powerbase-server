#!/usr/bin/env python3
"""Create the database, apply migrations, register an app and print its keys.

    python scripts/bootstrap.py --app-id com.example.shop --name "Example Shop"

Keys are printed once and never stored in plaintext — copy them now.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from powerbase.config import Settings  # noqa: E402
from powerbase.db import Database  # noqa: E402
from powerbase.migrations import run_migrations  # noqa: E402
from powerbase.provision import issue_key, provision_app  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Bootstrap a Powerbase instance")
    parser.add_argument("--db", default=None, help="path to the SQLite file")
    parser.add_argument("--app-id", required=True, help="package name or service slug")
    parser.add_argument("--name", required=True, help="human-readable app name")
    parser.add_argument(
        "--platform", default="android", choices=["android", "ios", "backend", "web"]
    )
    parser.add_argument("--retention-days", type=int, default=90)
    parser.add_argument("--sampling-rate", type=float, default=1.0)
    args = parser.parse_args()

    settings = Settings.from_env()
    db_path = args.db or settings.db_path
    db = Database(db_path)

    applied = run_migrations(db, settings.migrations_dir)
    print(f"database:  {db_path}")
    print(f"migrations applied: {applied or 'none (already up to date)'}")

    provision_app(
        db,
        args.app_id,
        args.name,
        platform=args.platform,
        retention_days=args.retention_days,
        sampling_rate=args.sampling_rate,
    )
    print(f"app registered: {args.app_id} ({args.platform})")

    ingest = issue_key(db, args.app_id, "ingest", "default ingest key")
    read = issue_key(db, args.app_id, "read", "default read key")

    print()
    print("=" * 68)
    print("  API KEYS — shown once, store them securely")
    print("=" * 68)
    print(f"  ingest (write-only): {ingest}")
    print(f"  read   (dashboard):  {read}")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
