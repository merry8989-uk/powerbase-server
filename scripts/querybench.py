#!/usr/bin/env python3
"""Load test and query-plan check for the Logs Explorer's read path.

`scripts/loadtest.py` covers **ingestion**. This covers the other half: the path
a person waits on when they open the explorer, which is the one with keyset
pagination, an allowlisted filter language and a query timeout — all of which
deserve evidence rather than confidence.

It answers two questions:

1. **How long does a query take** on a table of a realistic size? Four scenarios,
   each timed repeatedly and reported as a median, because a single run on a
   shared machine measures the machine.
2. **Which index does it use?** `EXPLAIN QUERY PLAN` for the same four, checked
   against the ten indexes on `log_events`. A query that is fast today because
   the table is small, and that will scan tomorrow, is the failure this is here
   to catch.

Usage:

    python scripts/querybench.py --rows 1000000
    python scripts/querybench.py --rows 200000 --quick      # for CI

The database is created fresh in a temporary directory and removed afterwards,
so this is safe to run anywhere.

## What it found the first time it ran

1. **Every query was sorting.** The indexes were `(app_id, event_time DESC)`
   while the query orders by `event_time DESC, id DESC`, so the planner could
   not satisfy the ordering from the index and reported
   `USE TEMP B-TREE FOR RIGHT PART OF ORDER BY` on all five scenarios.
   Migration 0010 declares `id DESC` as the last index column; the sort step
   is gone.

2. **Deep pagination was ~7x too expensive.** The cursor clause was written
   as `event_time < ? OR (event_time = ? AND id < ?)`, which made SQLite
   choose `MULTI-INDEX OR` and sort everything after the cursor before the
   LIMIT could apply. Page 1 cost 32 ms and every page after it 120-170 ms.
   As a row-value comparison it is a single index seek: pages now sit at
   16-28 ms and the cost no longer grows with depth.

Both are guarded by tests in `tests/test_query_envelope.py`, which assert the
**plan** rather than a timing — a timing test would pass on a fast machine
with the bug still in place.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import statistics
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from powerbase import logquery  # noqa: E402
from powerbase.config import Settings  # noqa: E402
from powerbase.db import Database  # noqa: E402
from powerbase.migrations import run_migrations  # noqa: E402
from powerbase.provision import provision_app  # noqa: E402

APP_ID = "com.example.shop"
SPAN_DAYS = 90

SEVERITIES = ["INFO"] * 80 + ["WARN"] * 12 + ["ERROR"] * 7 + ["FATAL"] + ["TRACE"] * 4
EVENT_TYPES = [
    "http.request", "job_feed_import_failed", "page_view", "user_login",
    "db.query", "cache.miss", "order_placed",
]
ROUTES = ["/orders/{id}", "/checkout", "/health", "/api/v1/items", "/search"]
CATEGORIES = ["api", "ui", "job", "auth"]
SOURCE_TYPES = ["web", "android", "backend"]


def seed(db: Database, rows: int, *, seed_value: int = 7) -> float:
    """Write `rows` log events straight to SQLite.

    Straight to SQL, not through the API: this is a read-path benchmark, and
    spending ten minutes generating rows over HTTP would measure the write path
    again for no reason.
    """
    rng = random.Random(seed_value)
    end = datetime.now(timezone.utc).replace(microsecond=0)
    start = end - timedelta(days=SPAN_DAYS)
    window = int((end - start).total_seconds())

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    sql = (
        "INSERT INTO log_events "
        "(event_id, app_id, event_time, received_at, source, severity, event_type, "
        " service_name, environment, app_version, request_id, trace_id, route_template, "
        " http_method, status_code, duration_ms, error_code, error_fingerprint, message, "
        " attributes, redaction_version, sdk_version, category, source_type, source_id, "
        " platform_version) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
    )

    began = time.monotonic()
    batch: list[tuple] = []
    with db.write() as conn:
        for i in range(rows):
            when = start + timedelta(seconds=rng.randrange(window))
            severity = rng.choice(SEVERITIES)
            is_error = severity in ("ERROR", "FATAL")
            batch.append(
                (
                    f"e{i:09d}", APP_ID,
                    when.strftime("%Y-%m-%dT%H:%M:%S.%fZ"), stamp,
                    rng.choice(["browser_sdk", "server_sdk", "otel"]),
                    severity, rng.choice(EVENT_TYPES),
                    "shop-api", "production", "1.4.0",
                    f"req-{rng.randrange(1 << 20):x}", f"{rng.randrange(1 << 40):010x}",
                    rng.choice(ROUTES), rng.choice(["GET", "POST"]),
                    rng.choice([200, 200, 200, 404, 500]) if not is_error else 500,
                    rng.randrange(5, 5000),
                    "UPSTREAM_TIMEOUT" if is_error else None,
                    f"{rng.randrange(1 << 32):08x}" if is_error else None,
                    "the job feed request timed out" if is_error else "ok",
                    "{}", 1, "1.4.0",
                    rng.choice(CATEGORIES), rng.choice(SOURCE_TYPES),
                    "shop-web", "Chrome 141",
                )
            )
            if len(batch) >= 5000:
                conn.executemany(sql, batch)
                batch.clear()
        if batch:
            conn.executemany(sql, batch)

    return time.monotonic() - began


#: The index shapes as they were before migration 0010 — no `id` column, so the
#: planner could not satisfy `ORDER BY event_time DESC, id DESC` from the index
#: and built a temp B-tree on every query. Kept here so `--compare` can measure
#: the difference rather than assert it.
LEGACY_INDEXES = {
    "idx_log_events_app_time": "app_id, event_time DESC",
    "idx_log_events_app_sev_time": "app_id, severity, event_time DESC",
    "idx_log_events_app_type_time": "app_id, event_type, event_time DESC",
    "idx_log_events_app_source_time": "app_id, source, event_time DESC",
    "idx_log_events_fingerprint": "app_id, error_fingerprint, event_time DESC",
    "idx_log_events_category": "app_id, category, event_time DESC",
    "idx_log_events_source_type": "app_id, source_type, event_time DESC",
}


def use_legacy_indexes(db: Database) -> None:
    """Put the pre-0010 index shapes back, for the comparison."""
    with db.write() as conn:
        for name, columns in LEGACY_INDEXES.items():
            conn.execute(f"DROP INDEX IF EXISTS {name}")
            conn.execute(f"CREATE INDEX {name} ON log_events({columns})")
        conn.execute("ANALYZE")


def use_ordered_indexes(db: Database) -> None:
    """Restore what migration 0010 declares."""
    with db.write() as conn:
        for name, columns in LEGACY_INDEXES.items():
            conn.execute(f"DROP INDEX IF EXISTS {name}")
            conn.execute(f"CREATE INDEX {name} ON log_events({columns}, id DESC)")
        conn.execute("ANALYZE")


def iso(days_ago: float) -> str:
    moment = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# --------------------------------------------------------------------------- #
# Scenarios
# --------------------------------------------------------------------------- #
def scenarios() -> list[tuple[str, dict, str]]:
    """name, query_log_events kwargs, what it is meant to exercise."""
    # Built through `parse_filters`, which is what the API calls, so the
    # benchmark exercises the same validated path a client does.
    error_filter = logquery.parse_filters(
        [{"field": "severity", "operator": "eq", "value": "ERROR"}]
    )
    return [
        (
            "default (7d, no filters)",
            {"since": iso(7), "limit": 50},
            "the query the explorer runs when it opens",
        ),
        (
            "wide (90d, no filters)",
            {"since": iso(90), "limit": 50},
            "the whole retention window",
        ),
        (
            "filtered (severity=ERROR, 7d)",
            {"since": iso(7), "filters": error_filter, "limit": 50},
            "a selective index",
        ),
        (
            "narrow window (1h, no filters)",
            {"since": iso(1 / 24), "limit": 50},
            "a window with almost nothing in it",
        ),
        (
            "free text (q=timeout, 7d)",
            {"since": iso(7), "q": "timeout", "limit": 50},
            "a LIKE scan, which no index can serve",
        ),
    ]


def plan_for(db: Database, kwargs: dict) -> str:
    """The query plan, built from the same clause builder the query uses."""
    where, params = logquery.build_where(
        app_id=APP_ID,
        since=kwargs.get("since"),
        until=kwargs.get("until"),
        filters=kwargs.get("filters") or [],
        q=kwargs.get("q"),
    )
    sql = (
        f"SELECT * FROM log_events WHERE {where} "
        f"ORDER BY event_time DESC, id DESC LIMIT ?"
    )
    rows = db.query("EXPLAIN QUERY PLAN " + sql, tuple(params) + (51,))
    return " | ".join(str(row["detail"]) for row in rows)


def time_it(db: Database, kwargs: dict, repeats: int) -> dict:
    samples = []
    rows_returned = 0
    for _ in range(repeats):
        began = time.monotonic()
        page = logquery.query_log_events(db, app_id=APP_ID, **kwargs)
        samples.append((time.monotonic() - began) * 1000)
        rows_returned = len(page.items)
    return {
        "median_ms": round(statistics.median(samples), 1),
        "min_ms": round(min(samples), 1),
        "max_ms": round(max(samples), 1),
        "rows": rows_returned,
    }


def walk_pages(db: Database, pages: int, limit: int = 200) -> dict:
    """Keyset pagination, walked to depth.

    The reason to measure this separately: a cursor should make page 200 cost
    what page 1 costs, and an implementation that has quietly become an OFFSET
    would not.
    """
    cursor = None
    timings = []
    for _ in range(pages):
        began = time.monotonic()
        page = logquery.query_log_events(
            db, app_id=APP_ID, since=iso(90), limit=limit, cursor=cursor
        )
        timings.append((time.monotonic() - began) * 1000)
        cursor = page.next_cursor
        if cursor is None:
            break
    return {
        "pages": len(timings),
        "limit": limit,
        "first_ms": round(timings[0], 1),
        "last_ms": round(timings[-1], 1),
        "median_ms": round(statistics.median(timings), 1),
        "grew": round(timings[-1] / max(timings[0], 0.001), 2),
    }


def index_usage(db: Database) -> dict:
    """Which indexes exist, and whether SQLite has ever used them.

    An index that is never used is either redundant or a query that is not
    shaped the way the index expects — both worth knowing.
    """
    indexes = [
        row["name"]
        for row in db.query(
            "SELECT name FROM sqlite_master WHERE type = 'index' "
            "AND tbl_name = 'log_events' AND sql IS NOT NULL ORDER BY name"
        )
    ]
    return {"count": len(indexes), "names": indexes}


def main() -> int:
    parser = argparse.ArgumentParser(description="Logs Explorer read-path benchmark")
    parser.add_argument("--rows", type=int, default=1_000_000, help="rows to seed")
    parser.add_argument("--repeats", type=int, default=3, help="timed runs per scenario")
    parser.add_argument("--pages", type=int, default=50, help="pagination depth to walk")
    parser.add_argument("--quick", action="store_true", help="short run for CI")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--keep", action="store_true", help="do not delete the database")
    parser.add_argument(
        "--compare", action="store_true",
        help="also measure the pre-0010 index shapes, for a side-by-side",
    )
    args = parser.parse_args()

    if args.quick:
        # Small enough to run on every push. The point in CI is that the
        # plans are checked, not that a number is produced.
        args.rows = min(args.rows, 25_000)
        args.repeats = 1
        args.pages = 10

    workdir = tempfile.mkdtemp(prefix="powerbase-bench-")
    db_path = os.path.join(workdir, "bench.db")
    settings = Settings(db_path=db_path, admin_password="bench")
    db = Database(db_path)
    run_migrations(db, settings.migrations_dir)
    provision_app(db, APP_ID, "Example Shop", "backend")

    report: dict = {"rows": args.rows, "span_days": SPAN_DAYS}

    seeded = seed(db, args.rows)
    report["seed_seconds"] = round(seeded, 1)
    report["rows_per_second"] = int(args.rows / max(seeded, 0.001))
    # ANALYZE so the planner has statistics. Without it SQLite guesses, and a
    # query plan measured against a guess is not evidence about production.
    with db.write() as conn:
        conn.execute("ANALYZE")

    actual = db.scalar("SELECT COUNT(*) FROM log_events")
    report["rows_stored"] = actual
    report["db_bytes"] = os.path.getsize(db_path)

    def run_scenarios() -> list[dict]:
        out = []
        for name, kwargs, purpose in scenarios():
            timing = time_it(db, kwargs, args.repeats)
            out.append(
                {"scenario": name, "purpose": purpose, "plan": plan_for(db, kwargs), **timing}
            )
        return out

    if args.compare:
        use_legacy_indexes(db)
        before = run_scenarios()
        before_pagination = walk_pages(db, args.pages)
        report["before_0010"] = {"scenarios": before, "pagination": before_pagination}
        use_ordered_indexes(db)

    results = run_scenarios()
    report["scenarios"] = results
    report["pagination"] = walk_pages(db, args.pages)
    report["indexes"] = index_usage(db)

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"\n  seeded {actual:,} rows in {report['seed_seconds']}s "
              f"({report['rows_per_second']:,} rows/s), "
              f"database {report['db_bytes'] / 1e6:.1f} MB\n")
        header = f"  {'scenario':<34} {'median':>8} {'min':>7} {'max':>7} {'rows':>5}"
        print(header)
        print("  " + "-" * (len(header) - 2))
        for item in results:
            print(
                f"  {item['scenario']:<34} {item['median_ms']:>7}ms "
                f"{item['min_ms']:>6}ms {item['max_ms']:>6}ms {item['rows']:>5}"
            )

        if args.compare:
            print(f"\n  {'scenario':<34} {'before':>9} {'after':>9} {'change':>9}")
            print("  " + "-" * 64)
            for old_item, new_item in zip(report["before_0010"]["scenarios"], results):
                b, a = old_item["median_ms"], new_item["median_ms"]
                print(f"  {new_item['scenario']:<34} {b:>7}ms {a:>7}ms {a / max(b, 0.001):>8.2f}x")
            bp, ap = report["before_0010"]["pagination"], report["pagination"]
            print(f"\n  pagination median      before {bp['median_ms']}ms  "
                  f"after {ap['median_ms']}ms  ({ap['median_ms'] / max(bp['median_ms'], 0.001):.2f}x)")
            print(f"  pagination growth      before {bp['grew']}x  after {ap['grew']}x")

        page = report["pagination"]
        print(f"\n  pagination: {page['pages']} pages of {page['limit']}, "
              f"first {page['first_ms']}ms, last {page['last_ms']}ms, "
              f"median {page['median_ms']}ms, grew {page['grew']}x")

        print("\n  query plans")
        for item in results:
            print(f"    {item['scenario']}")
            for part in item["plan"].split(" | "):
                print(f"      {part}")
        print(f"\n  indexes on log_events: {report['indexes']['count']}")

    if not args.keep:
        shutil.rmtree(workdir, ignore_errors=True)
    else:
        print(f"\n  kept: {db_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
