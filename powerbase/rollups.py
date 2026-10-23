"""Daily rollups: raw tables -> metrics_daily.

Raw rows are pruned at each app's `retention_days` (default 90). The dashboard's
month-over-month and long-range charts must keep working after that pruning, so
each day is folded into `metrics_daily` before the raw rows go away.

Two properties matter:

* **Idempotent.** Re-running a day overwrites its rows rather than doubling
  them, so a backfill or a retried job is always safe.
* **Complete.** Every app gets its base metrics written for every day, even
  when the value is zero. That is what lets the read path tell "this day had no
  activity" apart from "this day was never rolled up".
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from .db import Database
from .migrations import utcnow_iso

DAY_FORMAT = "%Y-%m-%d"

# Metrics written with dimension = '' for every app, every day.
BASE_METRICS = (
    "sessions",
    "events",
    "logs",
    "error_logs",
    "crashes",
    "active_devices",
    "crashed_sessions",
    "session_duration_total_ms",
    # Observability events. Rolled up by the same job as the rest, which is what
    # makes it safe to prune them: the job ledger day that protects the
    # analytics tables protects log_events too.
    "log_events",
    "log_events_errors",
    # Step 6. Folded in by the same job, so the same ledger day protects
    # them and they are safe to prune on the same schedule.
    "spans",
    "span_errors",
    "metric_points",
)

# Metrics written per dimension value, only for values that occurred.
DIMENSIONED_METRICS = (
    "events_by_screen",
    "events_by_name",
    "logs_by_level",
    "crashes_by_fingerprint",
    "log_events_by_severity",
)

ALL_METRICS = BASE_METRICS + DIMENSIONED_METRICS


def day_bounds(day: str) -> tuple[str, str]:
    """'2026-10-22' -> ('2026-10-22T00:00:00.000000Z', '2026-10-23T00:00:00.000000Z').

    Comparing ISO strings is index-friendly, unlike `substr(ts, 1, 10) = ?`.
    """
    start = datetime.strptime(day, DAY_FORMAT).replace(tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    return _iso(start), _iso(end)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def previous_day(now: datetime | None = None) -> str:
    """The last *complete* UTC day — the one a nightly job should fold up."""
    moment = now or datetime.now(timezone.utc)
    return (moment.date() - timedelta(days=1)).strftime(DAY_FORMAT)


def to_stamp(moment: datetime | None) -> str:
    """The timestamp a job run is recorded with.

    Jobs accept an injected `now` so they can be tested and backfilled, and the
    ledger has to be written with *that* clock — otherwise "did today's run
    already happen?" compares a real wall clock against a simulated one and the
    answer is always no.
    """
    return _iso(moment) if moment is not None else utcnow_iso()


def _record_run(
    conn, job: str, day: str | None, ok: bool, detail: str | None,
    at: datetime | None = None,
) -> None:
    conn.execute(
        "INSERT INTO job_runs (job, day, ran_at, ok, detail) VALUES (?, ?, ?, ?, ?)",
        (job, day, to_stamp(at), 1 if ok else 0, detail),
    )


# --------------------------------------------------------------------------- #
# Writing rollups
# --------------------------------------------------------------------------- #

def rollup_day(db: Database, day: str, now: datetime | None = None) -> dict:
    """Fold one UTC day into `metrics_daily` for every registered app."""
    start, end = day_bounds(day)
    apps = [row["id"] for row in db.query("SELECT id FROM apps ORDER BY id")]
    written = 0

    with db.write() as conn:
        for app_id in apps:
            written += _rollup_app_day(conn, app_id, day, start, end)
        _record_run(conn, "rollup", day, True, f"{len(apps)} apps, {written} rows", at=now)

    return {"day": day, "apps": len(apps), "rows": written}


def _rollup_app_day(conn, app_id: str, day: str, start: str, end: str) -> int:
    def scalar(sql: str, params: tuple) -> float:
        row = conn.execute(sql, params).fetchone()
        value = row[0] if row is not None else None
        return float(value) if value is not None else 0.0

    def count(table: str, column: str, extra: str = "") -> float:
        return scalar(
            f"SELECT COUNT(*) FROM {table} WHERE app_id = ? AND {column} >= ? AND {column} < ? {extra}",
            (app_id, start, end),
        )

    values: dict[str, float] = {
        "sessions": count("sessions", "started_at"),
        "events": count("events", "ts"),
        "logs": count("logs", "ts"),
        "error_logs": count("logs", "ts", "AND level IN ('ERROR', 'FATAL')"),
        "crashes": count("crashes", "ts"),
        "active_devices": scalar(
            "SELECT COUNT(DISTINCT device_id) FROM sessions WHERE app_id = ? AND started_at >= ? AND started_at < ?",
            (app_id, start, end),
        ),
        "crashed_sessions": scalar(
            "SELECT COUNT(DISTINCT session_id) FROM crashes "
            "WHERE app_id = ? AND ts >= ? AND ts < ? AND session_id IS NOT NULL",
            (app_id, start, end),
        ),
        "session_duration_total_ms": scalar(
            "SELECT COALESCE(SUM(duration_ms), 0) FROM sessions "
            "WHERE app_id = ? AND started_at >= ? AND started_at < ? AND duration_ms IS NOT NULL",
            (app_id, start, end),
        ),
        "log_events": count("log_events", "event_time"),
        "log_events_errors": count(
            "log_events", "event_time", "AND severity IN ('ERROR', 'FATAL')"
        ),
        "spans": count("spans", "start_time"),
        "span_errors": count("spans", "start_time", "AND status = 'error'"),
        "metric_points": count("metric_points", "point_time"),
    }

    written = 0
    for metric, value in values.items():
        written += _upsert(conn, day, app_id, metric, "", value)

    written += _rollup_dimensions(conn, app_id, day, start, end)
    return written


def _rollup_dimensions(conn, app_id: str, day: str, start: str, end: str) -> int:
    sources = (
        ("events_by_screen", "events", "screen", "screen IS NOT NULL", "ts"),
        ("events_by_name", "events", "name", "", "ts"),
        ("logs_by_level", "logs", "level", "", "ts"),
        ("crashes_by_fingerprint", "crashes", "fingerprint", "", "ts"),
        ("log_events_by_severity", "log_events", "severity", "", "event_time"),
    )
    written = 0
    for metric, table, column, predicate, time_column in sources:
        rows = conn.execute(
            f"""
            SELECT {column} AS dimension, COUNT(*) AS value
              FROM {table}
             WHERE app_id = ? AND {time_column} >= ? AND {time_column} < ? {('AND ' + predicate) if predicate else ''}
             GROUP BY {column}
            """,
            (app_id, start, end),
        ).fetchall()
        for row in rows:
            written += _upsert(conn, day, app_id, metric, str(row["dimension"]), float(row["value"]))
    return written


def _upsert(conn, day: str, app_id: str, metric: str, dimension: str, value: float) -> int:
    conn.execute(
        """
        INSERT INTO metrics_daily (day, app_id, metric, dimension, value)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (day, app_id, metric, dimension) DO UPDATE SET value = excluded.value
        """,
        (day, app_id, metric, dimension, value),
    )
    return 1


def backfill(db: Database, days: int, now: datetime | None = None) -> dict:
    """Fold the last `days` complete days. Used to seed rollups on a live DB."""
    moment = now or datetime.now(timezone.utc)
    folded = []
    for offset in range(days, 0, -1):
        day = (moment.date() - timedelta(days=offset)).strftime(DAY_FORMAT)
        result = rollup_day(db, day, now=moment)
        folded.append({"day": day, "rows": result["rows"]})
    return {"days": len(folded), "folded": folded}


# --------------------------------------------------------------------------- #
# Reading rollups
# --------------------------------------------------------------------------- #

def coverage(db: Database, app_id: str) -> tuple[str | None, str | None]:
    """(first_day, last_day) successfully rolled up for this app, or (None, None).

    Reads the job ledger rather than `metrics_daily` itself: a day can legitimately
    have no rows of its own, but a successful run for it is unambiguous evidence
    that the day was processed.
    """
    row = db.one(
        "SELECT MIN(day) AS first_day, MAX(day) AS last_day "
        "FROM job_runs WHERE job = 'rollup' AND ok = 1"
    )
    if row is None or row["last_day"] is None:
        return None, None
    return row["first_day"], row["last_day"]


def series(
    db: Database,
    app_id: str,
    metric: str,
    *,
    since: datetime,
    until: datetime,
    dimension: str = "",
) -> list[dict]:
    """Daily points for a metric from `metrics_daily`, inclusive of `since`'s day."""
    rows = db.query(
        """
        SELECT day AS bucket, value
          FROM metrics_daily
         WHERE app_id = ? AND metric = ? AND dimension = ?
           AND day >= ? AND day <= ?
         ORDER BY day ASC
        """,
        (
            app_id,
            metric,
            dimension,
            since.strftime(DAY_FORMAT),
            (until - timedelta(days=1)).strftime(DAY_FORMAT),
        ),
    )
    return [{"bucket": r["bucket"], "value": r["value"]} for r in rows]


def totals(db: Database, app_id: str, since: datetime, until: datetime) -> dict[str, float]:
    """Summed base metrics over a day-aligned, fully-covered range."""
    rows = db.query(
        """
        SELECT metric, SUM(value) AS total
          FROM metrics_daily
         WHERE app_id = ? AND dimension = ''
           AND day >= ? AND day <= ?
         GROUP BY metric
        """,
        (app_id, since.strftime(DAY_FORMAT), (until - timedelta(days=1)).strftime(DAY_FORMAT)),
    )
    return {row["metric"]: row["total"] for row in rows}


def recent_runs(db: Database, limit: int = 10) -> list[dict]:
    rows = db.query(
        "SELECT job, day, ran_at, ok, detail FROM job_runs ORDER BY id DESC LIMIT ?",
        (limit,),
    )
    return [dict(r) for r in rows]


def last_run(db: Database, job: str) -> dict | None:
    row = db.one(
        "SELECT job, day, ran_at, ok, detail FROM job_runs WHERE job = ? ORDER BY id DESC LIMIT 1",
        (job,),
    )
    return dict(row) if row else None


def ran_on(db: Database, job: str, wall_clock_day: str) -> bool:
    """Did `job` run successfully on this calendar date (YYYY-MM-DD)?

    Answers the scheduler's question — "has today's pass happened yet?" — using
    the wall clock, which is a different thing from [rolled_up].
    """
    return bool(
        db.scalar(
            "SELECT COUNT(*) FROM job_runs "
            "WHERE job = ? AND ok = 1 AND substr(ran_at, 1, 10) = ?",
            (job, wall_clock_day),
        )
    )


def rolled_up(db: Database, day: str) -> bool:
    """Was this *data* day folded into metrics_daily? Used by the read path."""
    return bool(
        db.scalar(
            "SELECT COUNT(*) FROM job_runs WHERE job = 'rollup' AND ok = 1 AND day = ?",
            (day,),
        )
    )
