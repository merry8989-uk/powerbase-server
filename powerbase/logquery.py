"""Validated querying over ``log_events``.

The rule this module exists to enforce: **no client string ever reaches SQL as
SQL.** A filter field is looked up in an allowlist, an operator must be one the
field's type supports, and every value is a bound parameter. A request naming an
unknown field, an operator the type does not allow, or a value of the wrong type
is refused, not silently ignored — a filter that quietly does nothing is worse
than an error, because the results look plausible.

Pagination is keyset, not offset. ``LIMIT/OFFSET`` re-scans everything before the
offset and can skip or repeat rows when new events land mid-pagination; a cursor
over ``(event_time, id)`` does neither.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from .db import Database, QueryTimeout
from .models import to_iso

MAX_LIMIT = 500
DEFAULT_LIMIT = 100
MAX_RANGE_DAYS = 92
QUERY_TIMEOUT_MS = 5000
MAX_FILTERS = 20
MAX_IN_VALUES = 50
MAX_VALUE_CHARS = 256

#: Field name -> type. The *name* is the only thing that ever becomes a column
#: reference, and it comes from this dict, never from the request body.
FIELDS: dict[str, str] = {
    "severity": "text",
    "source": "text",
    "event_type": "text",
    "service_name": "text",
    "environment": "text",
    "app_version": "text",
    "release_id": "text",
    "route_template": "text",
    "http_method": "text",
    "trace_id": "text",
    "request_id": "text",
    "span_id": "text",
    "error_code": "text",
    "error_fingerprint": "text",
    "status_code": "int",
    "duration_ms": "int",
    "message": "text",
    # Added in migration 0009.
    "category": "text",
    "source_type": "text",
    "source_id": "text",
    "platform_version": "text",
}

TEXT_OPERATORS = frozenset({"eq", "neq", "contains", "prefix", "suffix", "in"})
INT_OPERATORS = frozenset({"eq", "neq", "gt", "gte", "lt", "lte", "in"})

SORT_FIELDS = frozenset(
    {
        "event_time",
        "received_at",
        "severity",
        "duration_ms",
        "status_code",
        # The specification sorts by `timestamp DESC, id DESC`. `id` is the
        # stable tie-breaker, and the keyset cursor already orders on it.
        "id",
    }
)

#: Bucket name -> how many characters of the ISO timestamp to keep.
BUCKETS = {"hour": 13, "day": 10}


class QueryError(ValueError):
    """A client-supplied query we refuse to run. Surfaces as HTTP 400."""


@dataclass(frozen=True)
class Filter:
    field: str
    op: str
    value: Any


@dataclass
class Page:
    items: list[dict] = field(default_factory=list)
    next_cursor: str | None = None
    limit: int = DEFAULT_LIMIT
    truncated: bool = False
    elapsed_ms: int = 0


# --------------------------------------------------------------------------- #
# Parsing and validation
# --------------------------------------------------------------------------- #
def _coerce_scalar(field_name: str, kind: str, value: Any) -> Any:
    if kind == "int":
        if isinstance(value, bool):
            raise QueryError(f"'{field_name}' expects an integer")
        if isinstance(value, int):
            return value
        try:
            return int(value)
        except (TypeError, ValueError):
            raise QueryError(f"'{field_name}' expects an integer") from None
    if not isinstance(value, str):
        value = str(value)
    if len(value) > MAX_VALUE_CHARS:
        raise QueryError(f"'{field_name}' value is longer than {MAX_VALUE_CHARS} characters")
    return value


def _coerce(field_name: str, op: str, value: Any) -> Any:
    kind = FIELDS[field_name]
    if op == "in":
        if not isinstance(value, (list, tuple)) or not value:
            raise QueryError(f"'{field_name}' with operator 'in' needs a non-empty list")
        if len(value) > MAX_IN_VALUES:
            raise QueryError(f"'in' accepts at most {MAX_IN_VALUES} values")
        return [_coerce_scalar(field_name, kind, item) for item in value]
    return _coerce_scalar(field_name, kind, value)


def parse_filters(spec: Any) -> list[Filter]:
    """Validate the roadmap's ``filters`` array into typed Filters."""
    if spec in (None, [], {}):
        return []
    if not isinstance(spec, list):
        raise QueryError("'filters' must be a list")
    if len(spec) > MAX_FILTERS:
        raise QueryError(f"at most {MAX_FILTERS} filters are accepted")

    out: list[Filter] = []
    for index, item in enumerate(spec):
        if not isinstance(item, dict):
            raise QueryError(f"filter {index} must be an object")
        unknown = set(item) - {"field", "operator", "value"}
        if unknown:
            raise QueryError(f"filter {index} has unknown keys: {sorted(unknown)}")
        name = item.get("field")
        if name not in FIELDS:
            raise QueryError(f"filter {index} names an unknown field: {name!r}")
        op = item.get("operator", "eq")
        allowed = TEXT_OPERATORS if FIELDS[name] == "text" else INT_OPERATORS
        if op not in allowed:
            raise QueryError(f"operator {op!r} is not valid for {name!r}")
        if "value" not in item:
            raise QueryError(f"filter {index} has no value")
        out.append(Filter(name, op, _coerce(name, op, item["value"])))
    return out


#: The Logs Explorer specification calls the event time `timestamp`. The column
#: is `event_time`. Accepting the specification's name costs nothing and saves
#: every reader of that document a debugging session.
SORT_ALIASES = {
    "timestamp": "event_time",
    "time": "event_time",
    "received": "received_at",
    "severity_level": "severity",
}

#: The longest window each unit may express, so `"99999d"` is refused rather
#: than turning into a scan of everything.
MAX_WINDOW_UNITS = {"m": 24 * 60, "h": 24 * 90, "d": 92, "w": 13}

#: `"7d"`, `"24h"`, `"30m"`, `"2w"` — the specification's default is `"7d"`.
_RELATIVE_WINDOW = re.compile(r"^(\d+)\s*([mhdw])$", re.IGNORECASE)
_WINDOW_UNITS = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}


def parse_relative_window(
    value: Any, now: datetime | None = None
) -> tuple[str, str]:
    """Resolve ``"7d"`` into concrete half-open UTC bounds.

    The end of the window is the server's clock, not the client's — a client
    with a wrong clock must not be able to ask for a window in the future.
    """
    if not isinstance(value, str):
        raise QueryError("'time_range' must be a string like '7d' or '24h'")
    match = _RELATIVE_WINDOW.match(value.strip())
    if match is None:
        raise QueryError(
            "time_range must look like '30m', '24h', '7d' or '2w'"
        )
    amount, unit = int(match.group(1)), match.group(2).lower()
    if amount < 1:
        raise QueryError("time_range must be at least 1 unit")
    if amount > MAX_WINDOW_UNITS[unit]:
        raise QueryError(
            f"time_range is capped at {MAX_WINDOW_UNITS[unit]}{unit}"
        )

    moment = now or datetime.now(timezone.utc)
    since = moment - timedelta(**{_WINDOW_UNITS[unit]: amount})
    return to_iso(since), to_iso(moment)


def parse_sort(spec: Any) -> tuple[str, str]:
    """Validate ``{"field": ..., "direction": ...}``."""
    if not spec:
        return "event_time", "desc"
    if not isinstance(spec, dict):
        raise QueryError("'sort' must be an object")
    unknown = set(spec) - {"field", "direction"}
    if unknown:
        raise QueryError(f"'sort' has unknown keys: {sorted(unknown)}")
    name = spec.get("field", "event_time")
    if isinstance(name, str):
        name = SORT_ALIASES.get(name.lower(), name)
    if name not in SORT_FIELDS:
        raise QueryError(f"cannot sort by {name!r}")
    direction = str(spec.get("direction", "desc")).lower()
    if direction not in {"asc", "desc"}:
        raise QueryError("'direction' must be 'asc' or 'desc'")
    return name, direction


def clamp_limit(limit: int | None) -> int:
    if limit is None:
        return DEFAULT_LIMIT
    try:
        value = int(limit)
    except (TypeError, ValueError):
        raise QueryError("'limit' must be an integer") from None
    if value < 1:
        raise QueryError("'limit' must be at least 1")
    return min(value, MAX_LIMIT)


# --------------------------------------------------------------------------- #
# Cursors
# --------------------------------------------------------------------------- #
def encode_cursor(event_time: str, row_id: int) -> str:
    raw = f"{event_time}|{row_id}".encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> tuple[str, int]:
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise QueryError("malformed cursor") from None
    timestamp, separator, ident = raw.rpartition("|")
    if not separator or not timestamp:
        raise QueryError("malformed cursor")
    try:
        return timestamp, int(ident)
    except ValueError:
        raise QueryError("malformed cursor") from None


# --------------------------------------------------------------------------- #
# SQL construction
# --------------------------------------------------------------------------- #
def _like_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _clause(item: Filter) -> tuple[str, list[Any]]:
    # `item.field` came from FIELDS, so it is one of a fixed set of column names.
    column = item.field
    op = item.op
    if op == "eq":
        return f"{column} = ?", [item.value]
    if op == "neq":
        # NULL-inclusive, because `!=` silently excluding NULL rows surprises
        # people and looks like a missing-data bug.
        return f"({column} IS NULL OR {column} != ?)", [item.value]
    if op == "gt":
        return f"{column} > ?", [item.value]
    if op == "gte":
        return f"{column} >= ?", [item.value]
    if op == "lt":
        return f"{column} < ?", [item.value]
    if op == "lte":
        return f"{column} <= ?", [item.value]
    if op == "in":
        marks = ",".join("?" * len(item.value))
        return f"{column} IN ({marks})", list(item.value)
    if op == "contains":
        return f"{column} LIKE ? ESCAPE '\\'", [f"%{_like_escape(item.value)}%"]
    if op == "prefix":
        return f"{column} LIKE ? ESCAPE '\\'", [f"{_like_escape(item.value)}%"]
    if op == "suffix":
        return f"{column} LIKE ? ESCAPE '\\'", [f"%{_like_escape(item.value)}"]
    raise QueryError(f"unsupported operator {op!r}")  # pragma: no cover


def build_where(
    *,
    app_id: str,
    since: str | None,
    until: str | None,
    filters: list[Filter],
    q: str | None,
) -> tuple[str, list[Any]]:
    clauses = ["app_id = ?"]
    params: list[Any] = [app_id]

    if since:
        clauses.append("event_time >= ?")
        params.append(since)
    if until:
        clauses.append("event_time < ?")
        params.append(until)

    for item in filters:
        sql, values = _clause(item)
        clauses.append(sql)
        params.extend(values)

    if q:
        # Free-text search stays inside the allowlist: it only ever touches these
        # four columns, and the pattern is a bound parameter.
        like = f"%{_like_escape(q)}%"
        clauses.append(
            "(message LIKE ? ESCAPE '\\' OR error_code LIKE ? ESCAPE '\\' "
            "OR error_fingerprint LIKE ? ESCAPE '\\' "
            "OR route_template LIKE ? ESCAPE '\\')"
        )
        params.extend([like, like, like, like])

    return " AND ".join(clauses), params


def _decode_row(row) -> dict:
    out = dict(row)
    raw = out.get("attributes")
    if isinstance(raw, str):
        try:
            out["attributes"] = json.loads(raw)
        except json.JSONDecodeError:
            out["attributes"] = {}
    return out


# --------------------------------------------------------------------------- #
# Queries
# --------------------------------------------------------------------------- #
def query_log_events(
    db: Database,
    *,
    app_id: str,
    since: str | None = None,
    until: str | None = None,
    filters: list[Filter] | None = None,
    q: str | None = None,
    sort: str = "event_time",
    direction: str = "desc",
    limit: int = DEFAULT_LIMIT,
    cursor: str | None = None,
    timeout_ms: int = QUERY_TIMEOUT_MS,
) -> Page:
    filters = filters or []
    if sort not in SORT_FIELDS:
        raise QueryError(f"cannot sort by {sort!r}")
    if direction not in {"asc", "desc"}:
        raise QueryError("'direction' must be 'asc' or 'desc'")
    limit = clamp_limit(limit)

    where, params = build_where(
        app_id=app_id, since=since, until=until, filters=filters, q=q
    )

    if cursor:
        cursor_ts, cursor_id = decode_cursor(cursor)
        if direction == "desc":
            where += " AND (event_time < ? OR (event_time = ? AND id < ?))"
        else:
            where += " AND (event_time > ? OR (event_time = ? AND id > ?))"
        params.extend([cursor_ts, cursor_ts, cursor_id])

    # `sort` and `direction` are both from a fixed set, so this interpolation
    # cannot carry client input. `id` is the tiebreaker that makes the ordering
    # total, which is what makes the cursor exact.
    sql = (
        f"SELECT * FROM log_events WHERE {where} "
        f"ORDER BY {sort} {direction.upper()}, id {direction.upper()} "
        f"LIMIT ?"
    )
    params.append(limit + 1)  # one extra row tells us whether another page exists

    started = time.monotonic()
    try:
        rows = db.query_timed(sql, tuple(params), timeout_ms=timeout_ms)
    except QueryTimeout:
        raise QueryError(
            f"the query exceeded its {timeout_ms} ms budget; "
            "narrow the time range or add a filter"
        ) from None
    elapsed_ms = int((time.monotonic() - started) * 1000)

    truncated = len(rows) > limit
    rows = rows[:limit]

    next_cursor = None
    if truncated and rows:
        last = rows[-1]
        next_cursor = encode_cursor(last["event_time"], last["id"])

    return Page(
        items=[_decode_row(row) for row in rows],
        next_cursor=next_cursor,
        limit=limit,
        truncated=truncated,
        elapsed_ms=elapsed_ms,
    )


def log_event_stats(
    db: Database,
    *,
    app_id: str,
    since: str | None = None,
    until: str | None = None,
    filters: list[Filter] | None = None,
    q: str | None = None,
    bucket: str = "hour",
    timeout_ms: int = QUERY_TIMEOUT_MS,
) -> dict:
    """Volume over time plus a severity breakdown, for the same filter set."""
    if bucket not in BUCKETS:
        raise QueryError(f"bucket must be one of {sorted(BUCKETS)}")
    width = BUCKETS[bucket]

    where, params = build_where(
        app_id=app_id, since=since, until=until, filters=filters or [], q=q
    )

    started = time.monotonic()
    try:
        rows = db.query_timed(
            f"""
            SELECT substr(event_time, 1, {width}) AS bucket,
                   COUNT(*) AS count,
                   SUM(CASE WHEN severity IN ('ERROR', 'FATAL') THEN 1 ELSE 0 END) AS errors
            FROM log_events
            WHERE {where}
            GROUP BY bucket
            ORDER BY bucket
            """,
            tuple(params),
            timeout_ms=timeout_ms,
        )
        severity_rows = db.query_timed(
            f"""
            SELECT severity, COUNT(*) AS count
            FROM log_events
            WHERE {where}
            GROUP BY severity
            ORDER BY count DESC
            """,
            tuple(params),
            timeout_ms=timeout_ms,
        )
    except QueryTimeout:
        raise QueryError(
            f"the query exceeded its {timeout_ms} ms budget; narrow the time range"
        ) from None
    elapsed_ms = int((time.monotonic() - started) * 1000)

    buckets = [
        {"bucket": row["bucket"], "count": row["count"], "errors": row["errors"] or 0}
        for row in rows
    ]
    return {
        "bucket": bucket,
        "buckets": buckets,
        "by_severity": {row["severity"]: row["count"] for row in severity_rows},
        "total": sum(item["count"] for item in buckets),
        "elapsed_ms": elapsed_ms,
    }


def get_log_event(db: Database, app_id: str, event_id: str) -> dict | None:
    row = db.one(
        "SELECT * FROM log_events WHERE app_id = ? AND event_id = ?", (app_id, event_id)
    )
    return _decode_row(row) if row is not None else None


def related_events(db: Database, app_id: str, event: dict, limit: int = 20) -> list[dict]:
    """Events sharing a trace, or failing with the same fingerprint.

    This is the correlation the roadmap asks for: one error is rarely interesting
    on its own, but the other rows on the same trace usually explain it.
    """
    clauses = ["app_id = ?", "id != ?"]
    params: list[Any] = [app_id, event["id"]]
    links = []
    if event.get("trace_id"):
        links.append("trace_id = ?")
        params.append(event["trace_id"])
    if event.get("error_fingerprint"):
        links.append("error_fingerprint = ?")
        params.append(event["error_fingerprint"])

    if not links:
        return []

    rows = db.query(
        f"SELECT * FROM log_events WHERE {' AND '.join(clauses)} "
        f"AND ({' OR '.join(links)}) ORDER BY event_time DESC LIMIT ?",
        tuple(params) + (clamp_limit(limit),),
    )
    return [_decode_row(row) for row in rows]
