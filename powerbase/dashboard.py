"""Server-rendered dashboard.

Jinja2 templates plus HTMX for the interactive bits (filters, drawers,
pagination) and Chart.js for the graphs. No SPA, no build step, no node_modules:
one process serves both the API and the UI, which is the whole point of a
single-file self-hosted tool.

The dashboard reads through the same `queries` functions the JSON API uses, so
the UI and the API can never disagree about what a number means.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import Depends, Form, Query, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from datetime import datetime, timedelta, timezone
from . import alerts as alerts_module
from . import logquery, saved_queries
from .models import to_iso
from . import dashboard_auth as auth
from . import authz, team
from . import queries
from . import rollups
from .config import Settings
from .db import Database
from .queries import parse_range
from .version import __version__

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

LEVELS = ("DEBUG", "INFO", "WARN", "ERROR", "FATAL")
CRASH_KINDS = ("java", "native", "anr", "other")
END_REASONS = ("background", "crash", "timeout")


# --------------------------------------------------------------------------- #
# Template filters
# --------------------------------------------------------------------------- #

def _short_ts(value: str | None) -> str:
    """'2026-10-22T09:13:58.221000Z' -> '22 Oct 09:13'"""
    if not value or len(value) < 16:
        return value or ""
    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    try:
        day = value[8:10]
        month = months[int(value[5:7]) - 1]
        return f"{day} {month} {value[11:16]}"
    except (ValueError, IndexError):
        return value


def _duration(ms: int | None) -> str:
    if ms is None:
        return "—"
    seconds = ms / 1000
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes, seconds = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m {seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def _percent(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value * 100:.1f}%"


def _number(value) -> str:
    if value is None:
        return "—"
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return str(value)


def register_dashboard_routes(app, db: Database, settings: Settings) -> None:
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.filters["short_ts"] = _short_ts
    templates.env.filters["duration"] = _duration
    templates.env.filters["percent"] = _percent
    templates.env.filters["number"] = _number

    def _range_options(current: int, presets=(1, 7, 14, 30, 90)) -> list[int]:
        """Presets plus the active value, so the selector never mislabels the range.

        A <select> with no matching <option> silently displays its first entry,
        which made a `days=365` view claim to be showing "last 1d".
        """
        return sorted({int(preset) for preset in presets} | {int(current)})

    templates.env.globals["range_options"] = _range_options

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    def render(name: str, context: dict, status_code: int = 200, headers: dict | None = None):
        """The single place that knows Starlette's TemplateResponse argument order.

        Starlette 1.0 moved `request` to the first positional argument; keeping
        one wrapper here means the routes stay readable and a future signature
        change is a one-line edit.
        """
        # Every page needs to know who is looking, and what they may do. Doing
        # it here means a route cannot forget, and a template does not have to
        # be handed the same two things twenty times.
        actor = getattr(context["request"].state, "actor", None)
        context.setdefault("actor", actor)
        context.setdefault(
            "can",
            lambda permission: bool(actor) and team.can(actor["role"], permission),
        )
        return templates.TemplateResponse(
            request=context["request"],
            name=name,
            context=context,
            status_code=status_code,
            headers=headers,
        )

    throttle = auth.LoginThrottle()

    # ------------------------------------------------------------- helpers
    def all_apps() -> list[dict]:
        rows = db.query("SELECT id, name, platform FROM apps ORDER BY name")
        return [dict(r) for r in rows]

    def selected_app(app_id: str | None) -> str | None:
        apps = all_apps()
        if not apps:
            return None
        if app_id and any(a["id"] == app_id for a in apps):
            return app_id
        return apps[0]["id"]

    def base_context(request: Request, app_id: str | None, page: str) -> dict:
        return {
            "request": request,
            "page": page,
            "apps": all_apps(),
            "current_app": app_id,
            "version": __version__,
            "levels": LEVELS,
            "crash_kinds": CRASH_KINDS,
            "end_reasons": END_REASONS,
            **_alert_context(db, settings),
        }

    def client_key(request: Request) -> str:
        return request.client.host if request.client else "unknown"

    def actor_from(request: Request) -> dict | None:
        """Who is asking. `None` means nobody.

        Two ways in, and they are not the same thing:

        * the **admin password** cookie — break-glass, and it maps to `owner`
          because the person who holds it can do anything anyway. Saying
          otherwise would be a lie the UI tells itself.
        * a **user session** — a real account, with a real role, which is what
          makes the roles built in `powerbase/team.py` usable here at all.
        """
        if not settings.dashboard_enabled:
            return None
        secret = auth.derive_secret(settings.admin_password or "")
        if auth.verify_cookie(secret, request.cookies.get(auth.COOKIE_NAME)):
            return {
                "kind": "admin", "role": "owner", "email": None, "user_id": None,
                "label": "admin password", "break_glass": True,
            }
        token = request.cookies.get(auth.USER_COOKIE)
        if token:
            user = team.verify_session(db, token)
            if user is not None:
                return {
                    "kind": "user", "role": user["role"], "email": user["email"],
                    "user_id": user["id"], "label": user["email"],
                    "break_glass": False,
                }
        return None

    def require_admin(request: Request) -> bool:
        if not settings.dashboard_enabled:
            raise HTMLResponse_503()
        actor = actor_from(request)
        if actor is None:
            headers = {"Location": "/login"}
            if request.headers.get("HX-Request") == "true":
                headers = {"HX-Redirect": "/login"}
            raise HTMLRedirect(headers)
        # Stashed on the request so `render` can put it in every template, and so
        # a route that cares about the role can read it without a second
        # dependency.
        request.state.actor = actor
        return True

    def require_permission(request: Request, permission: str) -> dict:
        """Refuse a console action the actor's role does not hold."""
        actor = request.state.actor
        if not team.can(actor["role"], permission):
            raise HTMLRedirect(
                {"HX-Redirect": "/team?error=denied"} if request.headers.get("HX-Request") == "true"
                else {"Location": "/team?error=denied"}
            )
        return actor

    def guard(app_id: str | None) -> str:
        resolved = selected_app(app_id)
        if resolved is None:
            raise NoApps()
        return resolved

    # --------------------------------------------------------------- status
    # Public, and deliberately thin: a version, an uptime and an up/down. The
    # numbers an operator needs are on /v1/status behind the admin password.
    @app.get("/status", response_class=HTMLResponse, include_in_schema=False)
    def status_page(request: Request):
        from .status_api import human_uptime, uptime_seconds, _db_reachable

        reachable, _ = _db_reachable(db)
        return render(
            "status.html",
            {
                "request": request,
                "version": __version__,
                "uptime": human_uptime(uptime_seconds()),
                "healthy": reachable,
            },
        )

    # --------------------------------------------------------- log explorer
    # Observability events (log_events). Separate from the analytics "Log
    # explorer" above, which reads the `logs` table the Android SDK writes.
    EXPLORER_TEXT_FIELDS = (
        "severity",
        "source",
        "event_type",
        "service_name",
        "environment",
        "route_template",
        "app_version",
        "trace_id",
        "request_id",
        "error_fingerprint",
    )
    EXPLORER_BUCKETS = ("hour", "day")
    EXPLORER_SORT_FIELDS = ("event_time", "received_at", "severity", "duration_ms", "status_code")
    EXPLORER_DEFAULT_LIMIT = 50

    def explorer_params(values: dict) -> dict:
        """Normalise raw query parameters into the shape the templates expect."""
        out = {name: (values.get(name) or None) for name in EXPLORER_TEXT_FIELDS}
        out["min_status"] = values.get("min_status") or None
        out["q"] = (values.get("q") or "").strip() or None
        try:
            days = int(values.get("days") or 7)
        except (TypeError, ValueError):
            days = 7
        out["days"] = max(1, min(days, logquery.MAX_RANGE_DAYS))
        out["bucket"] = values.get("bucket") if values.get("bucket") in EXPLORER_BUCKETS else "hour"
        out["sort"] = (
            values.get("sort") if values.get("sort") in EXPLORER_SORT_FIELDS else "event_time"
        )
        out["direction"] = "asc" if values.get("direction") == "asc" else "desc"
        try:
            limit = int(values.get("limit") or EXPLORER_DEFAULT_LIMIT)
        except (TypeError, ValueError):
            limit = EXPLORER_DEFAULT_LIMIT
        out["limit"] = max(1, min(limit, 200))
        return out

    def explorer_filter_spec(params: dict) -> list[dict]:
        """Named parameters -> the validated filter format. One shape, one validator."""
        spec: list[dict] = []
        for name in EXPLORER_TEXT_FIELDS:
            if params.get(name):
                spec.append({"field": name, "operator": "eq", "value": params[name]})
        if params.get("min_status"):
            spec.append(
                {"field": "status_code", "operator": "gte", "value": params["min_status"]}
            )
        return spec

    def explorer_window(params: dict) -> tuple[str, str]:
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=params["days"])
        return to_iso(start), to_iso(end)

    def explorer_facets(app_id: str) -> dict:
        """Distinct values for the selector dropdowns, from the data itself."""

        def distinct(column: str) -> list[str]:
            # `column` comes from this fixed set, never from the request.
            if column not in ("service_name", "environment", "route_template"):
                return []
            rows = db.query(
                f"SELECT DISTINCT {column} AS value FROM log_events "
                f"WHERE app_id = ? AND {column} IS NOT NULL ORDER BY value LIMIT 50",
                (app_id,),
            )
            return [row["value"] for row in rows]

        return {
            "services": distinct("service_name"),
            "environments": distinct("environment"),
        }

    def run_explorer(app_id: str, params: dict, cursor: str | None = None):
        """Run the list and the histogram for one filter set.

        Raises logquery.QueryError for anything the query layer refuses; the
        callers turn that into a visible message rather than an empty table.
        """
        filters = logquery.parse_filters(explorer_filter_spec(params))
        since, until = explorer_window(params)
        page = logquery.query_log_events(
            db,
            app_id=app_id,
            since=since,
            until=until,
            filters=filters,
            q=params.get("q"),
            sort=params["sort"],
            direction=params["direction"],
            limit=params["limit"],
            cursor=cursor,
        )
        stats = logquery.log_event_stats(
            db,
            app_id=app_id,
            since=since,
            until=until,
            filters=filters,
            q=params.get("q"),
            bucket=params["bucket"],
        )
        return page, stats

    def explorer_redirect(app_id: str, params: dict, flash: str | None = None):
        from urllib.parse import urlencode

        from fastapi.responses import RedirectResponse

        query = {key: value for key, value in params.items() if value not in (None, "")}
        query["app_id"] = app_id
        if flash:
            query["flash"] = flash
        return RedirectResponse(f"/explorer?{urlencode(query)}", status_code=303)

    @app.get("/explorer", response_class=HTMLResponse, include_in_schema=False)
    def explorer_page(
        request: Request,
        app_id: str | None = None,
        saved: str | None = None,
        flash: str | None = None,
        _: bool = Depends(require_admin),
    ):
        app_id = guard(app_id)
        saved_items = saved_queries.list_saved_queries(db, app_id)

        # Loading a saved search seeds the form from its stored definition. The
        # definition was validated on the way in and is re-validated on the way
        # out, so this cannot smuggle a filter past the allowlist.
        values: dict = {}
        if saved:
            found = saved_queries.get_saved_query(db, saved)
            if found and found["app_id"] == app_id:
                definition = found["definition"]
                for item in definition.get("filters", []):
                    name, operator = item.get("field"), item.get("operator")
                    if name == "status_code" and operator == "gte":
                        values["min_status"] = item.get("value")
                    elif name in EXPLORER_TEXT_FIELDS:
                        values[name] = item.get("value")
                values["q"] = definition.get("q")
                values["bucket"] = definition.get("bucket")
                sort = definition.get("sort") or {}
                values["sort"] = sort.get("field")
                values["direction"] = sort.get("direction")

        context = base_context(request, app_id, "explorer")
        context.update(
            {
                "params": explorer_params(values),
                "saved_items": saved_items,
                "saved_id": saved,
                "flash": flash,
                "severities": list(LEVELS),
                "sources": ["browser_sdk", "server_sdk", "android_sdk", "otel", "system"],
                "buckets": list(EXPLORER_BUCKETS),
                "sort_fields": list(EXPLORER_SORT_FIELDS),
                "app_id": app_id,
                **explorer_facets(app_id),
            }
        )
        return render("explorer.html", context)

    @app.get("/partials/explorer", response_class=HTMLResponse, include_in_schema=False)
    def explorer_partial(
        request: Request,
        _: bool = Depends(require_admin),
    ):
        # Read the filters straight off the query string rather than naming them
        # in the signature: there are a dozen and they change as the explorer
        # grows, and `explorer_params` is the one place that validates them.
        raw = dict(request.query_params)
        app_id = guard(raw.get("app_id"))
        cursor = raw.get("cursor")
        params = explorer_params(raw)

        page = stats = None
        error = None
        try:
            page, stats = run_explorer(app_id, params, cursor)
        except logquery.QueryError as exc:
            error = str(exc)

        context = base_context(request, app_id, "explorer")
        context.update(
            {
                "params": params,
                "page": page,
                "stats": stats,
                "error": error,
                "app_id": app_id,
            }
        )
        return render("partials/explorer_results.html", context)

    @app.get(
        "/partials/log-event/{event_id}",
        response_class=HTMLResponse,
        include_in_schema=False,
    )
    def log_event_partial(
        request: Request,
        event_id: str,
        app_id: str | None = None,
        _: bool = Depends(require_admin),
    ):
        app_id = guard(app_id)
        event = logquery.get_log_event(db, app_id, event_id)
        related = logquery.related_events(db, app_id, event) if event else []
        context = base_context(request, app_id, "explorer")
        context.update({"event": event, "related": related})
        return render("partials/log_event_detail.html", context)

    @app.post("/explorer/save", include_in_schema=False)
    async def explorer_save(
        request: Request,
        _: bool = Depends(require_admin),
    ):
        form = await request.form()
        raw = dict(form)
        app_id = guard(raw.get("app_id"))
        name = str(raw.get("name") or "")
        params = explorer_params(raw)
        try:
            saved_queries.create_saved_query(
                db,
                app_id,
                name,
                {
                    "filters": explorer_filter_spec(params),
                    "sort": {"field": params["sort"], "direction": params["direction"]},
                    "q": params.get("q"),
                    "bucket": params["bucket"],
                },
            )
            flash = f"Saved \'{name.strip()}\'."
        except logquery.QueryError as exc:
            flash = f"Not saved: {exc}"
        return explorer_redirect(app_id, params, flash)

    @app.post("/explorer/delete", include_in_schema=False)
    async def explorer_delete(
        request: Request,
        _: bool = Depends(require_admin),
    ):
        form = await request.form()
        raw = dict(form)
        app_id = guard(raw.get("app_id"))
        query_id = str(raw.get("query_id") or "")
        found = saved_queries.get_saved_query(db, query_id)
        # Scoped to the current project, so a stray id cannot delete another
        # project's search.
        if found is None or found["app_id"] != app_id:
            return explorer_redirect(app_id, {}, "That saved search does not exist here.")
        saved_queries.delete_saved_query(db, query_id)
        return explorer_redirect(app_id, {}, "Saved search deleted.")

    # --------------------------------------------------------------- alerts
    @app.get("/alerts", response_class=HTMLResponse, include_in_schema=False)
    def alerts_page(
        request: Request,
        app_id: str | None = None,
        window_hours: int | None = Query(default=None, ge=1, le=24 * 30),
        _: bool = Depends(require_admin),
    ):
        app_id = selected_app(app_id)
        effective = (
            replace_settings_window(settings, window_hours) if window_hours else settings
        )
        report = alerts_module.evaluate(db, effective)
        context = base_context(request, app_id, "alerts")
        context.update(
            {
                "alerts": [a for a in report.alerts if a.app_id in (None, app_id)],
                "report": report,
                "window_hours": effective.alert_window_hours,
                "thresholds": effective,
            }
        )
        return render("alerts.html", context)

    # --------------------------------------------------------------- login
    @app.get("/login", response_class=HTMLResponse, include_in_schema=False)
    def login_page(request: Request, next: str = "/"):
        if not settings.dashboard_enabled:
            return render(
                "login.html",
                {"request": request, "disabled": True, "error": None, "next": next},
                status_code=503,
            )
        return render(
            "login.html",
            {"request": request, "disabled": False, "error": None, "next": next},
        )

    @app.post("/login", include_in_schema=False)
    def login_submit(
        request: Request,
        password: str = Form(...),
        email: str = Form(""),
        next: str = Form("/"),
    ):
        """Two ways in, and the form says which by whether an email was given.

        An email means a real account and a real role. An empty email means the
        admin password, which is the break-glass route — and the reason it is
        still here is that an instance whose only owner account is locked out
        must be recoverable from the host, not from a support ticket.
        """
        if not settings.dashboard_enabled:
            return render(
                "login.html",
                {"request": request, "disabled": True, "error": None, "next": next},
                status_code=503,
            )

        key = client_key(request)
        if throttle.is_blocked(key):
            retry = throttle.retry_after(key)
            return render(
                "login.html",
                {
                    "request": request, "disabled": False, "next": next,
                    "error": f"Too many attempts. Try again in {retry}s.",
                },
                status_code=429,
                headers={"Retry-After": str(retry)},
            )

        target = next if next.startswith("/") else "/"
        wants_account = bool(email.strip())

        if wants_account:
            try:
                session = team.login(
                    db, settings,
                    email=email.strip(),
                    password=password,
                    user_agent=request.headers.get("user-agent"),
                )
            except team.TeamError as exc:
                throttle.record_failure(key)
                return render(
                    "login.html",
                    {
                        "request": request, "disabled": False, "next": next,
                        "email": email.strip(), "error": str(exc),
                    },
                    status_code=authz.status_for(exc),
                )
            throttle.record_success(key)
            response = RedirectResponse(url=target, status_code=303)
            response.set_cookie(
                auth.USER_COOKIE,
                session["token"],
                max_age=settings.session_ttl_seconds,
                httponly=True,
                samesite="lax",
                path="/",
            )
            return response

        if not auth.check_password(settings.admin_password or "", password):
            throttle.record_failure(key)
            return render(
                "login.html",
                {"request": request, "disabled": False, "next": next, "error": "Incorrect password."},
                status_code=401,
            )

        throttle.record_success(key)
        secret = auth.derive_secret(settings.admin_password or "")
        response = RedirectResponse(url=target, status_code=303)
        response.set_cookie(
            auth.COOKIE_NAME,
            auth.issue_cookie(secret, settings.session_ttl_seconds),
            max_age=settings.session_ttl_seconds,
            httponly=True,
            samesite="lax",
            path="/",
        )
        return response

    # ------------------------------------------------------------------ team
    # The console's half of what `powerbase/team.py` already does over the API.
    # Without this the roles are real but unusable: a developer account could not
    # sign in to the one screen the roles are meant to govern.
    @app.get("/team", response_class=HTMLResponse, include_in_schema=False)
    def team_page(
        request: Request,
        _: bool = Depends(require_admin),
        error: str | None = None,
        notice: str | None = None,
        new_password: str | None = None,
    ):
        actor = request.state.actor
        denied = not team.can(actor["role"], "users:read")
        return render(
            "team.html",
            {
                "request": request, "page": "team", "denied": denied,
                "users": [] if denied else team.list_users(db),
                "invites": [] if denied else team.list_invites(db, pending_only=True),
                "roles": team.ROLES,
                "assignable": ["admin", "developer", "readonly"],
                "actor_email": actor["email"],
                "error": error, "notice": notice, "new_password": new_password,
                "current_app": None,
            },
            status_code=403 if denied else 200,
        )

    @app.post("/team/users", include_in_schema=False)
    def team_create_user(
        request: Request,
        _: bool = Depends(require_admin),
        email: str = Form(...),
        role: str = Form(...),
        display_name: str = Form(""),
        password: str = Form(...),
    ):
        actor = require_permission(request, "users:write")
        # Minting an admin is an owner's decision, not an admin's.
        if role == "admin" and not team.can(actor["role"], "users:write_admin"):
            raise HTMLRedirect({"Location": "/team?error=only+an+owner+may+create+an+admin"})
        who = actor["email"] or "admin-password"
        try:
            team.create_user(
                db, email=email, role=role, display_name=display_name or None,
                password=password, actor=who,
            )
        except team.TeamError as exc:
            raise HTMLRedirect({"Location": f"/team?error={str(exc).replace(' ', '+')}"})
        # The password is echoed back once so the operator can copy it. There is
        # no email delivery, so this is the only chance to see it.
        raise HTMLRedirect({"Location": f"/team?notice=created&new_password={password}"})

    @app.post("/team/users/{user_id}/role", include_in_schema=False)
    def team_set_role(
        request: Request,
        user_id: str,
        _: bool = Depends(require_admin),
        role: str = Form(...),
    ):
        actor = require_permission(request, "users:write")
        if role == "admin" and not team.can(actor["role"], "users:write_admin"):
            raise HTMLRedirect({"Location": "/team?error=only+an+owner+may+promote+to+admin"})
        try:
            team.set_role(db, user_id, role, actor=actor["email"] or "admin-password")
        except team.TeamError as exc:
            raise HTMLRedirect({"Location": f"/team?error={str(exc).replace(' ', '+')}"})
        raise HTMLRedirect({"Location": "/team?notice=role+changed"})

    @app.post("/team/users/{user_id}/{action}", include_in_schema=False)
    def team_toggle(
        request: Request,
        user_id: str,
        action: str,
        _: bool = Depends(require_admin),
    ):
        actor = require_permission(request, "users:write")
        if action not in ("disable", "enable"):
            raise HTMLRedirect({"Location": "/team?error=unknown+action"})
        who = actor["email"] or "admin-password"
        try:
            if action == "disable":
                team.disable(db, user_id, actor=who)
            else:
                team.enable(db, user_id, actor=who)
        except team.TeamError as exc:
            raise HTMLRedirect({"Location": f"/team?error={str(exc).replace(' ', '+')}"})
        raise HTMLRedirect({"Location": f"/team?notice={action}d"})

    @app.get("/logout", include_in_schema=False)
    def logout(request: Request):
        # Revoke the server-side session too, not just the cookie. Deleting a
        # cookie only tells this browser to forget; the token stays valid until
        # it expires unless it is revoked, which is the difference between
        # logging out and hiding.
        token = request.cookies.get(auth.USER_COOKIE)
        if token:
            team.logout(db, token)
        response = RedirectResponse(url="/login", status_code=303)
        response.delete_cookie(auth.COOKIE_NAME, path="/")
        response.delete_cookie(auth.USER_COOKIE, path="/")
        return response

    # ------------------------------------------------------------ overview
    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def overview(
        request: Request,
        app_id: str | None = None,
        days: int = Query(default=7, ge=1, le=365),
        _: bool = Depends(require_admin),
    ):
        app_id = guard(app_id)
        since, until = queries.default_range(days)
        context = base_context(request, app_id, "overview")
        context.update(
            {
                "days": days,
                "summary": queries.summary(db, app_id, since, until),
                "trend": queries.timeseries(db, app_id, "events", bucket="day", since=since, until=until),
                "crash_trend": queries.timeseries(db, app_id, "crashes", bucket="day", since=since, until=until),
                "top_screens": queries.top_values(db, app_id, "screen", since=since, until=until, limit=8),
                "top_events": queries.top_values(db, app_id, "event", since=since, until=until, limit=8),
                "top_devices": queries.top_values(db, app_id, "model", since=since, until=until, limit=6),
                "recent_crashes": queries.crash_groups(db, app_id, since=since, until=until, limit=5)[0],
                "recent_errors": queries.list_logs(
                    db, app_id, level="ERROR", since=since, until=until, limit=6
                )[0],
            }
        )
        return render("overview.html", context)

    # --------------------------------------------------------------- logs
    @app.get("/logs", response_class=HTMLResponse, include_in_schema=False)
    def logs_page(
        request: Request,
        app_id: str | None = None,
        level: str | None = None,
        tag: str | None = None,
        q: str | None = None,
        days: int = Query(default=7, ge=1, le=365),
        _: bool = Depends(require_admin),
    ):
        app_id = guard(app_id)
        context = base_context(request, app_id, "logs")
        context.update({"level": level, "tag": tag, "q": q, "days": days})
        return render("logs.html", context)

    @app.get("/partials/logs", response_class=HTMLResponse, include_in_schema=False)
    def logs_partial(
        request: Request,
        app_id: str | None = None,
        level: str | None = None,
        tag: str | None = None,
        q: str | None = None,
        days: int = Query(default=7, ge=1, le=365),
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
        _: bool = Depends(require_admin),
    ):
        app_id = guard(app_id)
        since, until = queries.default_range(days)
        rows, total = queries.list_logs(
            db, app_id, level=level or None, tag=tag or None, q=q or None,
            since=since, until=until, limit=limit, offset=offset,
        )
        return render(
            "partials/log_rows.html",
            {
                "request": request, "rows": rows, "total": total, "limit": limit,
                "offset": offset, "app_id": app_id, "level": level, "tag": tag, "q": q, "days": days,
                "has_more": offset + len(rows) < total,
            },
        )

    @app.get("/partials/log/{log_id}", response_class=HTMLResponse, include_in_schema=False)
    def log_detail(request: Request, log_id: int, _: bool = Depends(require_admin)):
        row = queries.get_log(db, log_id)
        return render(
            "partials/log_detail.html", {"request": request, "log": row}
        )

    # ------------------------------------------------------------ crashes
    @app.get("/crashes", response_class=HTMLResponse, include_in_schema=False)
    def crashes_page(
        request: Request,
        app_id: str | None = None,
        kind: str | None = None,
        days: int = Query(default=30, ge=1, le=365),
        _: bool = Depends(require_admin),
    ):
        app_id = guard(app_id)
        since, until = queries.default_range(days)
        groups, total = queries.crash_groups(db, app_id, since=since, until=until, kind=kind, limit=50)
        context = base_context(request, app_id, "crashes")
        context.update({"groups": groups, "total": total, "kind": kind, "days": days})
        return render("crashes.html", context)

    @app.get("/crashes/{fingerprint}", response_class=HTMLResponse, include_in_schema=False)
    def crash_detail(
        request: Request,
        fingerprint: str,
        app_id: str | None = None,
        days: int = Query(default=90, ge=1, le=365),
        _: bool = Depends(require_admin),
    ):
        app_id = guard(app_id)
        since, until = queries.default_range(days)
        occurrences, total = queries.list_crashes(
            db, app_id, fingerprint=fingerprint, since=since, until=until, limit=100
        )
        groups, _total = queries.crash_groups(db, app_id, since=since, until=until, limit=200)
        group = next((g for g in groups if g["fingerprint"] == fingerprint), None)
        context = base_context(request, app_id, "crashes")
        context.update(
            {
                "fingerprint": fingerprint, "group": group, "occurrences": occurrences,
                "total": total, "days": days,
            }
        )
        return render("crash_detail.html", context)

    # -------------------------------------------------------------- charts
    @app.get("/charts", response_class=HTMLResponse, include_in_schema=False)
    def charts_page(
        request: Request,
        app_id: str | None = None,
        metric: str = "events",
        dimension: str = "screen",
        bucket: str = "day",
        days: int = Query(default=30, ge=1, le=365),
        _: bool = Depends(require_admin),
    ):
        app_id = guard(app_id)
        since, until = queries.default_range(days)
        metric = metric if metric in queries.METRIC_NAMES else "events"
        dimension = dimension if dimension in queries.DIMENSION_NAMES else "screen"
        bucket = bucket if bucket in ("hour", "day") else "day"
        context = base_context(request, app_id, "charts")
        context.update(
            {
                "metric": metric, "dimension": dimension, "bucket": bucket, "days": days,
                "source": queries.series_source(db, app_id, bucket, since, until),
                "points": queries.timeseries(db, app_id, metric, bucket=bucket, since=since, until=until),
                "top": queries.top_values(db, app_id, dimension, since=since, until=until, limit=12),
                "metrics": list(queries.METRIC_NAMES),
                "dimensions": list(queries.DIMENSION_NAMES),
            }
        )
        return render("charts.html", context)

    # ------------------------------------------------------------- devices
    @app.get("/devices", response_class=HTMLResponse, include_in_schema=False)
    def devices_page(
        request: Request,
        app_id: str | None = None,
        search: str | None = None,
        _: bool = Depends(require_admin),
    ):
        app_id = guard(app_id)
        devices, total = queries.list_devices(db, app_id, search=search, limit=100)
        context = base_context(request, app_id, "devices")
        context.update({"devices": devices, "total": total, "search": search})
        return render("devices.html", context)

    @app.get("/devices/{device_id}", response_class=HTMLResponse, include_in_schema=False)
    def device_detail(
        request: Request,
        device_id: str,
        app_id: str | None = None,
        _: bool = Depends(require_admin),
    ):
        app_id = guard(app_id)
        device = queries.get_device(db, device_id)
        if device is None or device["app_id"] != app_id:
            raise NotFound(f"device {device_id}")
        sessions, total = queries.list_sessions(db, app_id, device_id=device_id, limit=50)
        context = base_context(request, app_id, "devices")
        context.update({"device": device, "sessions": sessions, "total": total})
        return render("device_detail.html", context)

    # ------------------------------------------------------------ sessions
    @app.get("/sessions", response_class=HTMLResponse, include_in_schema=False)
    def sessions_page(
        request: Request,
        app_id: str | None = None,
        device_id: str | None = None,
        days: int = Query(default=7, ge=1, le=365),
        _: bool = Depends(require_admin),
    ):
        app_id = guard(app_id)
        since, until = queries.default_range(days)
        sessions, total = queries.list_sessions(
            db, app_id, device_id=device_id, since=since, until=until, limit=100
        )
        context = base_context(request, app_id, "sessions")
        context.update({"sessions": sessions, "total": total, "device_id": device_id, "days": days})
        return render("sessions.html", context)

    @app.get("/sessions/{session_id}", response_class=HTMLResponse, include_in_schema=False)
    def session_detail(
        request: Request,
        session_id: str,
        app_id: str | None = None,
        _: bool = Depends(require_admin),
    ):
        app_id = guard(app_id)
        session = queries.get_session(db, session_id)
        if session is None or session["app_id"] != app_id:
            raise NotFound(f"session {session_id}")
        events, event_total = queries.list_events(db, app_id, session_id=session_id, limit=100, order="asc")
        logs, log_total = queries.list_logs(db, app_id, session_id=session_id, limit=100, order="asc")
        crashes, crash_total = queries.list_crashes(db, app_id, session_id=session_id, limit=50)
        context = base_context(request, app_id, "sessions")
        context.update(
            {
                "session": session, "events": events, "event_total": event_total,
                "logs": logs, "log_total": log_total, "crashes": crashes,
            }
        )
        return render("session_detail.html", context)

    # -------------------------------------------------------- data browser
    @app.get("/data", response_class=HTMLResponse, include_in_schema=False)
    def data_page(
        request: Request,
        app_id: str | None = None,
        _: bool = Depends(require_admin),
    ):
        app_id = selected_app(app_id)
        context = base_context(request, app_id, "data")
        context.update(
            {"tables": queries.list_tables(db), "table": None, **_maintenance_context(db)}
        )
        return render("data.html", context)

    @app.get("/data/{table}", response_class=HTMLResponse, include_in_schema=False)
    def data_table(
        request: Request,
        table: str,
        app_id: str | None = None,
        order_by: str | None = None,
        order: str = "desc",
        filter_col: str | None = None,
        filter_val: str | None = None,
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
        _: bool = Depends(require_admin),
    ):
        app_id = selected_app(app_id)
        tables = queries.list_tables(db)
        if not any(t["name"] == table for t in tables):
            raise NotFoundTable(table)

        # `filter_col` is validated inside table_rows against PRAGMA table_info,
        # so an unknown column is ignored rather than reaching the SQL.
        filters = {filter_col: filter_val} if filter_col and filter_val is not None else {}
        rows, total, columns = queries.table_rows(
            db, table, limit=limit, offset=offset, order_by=order_by, order=order, filters=filters
        )
        context = base_context(request, app_id, "data")
        context.update(
            {
                "tables": tables, "table": table, "rows": rows, "total": total,
                "columns": columns, "order_by": order_by, "order": order,
                "limit": limit, "offset": offset,
                "filter_col": filter_col, "filter_val": filter_val,
                "foreign_keys": queries.foreign_keys(db, table),
                "row_links": _foreign_key_map(db, table),
                **_maintenance_context(db),
            }
        )
        return render("data.html", context)

    # ---------------------------------------------------------------- error
    @app.exception_handler(HTMLRedirect)
    def _redirect(request: Request, exc: HTMLRedirect):
        # An HTMX request must not be answered with a 3xx: the browser would
        # follow it transparently inside the XHR and swap the login page into
        # the fragment. htmx's own `HX-Redirect` performs a real navigation.
        if "HX-Redirect" in exc.headers:
            return Response(status_code=200, headers={"HX-Redirect": exc.headers["HX-Redirect"]})
        return RedirectResponse(url=exc.headers.get("Location", "/login"), status_code=303)

    @app.exception_handler(HTMLResponse_503)
    def _disabled(request: Request, exc: HTMLResponse_503):
        return render(
            "login.html",
            {"request": request, "disabled": True, "error": None, "next": "/"},
            status_code=503,
        )

    @app.exception_handler(NoApps)
    def _no_apps(request: Request, exc: NoApps):
        return render(
            "empty.html", {"request": request, "page": "overview"}, status_code=200
        )

    @app.exception_handler(NotFoundTable)
    def _no_table(request: Request, exc: NotFoundTable):
        context = base_context(request, None, "data")
        context.update(
            {
                "tables": queries.list_tables(db),
                "table": None,
                "error": str(exc),
                **_maintenance_context(db),
            }
        )
        return render("data.html", context, status_code=404)

    @app.exception_handler(NotFound)
    def _not_found(request: Request, exc: NotFound):
        return render(
            "not_found.html",
            {"request": request, "page": None, "what": exc.what},
            status_code=404,
        )


def replace_settings_window(settings: Settings, hours: int) -> Settings:
    from dataclasses import replace

    return replace(settings, alert_window_hours=hours)


def _alert_context(db: Database, settings: Settings) -> dict:
    """Alerts shown in the banner on every page.

    A handful of indexed counts per app — cheap enough to run on each render,
    and worth it: an alert nobody sees is not an alert.
    """
    if not settings.alerts_enabled:
        return {"alerts": [], "alert_report": None}
    report = alerts_module.evaluate(db, settings)
    return {"alerts": report.alerts, "alert_report": report}


def _maintenance_context(db: Database) -> dict:
    """What the nightly jobs have been doing — shown on the data browser page."""
    first, last = rollups.coverage(db, app_id="")
    return {
        "job_runs": rollups.recent_runs(db, limit=8),
        "rollup_coverage": {"first_day": first, "last_day": last},
    }


def _foreign_key_map(db: Database, table: str) -> dict[str, tuple[str, str]]:
    """column -> (referenced_table, referenced_column) for clickable FKs."""
    mapping: dict[str, tuple[str, str]] = {}
    for fk in queries.foreign_keys(db, table):
        mapping[fk["from"]] = (fk["table"], fk["to"])
    return mapping


# --------------------------------------------------------------------------- #
# Tiny control-flow exceptions (kept out of the route bodies)
# --------------------------------------------------------------------------- #

class HTMLRedirect(Exception):
    def __init__(self, headers: dict[str, str]):
        self.headers = headers


class HTMLResponse_503(Exception):
    pass


class NoApps(Exception):
    pass


class NotFound(Exception):
    """A single record that does not exist (device, session, crash group)."""

    def __init__(self, what: str):
        super().__init__(what)
        self.what = what


class NotFoundTable(Exception):
    def __init__(self, table: str):
        super().__init__(f"unknown table: {table}")
        self.table = table
