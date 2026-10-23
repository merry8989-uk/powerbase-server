"""Read API for observability log events, and saved searches.

Reached with a `read`-scoped key. The console calls the same ``logquery``
functions server-side, so the JSON API and the UI cannot disagree about what a
filter means.

Every query error becomes a 400 with the reason, never a silently empty result:
a filter that quietly does nothing looks like missing data.
"""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta
from typing import Any

from fastapi import Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from . import logquery, saved_queries
from .config import Settings
from .db import Database
from .logquery import QueryError
from .models import to_iso
from .queries import parse_range

MAX_LIMIT = logquery.MAX_LIMIT

#: A query slower than this is logged with its id, so "which query was slow" has
#: an answer rather than a shrug.
SLOW_QUERY_MS = 1000

logger = logging.getLogger("powerbase.query")


class LogQueryIn(BaseModel):
    """The roadmap's validated query format. Raw SQL is never accepted."""

    # `extra="forbid"` is the point: an unknown key is a typo or an injection
    # attempt, and either way it must not be ignored.
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    app_id: str | None = None
    # The specification writes this as `"7d"`. A `{from, to}` object is also
    # accepted, because that is what the console sends.
    time_range: dict[str, Any] | str | None = Field(default=None, alias="timeRange")
    # Two shapes, both documented. A list of `{field, operator, value}` is the
    # general form the console uses; the specification's named object
    # `{platform, severity, category, environment, search}` is the simple one.
    filters: list[dict[str, Any]] | dict[str, Any] | None = None
    sort: dict[str, Any] | None = None
    limit: int | None = None
    cursor: str | None = None
    q: str | None = None
    # The specification nests the page controls. Also accepted flat, above.
    pagination: dict[str, Any] | None = None


#: The specification's filter names, mapped onto the columns. `platform` is the
#: one that is not a rename: the specification's platform (web / android /
#: backend) is this schema's `source_type`.
NAMED_FILTERS = {
    "platform": "source_type",
    "severity": "severity",
    "category": "category",
    "environment": "environment",
    "source": "source",
    "event_type": "event_type",
    "service": "service_name",
    "route": "route_template",
    "app_version": "app_version",
    "trace_id": "trace_id",
    "request_id": "request_id",
}

#: Keys that are not filters, and where they go instead.
NAMED_SPECIAL = {"search", "time_range"}


class SavedQueryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    app_id: str
    name: str
    definition: dict[str, Any] | None = None


def register_log_query_routes(app, db: Database, settings: Settings, read_auth, require_admin) -> None:
    def resolve_app(auth, app_id: str | None) -> str:
        if app_id and app_id != auth.app_id:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, "app_id does not match the authenticated key"
            )
        return auth.app_id

    def resolve_range(since: str | None, until: str | None) -> tuple[str | None, str | None]:
        start, end = parse_range(since, until)
        if start and end and (end - start) > timedelta(days=logquery.MAX_RANGE_DAYS):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"the range may not exceed {logquery.MAX_RANGE_DAYS} days",
            )
        return (to_iso(start) if start else None, to_iso(end) if end else None)

    def run(
        *,
        app_id: str,
        since: str | None,
        until: str | None,
        filters: list[dict[str, Any]] | None,
        q: str | None,
        sort: dict[str, Any] | None = None,
        sort_field: str = "event_time",
        direction: str = "desc",
        limit: int | None = None,
        cursor: str | None = None,
    ) -> dict:
        try:
            parsed = logquery.parse_filters(filters)
            if sort is not None:
                sort_field, direction = logquery.parse_sort(sort)
            start, end = resolve_range(since, until)
            page = logquery.query_log_events(
                db,
                app_id=app_id,
                since=start,
                until=end,
                filters=parsed,
                q=q,
                sort=sort_field,
                direction=direction,
                limit=logquery.clamp_limit(limit),
                cursor=cursor,
            )
        except QueryError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None

        query_id = uuid.uuid4().hex[:16]
        pagination = {
            "next_cursor": page.next_cursor,
            "has_more": page.next_cursor is not None,
            "limit": page.limit,
        }
        # Logged with the id so a slow query can be named. A query id that is
        # only ever returned and never recorded is decoration.
        if page.elapsed_ms >= SLOW_QUERY_MS:
            logger.warning(
                "slow log query id=%s app=%s ms=%s rows=%s",
                query_id, app_id, page.elapsed_ms, len(page.items),
            )
        else:
            logger.debug(
                "log query id=%s app=%s ms=%s rows=%s",
                query_id, app_id, page.elapsed_ms, len(page.items),
            )

        return {
            # The row list. The specification calls this `data`; the name is
            # kept as `items` because the console and every test read it, and
            # duplicating a 200-row page under a second key would double the
            # payload of every request.
            "items": page.items,
            "count": len(page.items),
            "limit": page.limit,
            "next_cursor": page.next_cursor,
            "has_more": page.next_cursor is not None,
            "truncated": page.truncated,
            "elapsed_ms": page.elapsed_ms,
            "sort": {"field": sort_field, "direction": direction},
            # The specification's envelope, alongside the flat keys rather than
            # replacing them.
            "meta": {
                "query_id": query_id,
                "duration_ms": page.elapsed_ms,
                "sort": {"field": sort_field, "direction": direction},
            },
            "pagination": pagination,
        }

    def named_filters(pairs: list[tuple[str, Any]]) -> list[dict[str, Any]]:
        """Turn simple GET parameters into the same filter format POST takes."""
        spec: list[dict[str, Any]] = []
        for name, value in pairs:
            if value is None or value == "":
                continue
            spec.append({"field": name, "operator": "eq", "value": value})
        return spec

    # --------------------------------------------------------------- query
    @app.get("/v1/query/log-events", tags=["query"])
    def query_log_events(
        app_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
        severity: str | None = None,
        source: str | None = None,
        event_type: str | None = None,
        service_name: str | None = None,
        environment: str | None = None,
        route_template: str | None = None,
        app_version: str | None = None,
        trace_id: str | None = None,
        request_id: str | None = None,
        error_fingerprint: str | None = None,
        status_code: int | None = None,
        min_status: int | None = None,
        max_status: int | None = None,
        min_duration_ms: int | None = None,
        q: str | None = None,
        sort: str = "event_time",
        direction: str = "desc",
        limit: int = Query(default=logquery.DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
        cursor: str | None = None,
        auth=Depends(read_auth),
    ) -> dict:
        filters = named_filters(
            [
                ("severity", severity),
                ("source", source),
                ("event_type", event_type),
                ("service_name", service_name),
                ("environment", environment),
                ("route_template", route_template),
                ("app_version", app_version),
                ("trace_id", trace_id),
                ("request_id", request_id),
                ("error_fingerprint", error_fingerprint),
                ("status_code", status_code),
            ]
        )
        if min_status is not None:
            filters.append({"field": "status_code", "operator": "gte", "value": min_status})
        if max_status is not None:
            filters.append({"field": "status_code", "operator": "lte", "value": max_status})
        if min_duration_ms is not None:
            filters.append(
                {"field": "duration_ms", "operator": "gte", "value": min_duration_ms}
            )

        return run(
            app_id=resolve_app(auth, app_id),
            since=since,
            until=until,
            filters=filters,
            q=q,
            sort={"field": sort, "direction": direction},
            limit=limit,
            cursor=cursor,
        )

    @app.post("/v1/query/log-events", tags=["query"])
    def query_log_events_post(
        body: LogQueryIn, app_id: str | None = None, auth=Depends(read_auth)
    ) -> dict:
        """The POST verb.

        `app_id` is read from the body, and from the query string when the body
        does not carry one — because the GET verb takes it from the query
        string, and a client following that convention was having it silently
        ignored. It never changed which rows came back (a key is bound to one
        project), but a request that looks like it asks for one project and
        quietly answers with another is a bad thing to leave in place.
        """
        # Normalise the specification's named filter object into the general
        # filter list, and lift `search` and `time_range` out of it.
        filters = body.filters
        q = body.q
        window = body.time_range
        if isinstance(filters, dict):
            named: list[dict[str, Any]] = []
            for key, value in filters.items():
                if key in NAMED_SPECIAL:
                    if key == "search" and q is None:
                        q = value if isinstance(value, str) else None
                    elif key == "time_range" and window is None:
                        window = value
                    continue
                column = NAMED_FILTERS.get(key)
                if column is None:
                    raise HTTPException(
                        status.HTTP_400_BAD_REQUEST,
                        f"unknown filter {key!r}; known: "
                        # `set(...)`, not the dict itself: `dict | set` is a
                        # TypeError, and it fired only on the unknown-filter
                        # path — so a user's typo produced a 500 instead of the
                        # message that would have told them the right name.
                        + ", ".join(sorted(set(NAMED_FILTERS) | NAMED_SPECIAL)),
                    )
                # `all` is the specification's "no filter", and it must mean
                # "everything the caller may see" — never "every tenant".
                if value is None or value == "" or value == "all":
                    continue
                named.append({"field": column, "operator": "eq", "value": value})
            filters = named

        # The specification nests the page controls.
        limit, cursor = body.limit, body.cursor
        if body.pagination:
            unknown_page = set(body.pagination) - {"limit", "cursor"}
            if unknown_page:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    f"unknown pagination keys: {sorted(unknown_page)}",
                )
            if limit is None:
                limit = body.pagination.get("limit")
            if cursor is None:
                cursor = body.pagination.get("cursor")

        # `"7d"` resolves against the server clock; `{from, to}` is taken as
        # given. The half-open interval is the same either way.
        if isinstance(window, str):
            try:
                since, until = logquery.parse_relative_window(window)
            except QueryError as exc:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None
        elif window is None:
            since, until = None, None
        elif not isinstance(window, dict):
            # A number, a list, a boolean — anything that is neither the
            # specification's string nor the console's object. Refused rather
            # than iterated, which is what produced a 500 here.
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "'timeRange' must be an object with 'from'/'to', or a string like '7d'",
            )
        else:
            unknown = set(window) - {"from", "to"}
            if unknown:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    f"unknown timeRange keys: {sorted(unknown)}",
                )
            since, until = window.get("from"), window.get("to")
            for label, value in (("from", since), ("to", until)):
                if value is not None and not isinstance(value, str):
                    raise HTTPException(
                        status.HTTP_400_BAD_REQUEST, f"timeRange.{label} must be a string"
                    )

        return run(
            app_id=resolve_app(auth, body.app_id or app_id),
            since=since,
            until=until,
            filters=filters,
            q=q,
            sort=body.sort,
            limit=limit,
            cursor=cursor,
        )

    @app.get("/v1/query/log-events/stats", tags=["query"])
    def query_log_events_stats(
        app_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
        severity: str | None = None,
        source: str | None = None,
        service_name: str | None = None,
        environment: str | None = None,
        route_template: str | None = None,
        error_fingerprint: str | None = None,
        min_status: int | None = None,
        q: str | None = None,
        bucket: str = "hour",
        auth=Depends(read_auth),
    ) -> dict:
        filters = named_filters(
            [
                ("severity", severity),
                ("source", source),
                ("service_name", service_name),
                ("environment", environment),
                ("route_template", route_template),
                ("error_fingerprint", error_fingerprint),
            ]
        )
        if min_status is not None:
            filters.append({"field": "status_code", "operator": "gte", "value": min_status})

        try:
            parsed = logquery.parse_filters(filters)
            start, end = resolve_range(since, until)
            return logquery.log_event_stats(
                db,
                app_id=resolve_app(auth, app_id),
                since=start,
                until=end,
                filters=parsed,
                q=q,
                bucket=bucket,
            )
        except QueryError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None

    # `stats` is registered above `{event_id}` on purpose: FastAPI matches in
    # declaration order, so the other way round would swallow it.
    @app.get("/v1/query/log-events/{event_id}", tags=["query"])
    def query_log_event(
        event_id: str,
        app_id: str | None = None,
        related: bool = True,
        auth=Depends(read_auth),
    ) -> dict:
        resolved = resolve_app(auth, app_id)
        event = logquery.get_log_event(db, resolved, event_id)
        if event is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such event")
        body = {"event": event}
        if related:
            body["related"] = logquery.related_events(db, resolved, event)
        return body

    # -------------------------------------------------------- saved queries
    # Admin-guarded: a saved search is console configuration, not client data.
    @app.get("/v1/saved-queries", tags=["saved-queries"])
    def saved_index(
        app_id: str, _: None = Depends(require_admin)
    ) -> dict:
        return {"items": saved_queries.list_saved_queries(db, app_id)}

    @app.post("/v1/saved-queries", status_code=status.HTTP_201_CREATED, tags=["saved-queries"])
    def saved_create(body: SavedQueryIn, _: None = Depends(require_admin)) -> dict:
        try:
            return saved_queries.create_saved_query(
                db, body.app_id, body.name, body.definition
            )
        except QueryError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None

    @app.get("/v1/saved-queries/{query_id}", tags=["saved-queries"])
    def saved_get(query_id: str, _: None = Depends(require_admin)) -> dict:
        found = saved_queries.get_saved_query(db, query_id)
        if found is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such saved query")
        return found

    @app.delete("/v1/saved-queries/{query_id}", tags=["saved-queries"])
    def saved_delete(query_id: str, _: None = Depends(require_admin)) -> dict:
        if not saved_queries.delete_saved_query(db, query_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such saved query")
        return {"id": query_id, "deleted": True}
