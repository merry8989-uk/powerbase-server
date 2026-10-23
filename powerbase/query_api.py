"""Read-only query API.

Reached with a `read`-scoped API key, never an ingest key. The dashboard uses
the same functions server-side, so the JSON API and the UI can never disagree
about what a number means.
"""

from __future__ import annotations

from dataclasses import replace

from fastapi import Depends, HTTPException, Query, status

from . import alerts, queries
from .config import Settings
from .db import Database
from .queries import parse_range

MAX_LIMIT = 500


def register_query_routes(app, db: Database, settings: Settings, read_auth) -> None:
    def resolve_app(auth, app_id: str | None) -> str:
        """A read key is scoped to one app and may not read another's data."""
        if app_id and app_id != auth.app_id:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, "app_id does not match the authenticated key"
            )
        return auth.app_id

    def clamp(limit: int) -> int:
        return max(1, min(limit, MAX_LIMIT))

    # ----------------------------------------------------------------- events
    @app.get("/v1/query/events", tags=["query"])
    def query_events(
        app_id: str | None = None,
        session_id: str | None = None,
        device_id: str | None = None,
        name: str | None = None,
        screen: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = Query(default=100, ge=1, le=MAX_LIMIT),
        offset: int = Query(default=0, ge=0),
        order: str = Query(default="desc", pattern="^(asc|desc)$"),
        auth=Depends(read_auth),
    ) -> dict:
        app_id = resolve_app(auth, app_id)
        start, end = parse_range(since, until)
        rows, total = queries.list_events(
            db, app_id, session_id=session_id, device_id=device_id, name=name, screen=screen,
            since=start, until=end, limit=clamp(limit), offset=offset, order=order,
        )
        return {"total": total, "count": len(rows), "offset": offset, "items": rows}

    # ------------------------------------------------------------------- logs
    @app.get("/v1/query/logs", tags=["query"])
    def query_logs(
        app_id: str | None = None,
        level: str | None = None,
        tag: str | None = None,
        session_id: str | None = None,
        device_id: str | None = None,
        q: str | None = Query(default=None, description="full-text search over messages"),
        since: str | None = None,
        until: str | None = None,
        limit: int = Query(default=100, ge=1, le=MAX_LIMIT),
        offset: int = Query(default=0, ge=0),
        order: str = Query(default="desc", pattern="^(asc|desc)$"),
        auth=Depends(read_auth),
    ) -> dict:
        app_id = resolve_app(auth, app_id)
        start, end = parse_range(since, until)
        rows, total = queries.list_logs(
            db, app_id, level=level, tag=tag, session_id=session_id, device_id=device_id, q=q,
            since=start, until=end, limit=clamp(limit), offset=offset, order=order,
        )
        return {"total": total, "count": len(rows), "offset": offset, "items": rows}

    @app.get("/v1/query/logs/{log_id}", tags=["query"])
    def query_log(log_id: int, auth=Depends(read_auth)) -> dict:
        row = queries.get_log(db, log_id)
        if row is None or row["app_id"] != auth.app_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "log not found")
        return row

    # ---------------------------------------------------------------- crashes
    @app.get("/v1/query/crashes", tags=["query"])
    def query_crashes(
        app_id: str | None = None,
        fingerprint: str | None = None,
        kind: str | None = None,
        device_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = Query(default=100, ge=1, le=MAX_LIMIT),
        offset: int = Query(default=0, ge=0),
        auth=Depends(read_auth),
    ) -> dict:
        app_id = resolve_app(auth, app_id)
        start, end = parse_range(since, until)
        rows, total = queries.list_crashes(
            db, app_id, fingerprint=fingerprint, kind=kind, device_id=device_id,
            since=start, until=end, limit=clamp(limit), offset=offset,
        )
        return {"total": total, "count": len(rows), "offset": offset, "items": rows}

    @app.get("/v1/query/crash-groups", tags=["query"])
    def query_crash_groups(
        app_id: str | None = None,
        kind: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = Query(default=50, ge=1, le=MAX_LIMIT),
        offset: int = Query(default=0, ge=0),
        auth=Depends(read_auth),
    ) -> dict:
        app_id = resolve_app(auth, app_id)
        start, end = parse_range(since, until)
        rows, total = queries.crash_groups(
            db, app_id, since=start, until=end, kind=kind, limit=clamp(limit), offset=offset,
        )
        return {"total": total, "count": len(rows), "offset": offset, "items": rows}

    @app.get("/v1/query/crashes/{crash_id}", tags=["query"])
    def query_crash(crash_id: int, auth=Depends(read_auth)) -> dict:
        row = queries.get_crash(db, crash_id)
        if row is None or row["app_id"] != auth.app_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "crash not found")
        return row

    # -------------------------------------------------------------- sessions
    @app.get("/v1/query/sessions", tags=["query"])
    def query_sessions(
        app_id: str | None = None,
        device_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = Query(default=100, ge=1, le=MAX_LIMIT),
        offset: int = Query(default=0, ge=0),
        auth=Depends(read_auth),
    ) -> dict:
        app_id = resolve_app(auth, app_id)
        start, end = parse_range(since, until)
        rows, total = queries.list_sessions(
            db, app_id, device_id=device_id, since=start, until=end,
            limit=clamp(limit), offset=offset,
        )
        return {"total": total, "count": len(rows), "offset": offset, "items": rows}

    @app.get("/v1/query/sessions/{session_id}", tags=["query"])
    def query_session(session_id: str, auth=Depends(read_auth)) -> dict:
        row = queries.get_session(db, session_id)
        if row is None or row["app_id"] != auth.app_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "session not found")
        return row

    # --------------------------------------------------------------- devices
    @app.get("/v1/query/devices", tags=["query"])
    def query_devices(
        app_id: str | None = None,
        search: str | None = None,
        limit: int = Query(default=100, ge=1, le=MAX_LIMIT),
        offset: int = Query(default=0, ge=0),
        auth=Depends(read_auth),
    ) -> dict:
        app_id = resolve_app(auth, app_id)
        rows, total = queries.list_devices(
            db, app_id, search=search, limit=clamp(limit), offset=offset,
        )
        return {"total": total, "count": len(rows), "offset": offset, "items": rows}

    @app.get("/v1/query/devices/{device_id}", tags=["query"])
    def query_device(device_id: str, auth=Depends(read_auth)) -> dict:
        row = queries.get_device(db, device_id)
        if row is None or row["app_id"] != auth.app_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "device not found")
        return row

    # --------------------------------------------------------------- metrics
    @app.get("/v1/metrics/summary", tags=["metrics"])
    def metrics_summary(
        app_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
        days: int = Query(default=7, ge=1, le=365),
        auth=Depends(read_auth),
    ) -> dict:
        app_id = resolve_app(auth, app_id)
        start, end = parse_range(since, until)
        if start is None and end is None:
            start, end = queries.default_range(days)
        return queries.summary(db, app_id, start, end)

    @app.get("/v1/metrics/timeseries", tags=["metrics"])
    def metrics_timeseries(
        metric: str = Query(default="events"),
        bucket: str = Query(default="day", pattern="^(hour|day)$"),
        app_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
        days: int = Query(default=7, ge=1, le=365),
        auth=Depends(read_auth),
    ) -> dict:
        app_id = resolve_app(auth, app_id)
        start, end = parse_range(since, until)
        if start is None and end is None:
            start, end = queries.default_range(days)
        try:
            points = queries.timeseries(db, app_id, metric, bucket=bucket, since=start, until=end)
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
        return {
            "metric": metric,
            "bucket": bucket,
            # "rollup" means the points came from metrics_daily, "raw" that the
            # range was aggregated from the raw tables. Reported so a surprising
            # number can always be traced to its source.
            "source": queries.series_source(db, app_id, bucket, start, end),
            "available_metrics": list(queries.METRIC_NAMES),
            "points": points,
        }

    @app.get("/v1/metrics/top", tags=["metrics"])
    def metrics_top(
        dimension: str = Query(default="screen"),
        app_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
        days: int = Query(default=7, ge=1, le=365),
        limit: int = Query(default=10, ge=1, le=100),
        auth=Depends(read_auth),
    ) -> dict:
        app_id = resolve_app(auth, app_id)
        start, end = parse_range(since, until)
        if start is None and end is None:
            start, end = queries.default_range(days)
        try:
            items = queries.top_values(db, app_id, dimension, since=start, until=end, limit=limit)
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
        return {
            "dimension": dimension,
            "available_dimensions": list(queries.DIMENSION_NAMES),
            "items": items,
        }

    # --------------------------------------------------------------- alerts
    @app.get("/v1/alerts", tags=["alerts"])
    def alerts_endpoint(
        app_id: str | None = None,
        window_hours: int | None = Query(default=None, ge=1, le=24 * 30),
        auth=Depends(read_auth),
    ) -> dict:
        app_id = resolve_app(auth, app_id)
        effective = replace(settings, alert_window_hours=window_hours) if window_hours else settings
        report = alerts.evaluate(db, effective)
        # A read key is scoped to one app, so it sees that app's alerts and the
        # global ones (job failures) — never another tenant's.
        report.alerts = [a for a in report.alerts if a.app_id in (None, app_id)]
        return report.to_dict()

    # ---------------------------------------------------------- data browser
    @app.get("/v1/query/tables", tags=["query"])
    def query_tables(auth=Depends(read_auth)) -> dict:
        return {"items": queries.list_tables(db)}

    @app.get("/v1/query/tables/{table}", tags=["query"])
    def query_table(
        table: str,
        limit: int = Query(default=50, ge=1, le=MAX_LIMIT),
        offset: int = Query(default=0, ge=0),
        order_by: str | None = None,
        order: str = Query(default="desc", pattern="^(asc|desc)$"),
        auth=Depends(read_auth),
    ) -> dict:
        if not any(t["name"] == table for t in queries.list_tables(db)):
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown table: {table}")
        rows, total, columns = queries.table_rows(
            db, table, limit=clamp(limit), offset=offset, order_by=order_by, order=order,
        )
        return {
            "table": table,
            "columns": columns,
            "total": total,
            "items": rows,
            "foreign_keys": queries.foreign_keys(db, table),
        }
