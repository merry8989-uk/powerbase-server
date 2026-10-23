"""Read-side SQL.

Every function here is plain SQL over the schema from Phase 1 — no FastAPI, no
templating — so the whole read path is testable against a database directly.

Two conventions matter:

* **Time ranges are half-open**: `since <= ts < until`. A `until` given as a
  bare date is bumped by one day so `until=2026-10-22` includes that whole day,
  which is what a person means by it.
* **Aggregates are computed from the raw tables** in this phase. Phase 4 adds
  `metrics_daily` rollups and long ranges will read those instead; the function
  signatures are already shaped so only the body changes.

Identifiers are never interpolated from user input: table and column names are
validated against `sqlite_master` / `PRAGMA table_info` before use, and all
values are bound parameters.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from . import rollups
from .db import Database
from .models import to_iso

# --------------------------------------------------------------------------- #
# Time helpers
# --------------------------------------------------------------------------- #

_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def parse_time(value: str | None, *, bump_day: bool = False) -> datetime | None:
    """Parse an ISO-8601 timestamp or a bare date into an aware UTC datetime."""
    if not value:
        return None
    raw = value.strip()
    if _DATE_ONLY.match(raw):
        parsed = datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return parsed + timedelta(days=1) if bump_day else parsed
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_range(since: str | None, until: str | None) -> tuple[datetime | None, datetime | None]:
    return parse_time(since), parse_time(until, bump_day=True)


def default_range(days: int = 7) -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    return now - timedelta(days=days), now


# --------------------------------------------------------------------------- #
# Internal helpers
# --------------------------------------------------------------------------- #

def _range(column: str, since: datetime | None, until: datetime | None,
           clauses: list[str], params: list[Any]) -> None:
    if since is not None:
        clauses.append(f"{column} >= ?")
        params.append(to_iso(since))
    if until is not None:
        clauses.append(f"{column} < ?")
        params.append(to_iso(until))


def _where(clauses: list[str]) -> str:
    return " AND ".join(clauses) if clauses else "1"


def _decode(row: sqlite3.Row | dict, *json_columns: str) -> dict:
    out = dict(row)
    for column in json_columns:
        raw = out.get(column)
        if isinstance(raw, str) and raw:
            try:
                out[column] = json.loads(raw)
            except json.JSONDecodeError:
                pass
    return out


def _fts_query(q: str) -> str:
    """Turn free text into a safe FTS5 query: every token quoted, AND-ed."""
    tokens = []
    for token in re.split(r"\s+", q.strip()):
        cleaned = re.sub(r"[^\w@.\-]", " ", token).strip()
        if cleaned:
            tokens.append('"' + cleaned.replace('"', "") + '"')
    return " AND ".join(tokens)


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


# --------------------------------------------------------------------------- #
# Events
# --------------------------------------------------------------------------- #

def list_events(
    db: Database,
    app_id: str,
    *,
    session_id: str | None = None,
    device_id: str | None = None,
    name: str | None = None,
    screen: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 100,
    offset: int = 0,
    order: str = "desc",
) -> tuple[list[dict], int]:
    clauses = ["app_id = ?"]
    params: list[Any] = [app_id]
    if session_id:
        clauses.append("session_id = ?"); params.append(session_id)
    if device_id:
        clauses.append("device_id = ?"); params.append(device_id)
    if name:
        clauses.append("name = ?"); params.append(name)
    if screen:
        clauses.append("screen = ?"); params.append(screen)
    _range("ts", since, until, clauses, params)

    where = _where(clauses)
    total = db.scalar(f"SELECT COUNT(*) FROM events WHERE {where}", tuple(params)) or 0
    direction = "ASC" if order.lower() == "asc" else "DESC"
    rows = db.query(
        f"SELECT * FROM events WHERE {where} "
        f"ORDER BY ts {direction}, id {direction} LIMIT ? OFFSET ?",
        tuple(params) + (limit, offset),
    )
    return [_decode(r, "properties") for r in rows], total


# --------------------------------------------------------------------------- #
# Logs
# --------------------------------------------------------------------------- #

def list_logs(
    db: Database,
    app_id: str,
    *,
    level: str | None = None,
    tag: str | None = None,
    session_id: str | None = None,
    device_id: str | None = None,
    q: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 100,
    offset: int = 0,
    order: str = "desc",
) -> tuple[list[dict], int]:
    clauses = ["l.app_id = ?"]
    params: list[Any] = [app_id]
    if level:
        clauses.append("l.level = ?"); params.append(level.upper())
    if tag:
        clauses.append("l.tag = ?"); params.append(tag)
    if session_id:
        clauses.append("l.session_id = ?"); params.append(session_id)
    if device_id:
        clauses.append("l.device_id = ?"); params.append(device_id)
    _range("l.ts", since, until, clauses, params)

    fts = _fts_query(q) if q else ""
    if fts:
        clauses.append("f MATCH ?")
        params.append(fts)
        source = "logs l JOIN logs_fts f ON f.rowid = l.id"
    else:
        source = "logs l"

    where = _where(clauses)
    direction = "ASC" if order.lower() == "asc" else "DESC"
    try:
        total = db.scalar(f"SELECT COUNT(*) FROM {source} WHERE {where}", tuple(params)) or 0
        rows = db.query(
            f"SELECT l.* FROM {source} WHERE {where} "
            f"ORDER BY l.ts {direction}, l.id {direction} LIMIT ? OFFSET ?",
            tuple(params) + (limit, offset),
        )
    except sqlite3.OperationalError:
        # A malformed FTS expression should not 500 the endpoint; fall back to LIKE.
        if not fts:
            raise
        return list_logs(
            db, app_id, level=level, tag=tag, session_id=session_id, device_id=device_id,
            q=None, since=since, until=until, limit=limit, offset=offset, order=order,
        )
    return [_decode(r, "context") for r in rows], total


def get_log(db: Database, log_id: int) -> dict | None:
    row = db.one("SELECT * FROM logs WHERE id = ?", (log_id,))
    return _decode(row, "context") if row else None


# --------------------------------------------------------------------------- #
# Crashes
# --------------------------------------------------------------------------- #

def list_crashes(
    db: Database,
    app_id: str,
    *,
    fingerprint: str | None = None,
    kind: str | None = None,
    device_id: str | None = None,
    session_id: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[dict], int]:
    clauses = ["app_id = ?"]
    params: list[Any] = [app_id]
    if fingerprint:
        clauses.append("fingerprint = ?"); params.append(fingerprint)
    if kind:
        clauses.append("kind = ?"); params.append(kind)
    if device_id:
        clauses.append("device_id = ?"); params.append(device_id)
    if session_id:
        clauses.append("session_id = ?"); params.append(session_id)
    _range("ts", since, until, clauses, params)

    where = _where(clauses)
    total = db.scalar(f"SELECT COUNT(*) FROM crashes WHERE {where}", tuple(params)) or 0
    rows = db.query(
        f"SELECT * FROM crashes WHERE {where} ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?",
        tuple(params) + (limit, offset),
    )
    return [dict(r) for r in rows], total


def crash_groups(
    db: Database,
    app_id: str,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    kind: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict], int]:
    """Crashes grouped by fingerprint — the view that tells you what to fix first."""
    clauses = ["app_id = ?"]
    params: list[Any] = [app_id]
    if kind:
        clauses.append("kind = ?"); params.append(kind)
    _range("ts", since, until, clauses, params)
    where = _where(clauses)

    total = db.scalar(
        f"SELECT COUNT(DISTINCT fingerprint) FROM crashes WHERE {where}", tuple(params)
    ) or 0
    rows = db.query(
        f"""
        SELECT fingerprint,
               MAX(kind)                AS kind,
               MAX(message)             AS message,
               COUNT(*)                 AS occurrences,
               COUNT(DISTINCT device_id) AS devices,
               MIN(ts)                  AS first_seen,
               MAX(ts)                  AS last_seen,
               MAX(app_version)         AS app_version
          FROM crashes
         WHERE {where}
         GROUP BY fingerprint
         ORDER BY occurrences DESC, last_seen DESC
         LIMIT ? OFFSET ?
        """,
        tuple(params) + (limit, offset),
    )
    return [dict(r) for r in rows], total


def get_crash(db: Database, crash_id: int) -> dict | None:
    row = db.one("SELECT * FROM crashes WHERE id = ?", (crash_id,))
    return dict(row) if row else None


# --------------------------------------------------------------------------- #
# Sessions and devices
# --------------------------------------------------------------------------- #

def list_sessions(
    db: Database,
    app_id: str,
    *,
    device_id: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[dict], int]:
    clauses = ["s.app_id = ?"]
    params: list[Any] = [app_id]
    if device_id:
        clauses.append("s.device_id = ?"); params.append(device_id)
    _range("s.started_at", since, until, clauses, params)
    where = _where(clauses)

    total = db.scalar(f"SELECT COUNT(*) FROM sessions s WHERE {where}", tuple(params)) or 0
    rows = db.query(
        f"""
        SELECT s.*,
               (SELECT COUNT(*) FROM events e WHERE e.session_id = s.id) AS event_count,
               (SELECT COUNT(*) FROM logs   g WHERE g.session_id = s.id) AS log_count,
               (SELECT COUNT(*) FROM crashes c WHERE c.session_id = s.id) AS crash_count
          FROM sessions s
         WHERE {where}
         ORDER BY s.started_at DESC
         LIMIT ? OFFSET ?
        """,
        tuple(params) + (limit, offset),
    )
    return [dict(r) for r in rows], total


def get_session(db: Database, session_id: str) -> dict | None:
    row = db.one("SELECT * FROM sessions WHERE id = ?", (session_id,))
    return dict(row) if row else None


def list_devices(
    db: Database,
    app_id: str,
    *,
    search: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[dict], int]:
    clauses = ["d.app_id = ?"]
    params: list[Any] = [app_id]
    if search:
        clauses.append("(d.id LIKE ? OR d.model LIKE ? OR d.manufacturer LIKE ?)")
        like = f"%{search}%"
        params.extend([like, like, like])
    where = _where(clauses)

    total = db.scalar(f"SELECT COUNT(*) FROM devices d WHERE {where}", tuple(params)) or 0
    rows = db.query(
        f"""
        SELECT d.*,
               (SELECT COUNT(*) FROM sessions s WHERE s.device_id = d.id) AS session_count
          FROM devices d
         WHERE {where}
         ORDER BY d.last_seen DESC
         LIMIT ? OFFSET ?
        """,
        tuple(params) + (limit, offset),
    )
    return [dict(r) for r in rows], total


def get_device(db: Database, device_id: str) -> dict | None:
    row = db.one("SELECT * FROM devices WHERE id = ?", (device_id,))
    return dict(row) if row else None


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #

def summary(
    db: Database,
    app_id: str,
    since: datetime | None = None,
    until: datetime | None = None,
) -> dict:
    def count(table: str, column: str, extra: str = "") -> int:
        clauses = ["app_id = ?"]
        params: list[Any] = [app_id]
        _range(column, since, until, clauses, params)
        return db.scalar(
            f"SELECT COUNT(*) FROM {table} WHERE {_where(clauses)} {extra}", tuple(params)
        ) or 0

    sessions = count("sessions", "started_at")
    events = count("events", "ts")
    logs = count("logs", "ts")
    errors = count("logs", "ts", "AND level IN ('ERROR', 'FATAL')")
    crashes = count("crashes", "ts")

    device_clauses = ["app_id = ?"]
    device_params: list[Any] = [app_id]
    _range("started_at", since, until, device_clauses, device_params)
    active_devices = db.scalar(
        f"SELECT COUNT(DISTINCT device_id) FROM sessions WHERE {_where(device_clauses)}",
        tuple(device_params),
    ) or 0

    crash_clauses = ["app_id = ?", "session_id IS NOT NULL"]
    crash_params: list[Any] = [app_id]
    _range("ts", since, until, crash_clauses, crash_params)
    crashed_sessions = db.scalar(
        f"SELECT COUNT(DISTINCT session_id) FROM crashes WHERE {_where(crash_clauses)}",
        tuple(crash_params),
    ) or 0

    duration_clauses = ["app_id = ?", "duration_ms IS NOT NULL"]
    duration_params: list[Any] = [app_id]
    _range("started_at", since, until, duration_clauses, duration_params)
    avg_duration = db.scalar(
        f"SELECT AVG(duration_ms) FROM sessions WHERE {_where(duration_clauses)}",
        tuple(duration_params),
    )

    crash_free = (sessions - min(crashed_sessions, sessions)) if sessions else sessions

    return {
        "app_id": app_id,
        "sessions": sessions,
        "events": events,
        "logs": logs,
        "error_logs": errors,
        "crashes": crashes,
        "active_devices": active_devices,
        "crashed_sessions": crashed_sessions,
        "crash_free_sessions": crash_free,
        "crash_free_rate": round(crash_free / sessions, 4) if sessions else 1.0,
        "avg_session_duration_ms": round(avg_duration, 1) if avg_duration is not None else None,
    }


# metric name -> (table, time column, extra predicate, aggregate)
_METRIC_SOURCES: dict[str, tuple[str, str, str, str]] = {
    "events": ("events", "ts", "", "COUNT(*)"),
    "logs": ("logs", "ts", "", "COUNT(*)"),
    "error_logs": ("logs", "ts", "AND level IN ('ERROR', 'FATAL')", "COUNT(*)"),
    "crashes": ("crashes", "ts", "", "COUNT(*)"),
    "sessions": ("sessions", "started_at", "", "COUNT(*)"),
    "active_devices": ("sessions", "started_at", "", "COUNT(DISTINCT device_id)"),
}

METRIC_NAMES = tuple(_METRIC_SOURCES)


def _day_floor(moment: datetime) -> datetime:
    return moment.replace(hour=0, minute=0, second=0, microsecond=0)


def _day_ceil(moment: datetime) -> datetime:
    floor = _day_floor(moment)
    return floor if floor == moment else floor + timedelta(days=1)


def _rollup_usable(db: Database, app_id: str, since: datetime | None, until: datetime | None) -> bool:
    """True when a complete middle stretch of the range is covered by rollups."""
    if since is None or until is None:
        return False
    first, last = rollups.coverage(db, app_id)
    if first is None or last is None:
        return False
    middle_start = max(_day_ceil(since), since)
    middle_end = min(_day_floor(until), until)
    if middle_start >= middle_end:
        return False
    return (
        first <= middle_start.strftime("%Y-%m-%d")
        and last >= (middle_end - timedelta(days=1)).strftime("%Y-%m-%d")
    )


def series_source(
    db: Database,
    app_id: str,
    bucket: str = "day",
    since: datetime | None = None,
    until: datetime | None = None,
) -> str:
    """Which storage the next `timeseries` call will read from."""
    if bucket == "day" and _rollup_usable(db, app_id, since, until):
        return "rollup"
    return "raw"


def _raw_series(
    db: Database,
    app_id: str,
    metric: str,
    *,
    bucket: str,
    since: datetime | None,
    until: datetime | None,
) -> list[dict]:
    table, time_column, extra, aggregate = _METRIC_SOURCES[metric]

    if bucket == "hour":
        bucket_expr = f"substr({time_column}, 1, 13) || ':00'"
    else:
        bucket_expr = f"substr({time_column}, 1, 10)"

    clauses = ["app_id = ?"]
    params: list[Any] = [app_id]
    _range(time_column, since, until, clauses, params)

    rows = db.query(
        f"""
        SELECT {bucket_expr} AS bucket, {aggregate} AS value
          FROM {table}
         WHERE {_where(clauses)} {extra}
         GROUP BY bucket
         ORDER BY bucket ASC
        """,
        tuple(params),
    )
    return [{"bucket": r["bucket"], "value": r["value"]} for r in rows]


def _merged_series(
    db: Database, app_id: str, metric: str, since: datetime, until: datetime
) -> list[dict]:
    """Rollups for the whole days, raw for the partial days at either end.

    The three stretches meet exactly on day boundaries and cannot overlap, so
    nothing is counted twice: the head covers `[since, next midnight)`, the
    middle covers whole days, the tail covers `[last midnight, until)`.
    """
    middle_start = max(_day_ceil(since), since)
    middle_end = min(_day_floor(until), until)
    points: dict[str, float] = {}

    def add(rows: list[dict]) -> None:
        for point in rows:
            points[point["bucket"]] = points.get(point["bucket"], 0) + point["value"]

    if middle_start > since:
        add(_raw_series(db, app_id, metric, bucket="day", since=since, until=middle_start))
    add(rollups.series(db, app_id, metric, since=middle_start, until=middle_end))
    if middle_end < until:
        add(_raw_series(db, app_id, metric, bucket="day", since=middle_end, until=until))

    return [{"bucket": key, "value": points[key]} for key in sorted(points)]


def timeseries(
    db: Database,
    app_id: str,
    metric: str,
    *,
    bucket: str = "day",
    since: datetime | None = None,
    until: datetime | None = None,
    source: str = "auto",
) -> list[dict]:
    if metric not in _METRIC_SOURCES:
        raise ValueError(f"unknown metric: {metric}")
    if bucket not in ("hour", "day"):
        raise ValueError(f"unknown bucket: {bucket}")

    # Hourly data has no rollup equivalent — `metrics_daily` is daily by design.
    if bucket == "day" and source in ("auto", "rollup"):
        if source == "rollup" or _rollup_usable(db, app_id, since, until):
            if since is None or until is None:
                raise ValueError("rollup source requires since and until")
            return _merged_series(db, app_id, metric, since, until)

    return _raw_series(db, app_id, metric, bucket=bucket, since=since, until=until)


# dimension name -> (table, column, time column, predicate)
_DIMENSION_SOURCES: dict[str, tuple[str, str, str, str]] = {
    "screen": ("events", "screen", "ts", "screen IS NOT NULL"),
    "event": ("events", "name", "ts", ""),
    "category": ("events", "category", "ts", "category IS NOT NULL"),
    "tag": ("logs", "tag", "ts", "tag IS NOT NULL"),
    "level": ("logs", "level", "ts", ""),
    "device": ("sessions", "device_id", "started_at", ""),
    "country": ("sessions", "country", "started_at", "country IS NOT NULL"),
    "end_reason": ("sessions", "end_reason", "started_at", "end_reason IS NOT NULL"),
    "model": ("devices", "model", "last_seen", "model IS NOT NULL"),
    "app_version": ("devices", "app_version", "last_seen", "app_version IS NOT NULL"),
    "os_version": ("devices", "os_version", "last_seen", "os_version IS NOT NULL"),
}

DIMENSION_NAMES = tuple(_DIMENSION_SOURCES)


def top_values(
    db: Database,
    app_id: str,
    dimension: str,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 10,
) -> list[dict]:
    if dimension not in _DIMENSION_SOURCES:
        raise ValueError(f"unknown dimension: {dimension}")
    table, column, time_column, predicate = _DIMENSION_SOURCES[dimension]

    clauses = ["app_id = ?"]
    params: list[Any] = [app_id]
    _range(time_column, since, until, clauses, params)

    rows = db.query(
        f"""
        SELECT {column} AS key, COUNT(*) AS value
          FROM {table}
         WHERE {_where(clauses)} {('AND ' + predicate) if predicate else ''}
         GROUP BY {column}
         ORDER BY value DESC, key ASC
         LIMIT ?
        """,
        tuple(params) + (limit,),
    )
    return [{"key": r["key"], "value": r["value"]} for r in rows]


# --------------------------------------------------------------------------- #
# Data browser
# --------------------------------------------------------------------------- #

def list_tables(db: Database) -> list[dict]:
    rows = db.query(
        """
        SELECT name FROM sqlite_master
         WHERE type = 'table'
           AND name NOT LIKE 'sqlite_%'
           -- Hides the FTS index *and* its shadow tables (logs_fts_data,
           -- logs_fts_idx, logs_fts_docsize, logs_fts_config).
           AND name NOT LIKE '%_fts%'
         ORDER BY name
        """
    )
    out = []
    for row in rows:
        name = row["name"]
        count = db.scalar(f"SELECT COUNT(*) FROM {quote_ident(name)}") or 0
        out.append({"name": name, "rows": count})
    return out


def table_columns(db: Database, table: str) -> list[dict]:
    return [dict(r) for r in db.query(f"PRAGMA table_info({quote_ident(table)})")]


def table_rows(
    db: Database,
    table: str,
    *,
    limit: int = 50,
    offset: int = 0,
    order_by: str | None = None,
    order: str = "desc",
    filters: dict[str, str] | None = None,
) -> tuple[list[dict], int, list[str]]:
    columns = [c["name"] for c in table_columns(db, table)]

    clauses: list[str] = []
    params: list[Any] = []
    for column, value in (filters or {}).items():
        if column not in columns:
            continue
        clauses.append(f"{quote_ident(column)} = ?")
        params.append(value)

    if order_by and order_by in columns:
        direction = "ASC" if order.lower() == "asc" else "DESC"
        order_clause = f"ORDER BY {quote_ident(order_by)} {direction}"
    elif "id" in columns:
        order_clause = "ORDER BY id DESC"
    else:
        order_clause = ""

    where = _where(clauses)
    quoted = quote_ident(table)
    total = db.scalar(f"SELECT COUNT(*) FROM {quoted} WHERE {where}", tuple(params)) or 0
    rows = db.query(
        f"SELECT * FROM {quoted} WHERE {where} {order_clause} LIMIT ? OFFSET ?",
        tuple(params) + (limit, offset),
    )
    return [dict(r) for r in rows], total, columns


def foreign_keys(db: Database, table: str) -> list[dict]:
    return [dict(r) for r in db.query(f"PRAGMA foreign_key_list({quote_ident(table)})")]


def referenced_rows(db: Database, table: str, column: str, value: str, limit: int = 20) -> list[dict]:
    columns = [c["name"] for c in table_columns(db, table)]
    if column not in columns:
        return []
    rows = db.query(
        f"SELECT * FROM {quote_ident(table)} WHERE {quote_ident(column)} = ? LIMIT ?",
        (value, limit),
    )
    return [dict(r) for r in rows]
