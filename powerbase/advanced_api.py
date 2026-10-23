"""Step 6: traces, RED metrics, OTLP metrics, capacity and drains.

Every route names a permission and is refused by default — see `authz`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict

from . import authz, capacity, drains, traces
from .config import Settings
from .db import Database


class DrainIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    target: str
    app_id: str | None = None
    min_severity: str = "DEBUG"
    batch_size: int = 500
    enabled: bool = True


def _window(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )


def register_advanced_routes(
    app, db: Database, settings: Settings, require_admin
) -> None:
    _, require = authz.build(db, settings)

    def guard_app(actor: dict, app_id: str) -> str:
        """A scoped user may only read their own projects."""
        from . import team

        if actor["user"] is not None and not team.may_see_project(db, actor["user"], app_id):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "not your project")
        return app_id

    # ---------------------------------------------------------------- traces
    @app.get("/v1/traces", tags=["traces"])
    def traces_index(
        app_id: str,
        service_name: str | None = None,
        status_filter: str | None = Query(default=None, alias="status"),
        min_duration_ms: int | None = None,
        days: int = Query(default=1, ge=1, le=90),
        limit: int = Query(default=50, ge=1, le=500),
        actor: dict = Depends(require("read")),
    ) -> dict:
        app_id = guard_app(actor, app_id)
        since = _window(days)
        return {
            "summary": traces.trace_summary(db, app_id, since),
            "services": traces.list_services(db, app_id, since),
            "items": traces.list_traces(
                db, app_id=app_id, service_name=service_name, status=status_filter,
                min_duration_ms=min_duration_ms, since=since, limit=limit,
            ),
        }

    @app.get("/v1/traces/{trace_id}", tags=["traces"])
    def traces_get(
        trace_id: str, app_id: str, actor: dict = Depends(require("read"))
    ) -> dict:
        """The spans, plus every log event sharing this trace id.

        That join is the whole point: a slow request and the error it logged
        stop being two unrelated rows.
        """
        app_id = guard_app(actor, app_id)
        trace = traces.get_trace(db, app_id, trace_id, redact=settings.redaction_enabled)
        if trace is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such trace")
        return trace

    @app.get("/v1/metrics/red", tags=["traces"])
    def red(
        app_id: str,
        group_by: str = Query(default="service", pattern="^(service|operation|route)$"),
        days: int = Query(default=1, ge=1, le=90),
        limit: int = Query(default=50, ge=1, le=200),
        actor: dict = Depends(require("read")),
    ) -> dict:
        """Rate, errors and duration, derived from spans.

        A service that sends spans gets RED without being asked for it.
        """
        app_id = guard_app(actor, app_id)
        now = datetime.now(timezone.utc)
        since = (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        until = now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        return traces.red_metrics(
            db, app_id=app_id, since=since, until=until, group_by=group_by, limit=limit
        )

    # --------------------------------------------------------- otel metrics
    @app.get("/v1/otel-metrics", tags=["traces"])
    def otel_metrics_index(
        app_id: str, days: int = Query(default=1, ge=1, le=90),
        actor: dict = Depends(require("read")),
    ) -> dict:
        app_id = guard_app(actor, app_id)
        return {"items": traces.list_metric_names(db, app_id, _window(days))}

    @app.get("/v1/otel-metrics/{metric_name}", tags=["traces"])
    def otel_metric_series(
        metric_name: str,
        app_id: str,
        service_name: str | None = None,
        days: int = Query(default=1, ge=1, le=90),
        limit: int = Query(default=1000, ge=1, le=5000),
        actor: dict = Depends(require("read")),
    ) -> dict:
        app_id = guard_app(actor, app_id)
        points = traces.metric_series(
            db, app_id=app_id, metric_name=metric_name, since=_window(days),
            service_name=service_name, limit=limit,
        )
        return {"metric_name": metric_name, "count": len(points), "points": points}

    # -------------------------------------------------------------- capacity
    @app.get("/v1/capacity", tags=["capacity"])
    def capacity_report(
        days: int = Query(default=7, ge=1, le=90),
        actor: dict = Depends(require("read")),
    ) -> dict:
        return capacity.report(db, settings, window_days=days)

    # ---------------------------------------------------------------- drains
    @app.get("/v1/log-drains", tags=["drains"])
    def drains_index(
        app_id: str | None = None, actor: dict = Depends(require("read"))
    ) -> dict:
        items = drains.list_drains(db, app_id)
        for item in items:
            item["runs"] = drains.list_runs(db, item["id"], limit=5)
        return {"items": items}

    @app.post("/v1/log-drains", status_code=status.HTTP_201_CREATED, tags=["drains"])
    def drains_create(body: DrainIn, actor: dict = Depends(require("settings:write"))) -> dict:
        try:
            return drains.create_drain(
                db, name=body.name, target=body.target, app_id=body.app_id,
                min_severity=body.min_severity, batch_size=body.batch_size,
                enabled=body.enabled,
            )
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None

    @app.delete("/v1/log-drains/{drain_id}", tags=["drains"])
    def drains_delete(
        drain_id: str, actor: dict = Depends(require("settings:write"))
    ) -> dict:
        if not drains.delete_drain(db, drain_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such drain")
        return {"id": drain_id, "deleted": True}

    @app.post("/v1/log-drains/{drain_id}/flush", tags=["drains"])
    def drains_flush(
        drain_id: str, actor: dict = Depends(require("settings:write"))
    ) -> dict:
        """Run one drain now, rather than waiting for the next pass."""
        if drains.get_drain(db, drain_id) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such drain")
        return drains.flush(db, settings, drain_id=drain_id)

    @app.get("/v1/log-drains/{drain_id}/runs", tags=["drains"])
    def drains_runs(
        drain_id: str, limit: int = Query(default=50, ge=1, le=200),
        actor: dict = Depends(require("read")),
    ) -> dict:
        if drains.get_drain(db, drain_id) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such drain")
        return {"items": drains.list_runs(db, drain_id, limit)}
