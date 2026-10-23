"""Spans, traces, and RED metrics.

A trace is what turns a list of errors into an explanation: the same
`trace_id` is on the log events *and* on the spans, so "this request was slow"
and "this request logged a timeout" become one story.

RED — **R**ate, **E**rrors, **D**uration — is the smallest set of numbers that
tells you whether a service is healthy, and it is derived here from spans rather
than asked of the client. A service that sends spans gets RED for free.
"""

from __future__ import annotations

import json
from typing import Any

from .db import Database
from .migrations import utcnow_iso

MAX_LIMIT = 500


def _redact_row(row: dict, enabled: bool) -> dict:
    from .redaction import redact_text, redact_value

    if not enabled:
        return row
    out = dict(row)
    if isinstance(out.get("attributes"), dict):
        out["attributes"] = redact_value(out["attributes"])
    for key in ("status_message", "operation", "route_template"):
        if isinstance(out.get(key), str):
            out[key] = redact_text(out[key])
    return out


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #
def write_spans(
    db: Database, app_id: str, spans: list[dict], redact: bool = True
) -> dict:
    """Insert spans, ignoring any span id already seen for this project."""
    accepted = skipped = dropped = 0
    stamp = utcnow_iso()

    with db.write() as conn:
        for span in spans:
            from .attributes import sanitize_attributes

            cleaned, dropped_fields = sanitize_attributes(span.get("attributes") or {})
            dropped += dropped_fields
            if redact:
                from .redaction import redact_text, redact_value

                cleaned = redact_value(cleaned)
                message = span.get("status_message")
                message = redact_text(message) if isinstance(message, str) else message
            else:
                message = span.get("status_message")

            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO spans
                    (span_id, trace_id, parent_span_id, app_id, service_name, operation,
                     span_kind, status, status_message, start_time, end_time, duration_ms,
                     route_template, attributes, received_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    span["span_id"], span["trace_id"], span.get("parent_span_id"), app_id,
                    span.get("service_name"), span["operation"], span["span_kind"],
                    span["status"], message, span["start_time"], span["end_time"],
                    span["duration_ms"], span.get("route_template"),
                    json.dumps(cleaned, separators=(",", ":"), default=str), stamp,
                ),
            )
            if cursor.rowcount == 1:
                accepted += 1
            else:
                skipped += 1

    return {"accepted": accepted, "skipped_duplicates": skipped, "dropped_attributes": dropped}


def write_metric_points(
    db: Database, app_id: str, points: list[dict], redact: bool = True
) -> dict:
    accepted = skipped = dropped = 0
    stamp = utcnow_iso()

    with db.write() as conn:
        for point in points:
            from .attributes import sanitize_attributes

            cleaned, dropped_fields = sanitize_attributes(point.get("attributes") or {})
            dropped += dropped_fields
            if redact:
                from .redaction import redact_value

                cleaned = redact_value(cleaned)

            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO metric_points
                    (app_id, metric_name, metric_kind, unit, service_name, point_time,
                     value, count, attributes, dimension_hash, received_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    app_id, point["metric_name"], point["metric_kind"], point.get("unit"),
                    point.get("service_name"), point["point_time"], point["value"],
                    point.get("count"),
                    json.dumps(cleaned, separators=(",", ":"), default=str),
                    point["dimension_hash"], stamp,
                ),
            )
            if cursor.rowcount == 1:
                accepted += 1
            else:
                skipped += 1

    return {"accepted": accepted, "skipped_duplicates": skipped, "dropped_attributes": dropped}


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #
def _span_row(row, redact: bool) -> dict:
    out = dict(row)
    raw = out.pop("attributes", None)
    try:
        out["attributes"] = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        out["attributes"] = {}
    return _redact_row(out, redact)


def list_traces(
    db: Database,
    *,
    app_id: str,
    service_name: str | None = None,
    status: str | None = None,
    min_duration_ms: int | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """One row per trace: when it started, how long, how many spans, any errors."""
    clauses = ["app_id = ?"]
    params: list[Any] = [app_id]
    if since:
        clauses.append("start_time >= ?")
        params.append(since)
    if until:
        clauses.append("start_time < ?")
        params.append(until)
    if service_name:
        clauses.append("service_name = ?")
        params.append(service_name)
    if status:
        clauses.append("status = ?")
        params.append(status)
    if min_duration_ms is not None:
        clauses.append("duration_ms >= ?")
        params.append(int(min_duration_ms))

    rows = db.query(
        f"""
        SELECT trace_id,
               MIN(start_time)                     AS started_at,
               MAX(duration_ms)                    AS duration_ms,
               COUNT(*)                            AS span_count,
               SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) AS error_count,
               GROUP_CONCAT(DISTINCT service_name) AS services,
               (SELECT operation FROM spans s2
                 WHERE s2.trace_id = spans.trace_id AND s2.app_id = spans.app_id
                   AND s2.parent_span_id IS NULL
                 ORDER BY s2.duration_ms DESC LIMIT 1) AS root_operation
          FROM spans
         WHERE {' AND '.join(clauses)}
         GROUP BY trace_id
         ORDER BY started_at DESC
         LIMIT ?
        """,
        tuple(params) + (max(1, min(int(limit), MAX_LIMIT)),),
    )
    out = []
    for row in rows:
        item = dict(row)
        item["services"] = (item.get("services") or "").split(",") if item.get("services") else []
        out.append(item)
    return out


def get_trace(db: Database, app_id: str, trace_id: str, redact: bool = True) -> dict | None:
    """Every span in a trace, plus the log events that share its trace id.

    The join is the point: this is where a slow request and the error it logged
    stop being two unrelated rows.
    """
    spans = db.query(
        "SELECT * FROM spans WHERE app_id = ? AND trace_id = ? ORDER BY start_time, duration_ms DESC",
        (app_id, trace_id),
    )
    if not spans:
        return None

    logs = db.query(
        """
        SELECT id, event_time, severity, event_type, service_name, message,
               route_template, status_code, duration_ms, error_code, error_fingerprint
          FROM log_events
         WHERE app_id = ? AND trace_id = ?
         ORDER BY event_time
         LIMIT 500
        """,
        (app_id, trace_id),
    )

    span_rows = [_span_row(row, redact) for row in spans]
    root = next((s for s in span_rows if not s.get("parent_span_id")), span_rows[0])
    total = max((s["duration_ms"] for s in span_rows), default=0)
    errors = sum(1 for s in span_rows if s["status"] == "error")

    return {
        "trace_id": trace_id,
        "root_operation": root["operation"],
        "started_at": root["start_time"],
        "duration_ms": total,
        "span_count": len(span_rows),
        "error_count": errors,
        "services": sorted({s["service_name"] for s in span_rows if s.get("service_name")}),
        "spans": span_rows,
        "logs": [dict(row) for row in logs],
    }


def list_services(db: Database, app_id: str, since: str | None = None) -> list[str]:
    clauses = ["app_id = ?", "service_name IS NOT NULL"]
    params: list[Any] = [app_id]
    if since:
        clauses.append("start_time >= ?")
        params.append(since)
    rows = db.query(
        f"SELECT DISTINCT service_name FROM spans WHERE {' AND '.join(clauses)} "
        "ORDER BY service_name LIMIT 200",
        tuple(params),
    )
    return [row["service_name"] for row in rows]


def _percentile(db: Database, where: str, params: tuple, fraction: float) -> int | None:
    total = db.scalar(f"SELECT COUNT(*) FROM spans WHERE {where}", params) or 0
    if total == 0:
        return None
    offset = max(0, min(int(total * fraction), total - 1))
    row = db.one(
        f"SELECT duration_ms FROM spans WHERE {where} ORDER BY duration_ms LIMIT 1 OFFSET ?",
        params + (offset,),
    )
    return int(row["duration_ms"]) if row else None


def red_metrics(
    db: Database,
    *,
    app_id: str,
    since: str,
    until: str | None = None,
    group_by: str = "service",
    limit: int = 50,
) -> dict:
    """Rate, Errors and Duration per service or per operation.

    Percentiles are exact, not estimated: a bounded number of groups means one
    indexed lookup each, which is cheaper than being wrong about p95.
    """
    if group_by not in ("service", "operation", "route"):
        raise ValueError("group_by must be service, operation or route")
    column = {
        "service": "service_name",
        "operation": "operation",
        "route": "route_template",
    }[group_by]

    clauses = ["app_id = ?", "start_time >= ?", f"{column} IS NOT NULL"]
    params: list[Any] = [app_id, since]
    if until:
        clauses.append("start_time < ?")
        params.append(until)
    where = " AND ".join(clauses)

    rows = db.query(
        f"""
        SELECT {column} AS label,
               COUNT(*) AS spans,
               SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) AS errors,
               AVG(duration_ms) AS avg_ms,
               MAX(duration_ms) AS max_ms
          FROM spans
         WHERE {where}
         GROUP BY {column}
         ORDER BY spans DESC
         LIMIT ?
        """,
        tuple(params) + (max(1, min(int(limit), 200)),),
    )

    window_seconds = None
    if since and until:
        from datetime import datetime

        try:
            start = datetime.strptime(since[:19], "%Y-%m-%dT%H:%M:%S")
            end = datetime.strptime(until[:19], "%Y-%m-%dT%H:%M:%S")
            window_seconds = max((end - start).total_seconds(), 1)
        except ValueError:
            window_seconds = None

    out = []
    for row in rows:
        group_where = f"{where} AND {column} = ?"
        group_params = tuple(params) + (row["label"],)
        spans = int(row["spans"])
        errors = int(row["errors"] or 0)
        out.append(
            {
                "label": row["label"],
                "spans": spans,
                "errors": errors,
                "error_rate": round(errors / spans, 4) if spans else 0.0,
                "rate_per_second": (
                    round(spans / window_seconds, 4) if window_seconds else None
                ),
                "avg_ms": round(float(row["avg_ms"] or 0), 1),
                "p50_ms": _percentile(db, group_where, group_params, 0.50),
                "p95_ms": _percentile(db, group_where, group_params, 0.95),
                "p99_ms": _percentile(db, group_where, group_params, 0.99),
                "max_ms": int(row["max_ms"] or 0),
            }
        )

    return {
        "group_by": group_by,
        "since": since,
        "until": until,
        "window_seconds": window_seconds,
        "groups": out,
    }


def trace_summary(db: Database, app_id: str, since: str) -> dict:
    """The headline numbers for the traces page."""
    row = db.one(
        """
        SELECT COUNT(*) AS spans,
               COUNT(DISTINCT trace_id) AS traces,
               SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) AS errors,
               AVG(duration_ms) AS avg_ms
          FROM spans WHERE app_id = ? AND start_time >= ?
        """,
        (app_id, since),
    )
    spans = int(row["spans"] or 0)
    errors = int(row["errors"] or 0)
    return {
        "spans": spans,
        "traces": int(row["traces"] or 0),
        "errors": errors,
        "error_rate": round(errors / spans, 4) if spans else 0.0,
        "avg_ms": round(float(row["avg_ms"] or 0), 1),
        "services": len(list_services(db, app_id, since)),
    }


def list_metric_names(db: Database, app_id: str, since: str | None = None) -> list[dict]:
    clauses = ["app_id = ?"]
    params: list[Any] = [app_id]
    if since:
        clauses.append("point_time >= ?")
        params.append(since)
    rows = db.query(
        f"""
        SELECT metric_name, metric_kind, unit, COUNT(*) AS points,
               MAX(point_time) AS latest
          FROM metric_points WHERE {' AND '.join(clauses)}
         GROUP BY metric_name, metric_kind, unit
         ORDER BY metric_name LIMIT 200
        """,
        tuple(params),
    )
    return [dict(row) for row in rows]


def metric_series(
    db: Database,
    *,
    app_id: str,
    metric_name: str,
    since: str,
    until: str | None = None,
    service_name: str | None = None,
    limit: int = 1000,
) -> list[dict]:
    clauses = ["app_id = ?", "metric_name = ?", "point_time >= ?"]
    params: list[Any] = [app_id, metric_name, since]
    if until:
        clauses.append("point_time < ?")
        params.append(until)
    if service_name:
        clauses.append("service_name = ?")
        params.append(service_name)

    rows = db.query(
        f"""
        SELECT point_time, value, count, service_name, metric_kind, unit
          FROM metric_points WHERE {' AND '.join(clauses)}
         ORDER BY point_time LIMIT ?
        """,
        tuple(params) + (max(1, min(int(limit), 5000)),),
    )
    return [dict(row) for row in rows]
