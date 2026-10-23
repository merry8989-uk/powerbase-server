"""FastAPI application — the ingestion service.

Write endpoints are the hot path and are kept deliberately thin: authenticate,
check the app matches the key, check the batch size, hand off to a transactional
repository function. Reads (query endpoints) are Phase 3 work and are not here.
"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse

from . import repository as repo
from .config import Settings
from . import deployment
from .db import Database
from .migrations import run_migrations, utcnow_iso
from .models import (
    CrashesBatch,
    EventsBatch,
    KeyCreateIn,
    LogEventsBatch,
    LogsBatch,
    SessionEndIn,
    SessionStartIn,
)
from .dashboard import register_dashboard_routes
from .advanced_api import register_advanced_routes
from .export_api import register_export_routes
from .log_query_api import register_log_query_routes
from .otlp import register_otlp_routes
from .status_api import register_status_routes
from .team_api import register_team_routes
from .query_api import register_query_routes
from .ratelimit import TokenBucketLimiter
from .dashboard_auth import check_password
from .provision import list_keys, mint_key, revoke_key, rotate_key
from .security import VerificationCache, parse_token, verify_secret
from .version import __version__


@dataclass(frozen=True)
class AuthContext:
    app_id: str
    key_id: str
    scope: str


# last_used_at bookkeeping: at most one write per key per minute.
_touch_lock = threading.Lock()
_touch_seen: dict[str, float] = {}
_TOUCH_INTERVAL = 60.0


logger = logging.getLogger("powerbase")


def create_app(settings: Settings | None = None, db: Database | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    db = db or Database(settings.db_path)
    limiter = TokenBucketLimiter(settings.rate_per_second, settings.rate_burst)
    # A second bucket, keyed by project rather than by key. Without it, one
    # project could mint many keys and spend everyone else's headroom.
    project_limiter = TokenBucketLimiter(
        settings.project_rate_per_second, settings.project_rate_burst
    )
    verifications = VerificationCache(ttl_seconds=settings.auth_cache_seconds)

    run_migrations(db, settings.migrations_dir)

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        # Two things in this build are in-process and assume one process. Say so
        # before starting, rather than letting an operator discover it from
        # rate limits that do not add up. See `powerbase/deployment.py`.
        for warning in deployment.warnings_for(settings):
            logger.warning("deployment: %s", warning)

        # The nightly maintenance pass runs on its own thread while the server
        # is up. `scripts/maintenance.py` is the alternative for cron users.
        scheduler = None
        if settings.maintenance_enabled:
            from .scheduler import MaintenanceScheduler

            scheduler = MaintenanceScheduler(db, settings, logger=logger.info)
            scheduler.start()
            application.state.scheduler = scheduler
            logger.info(
                "maintenance scheduler started (rollup hour %02d:00 UTC, interval %ss)",
                settings.rollup_hour,
                settings.maintenance_interval_seconds,
            )
        try:
            yield
        finally:
            if scheduler is not None:
                scheduler.stop()

    app = FastAPI(
        title="Powerbase Ingestion API",
        version=__version__,
        description="Self-hosted diagnostics and usage-log ingestion.",
        lifespan=lifespan,
    )
    app.state.scheduler = None
    app.state.settings = settings
    app.state.db = db
    app.state.limiter = limiter
    app.state.verifications = verifications
    app.state.deployment = deployment.summarise(settings)

    # ------------------------------------------------------ access logging
    if settings.access_log:
        http_logger = logging.getLogger("powerbase.http")

        @app.middleware("http")
        async def access_log(request: Request, call_next):
            started = time.perf_counter()
            try:
                response = await call_next(request)
            except Exception:
                http_logger.exception(
                    "http_request_failed",
                    extra={"fields": {
                        "method": request.method,
                        "path": request.url.path,
                        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                    }},
                )
                raise

            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            response.headers["X-Response-Time-Ms"] = f"{duration_ms}"

            if not request.url.path.startswith("/static"):
                http_logger.info(
                    "http_request",
                    extra={"fields": {
                        "method": request.method,
                        "path": request.url.path,
                        "status": response.status_code,
                        "duration_ms": duration_ms,
                        "app_id": getattr(request.state, "app_id", None),
                    }},
                )
            return response

    # ------------------------------------------------------------- auth
    def _touch_key(key_id: str) -> None:
        now = time.monotonic()
        with _touch_lock:
            if now - _touch_seen.get(key_id, 0.0) < _TOUCH_INTERVAL:
                return
            _touch_seen[key_id] = now
        try:
            with db.write() as conn:
                conn.execute(
                    "UPDATE api_keys SET last_used_at = ? WHERE id = ?",
                    (utcnow_iso(), key_id),
                )
        except Exception:  # never fail a request over bookkeeping
            pass

    def require_scope(scope: str):
        def dependency(
            request: Request, authorization: str = Header(default="")
        ) -> AuthContext:
            if not authorization.lower().startswith("bearer "):
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="missing bearer token",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            token = authorization.split(" ", 1)[1].strip()
            parts = parse_token(token)
            if parts is None:
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, "malformed token")

            row = db.one(
                "SELECT id, app_id, key_hash, scope, revoked_at, expires_at "
                "FROM api_keys WHERE id = ?",
                (parts.key_id,),
            )
            if row is None or row["revoked_at"] is not None:
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, "unknown or revoked key")
            # Checked before the Argon2 verify, so refusing an expired key costs
            # nothing. Both timestamps are the same fixed-width UTC format, so
            # the string comparison is a real time comparison.
            if row["expires_at"] is not None and row["expires_at"] <= utcnow_iso():
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, "key expired")
            # The expensive Argon2 check is skipped when this exact token was
            # verified moments ago; the row above is still re-read every time,
            # so revocation and scope are never stale.
            if not verifications.get(parts.key_id, token):
                if not verify_secret(row["key_hash"], parts.secret):
                    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")
                verifications.put(parts.key_id, token)
            if row["scope"] != scope:
                raise HTTPException(
                    status.HTTP_403_FORBIDDEN, f"key does not have '{scope}' scope"
                )

            allowed, retry_after = limiter.allow(parts.key_id)
            if not allowed:
                raise HTTPException(
                    status.HTTP_429_TOO_MANY_REQUESTS,
                    "rate limit exceeded",
                    headers={"Retry-After": str(max(1, int(retry_after + 0.999)))},
                )
            project_allowed, project_retry = project_limiter.allow(row["app_id"])
            if not project_allowed:
                raise HTTPException(
                    status.HTTP_429_TOO_MANY_REQUESTS,
                    "project rate limit exceeded",
                    headers={"Retry-After": str(max(1, int(project_retry + 0.999)))},
                )

            _touch_key(parts.key_id)
            # Stashed for the access log, so every request line names the app and
            # key it acted as without the handler having to say so.
            request.state.app_id = row["app_id"]
            request.state.key_id = parts.key_id
            return AuthContext(app_id=row["app_id"], key_id=parts.key_id, scope=row["scope"])

        return dependency

    ingest_auth = require_scope("ingest")

    # -------------------------------------------------------- guards
    def _guard(auth: AuthContext, body_app_id: str, count: int) -> None:
        if body_app_id != auth.app_id:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "app_id does not match the authenticated key",
            )
        if count > settings.max_batch_size:
            raise HTTPException(
                413,  # Payload Too Large (constant name varies across Starlette versions)
                f"batch exceeds max size of {settings.max_batch_size}",
            )

    def _batch_response(batch_id: str, kind: str, result: dict) -> JSONResponse:
        body = {"batch_id": batch_id, "kind": kind, **result}
        code = status.HTTP_200_OK if result.get("duplicate") else status.HTTP_202_ACCEPTED
        return JSONResponse(status_code=code, content=body)

    # ------------------------------------------------------- endpoints
    @app.get("/v1/", include_in_schema=False)
    def api_root() -> dict:
        return {
            "service": "powerbase",
            "version": __version__,
            "docs": "/docs",
            "dashboard": "/" if settings.dashboard_enabled else None,
        }

    @app.get("/v1/health")
    def health() -> dict:
        try:
            mode = db.scalar("PRAGMA journal_mode")
            db.scalar("SELECT 1")
        except Exception as exc:  # pragma: no cover - only on a broken DB
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"db unavailable: {exc}")
        return {"status": "ok", "db": "ok", "journal_mode": mode, "version": __version__}

    @app.post("/v1/ingest/events")
    def ingest_events(batch: EventsBatch, auth: AuthContext = Depends(ingest_auth)):
        _guard(auth, batch.app_id, len(batch.events))
        result = repo.write_events(db, auth.app_id, batch, settings.redaction_enabled)
        return _batch_response(batch.batch_id, "events", result)

    @app.post("/v1/ingest/logs")
    def ingest_logs(batch: LogsBatch, auth: AuthContext = Depends(ingest_auth)):
        _guard(auth, batch.app_id, len(batch.logs))
        result = repo.write_logs(db, auth.app_id, batch, settings.redaction_enabled)
        return _batch_response(batch.batch_id, "logs", result)

    @app.post("/v1/ingest/crashes")
    def ingest_crashes(batch: CrashesBatch, auth: AuthContext = Depends(ingest_auth)):
        _guard(auth, batch.app_id, len(batch.crashes))
        result = repo.write_crashes(db, auth.app_id, batch, settings.redaction_enabled)
        return _batch_response(batch.batch_id, "crashes", result)

    @app.post("/v1/ingest/session/start", status_code=status.HTTP_202_ACCEPTED)
    def session_start(payload: SessionStartIn, auth: AuthContext = Depends(ingest_auth)):
        result = repo.start_session(db, auth.app_id, payload)
        return JSONResponse(status_code=status.HTTP_202_ACCEPTED, content=result)

    @app.post("/v1/ingest/session/end")
    def session_end(payload: SessionEndIn, auth: AuthContext = Depends(ingest_auth)):
        result = repo.end_session(db, auth.app_id, payload)
        if not result["updated"]:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "session not found")
        return JSONResponse(status_code=status.HTTP_200_OK, content=result)

    # ------------------------------------------------ observability ingestion
    @app.post("/v1/ingest/log-events")
    def ingest_log_events(
        batch: LogEventsBatch, auth: AuthContext = Depends(ingest_auth)
    ):
        """Log events in the observability shape: browser, server, OTel.

        A different path from ``/v1/ingest/events`` on purpose. That endpoint
        takes the mobile-analytics shape the shipped Android SDK already sends,
        and changing its body would break every installed client. Both feed the
        same dashboard; they are two models, not two versions of one.
        """
        _guard(auth, batch.app_id, len(batch.events))
        result = repo.write_log_events(
            db,
            auth.app_id,
            batch,
            settings.redaction_enabled,
            settings.log_events_max_attributes,
        )
        return _batch_response(batch.batch_id, "log_events", result)

    # --------------------------------------------------- key administration
    # Guarded by the admin password, not by an API key: no key, ingest or read,
    # may mint or revoke keys. Otherwise a leaked key could renew itself.
    def require_admin(x_admin_password: str | None = Header(default=None)) -> None:
        configured = settings.admin_password
        if not configured:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "key administration is disabled: no admin password is configured",
            )
        if not x_admin_password or not check_password(configured, x_admin_password):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid admin password")

    @app.get("/v1/keys")
    def keys_index(app_id: str | None = None, _: None = Depends(require_admin)) -> dict:
        return {"items": list_keys(db, app_id)}

    @app.post("/v1/keys", status_code=status.HTTP_201_CREATED)
    def keys_create(payload: KeyCreateIn, _: None = Depends(require_admin)) -> dict:
        if db.one("SELECT id FROM apps WHERE id = ?", (payload.app_id,)) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown app_id")
        key_id, token = mint_key(
            db,
            payload.app_id,
            payload.scope,
            payload.label,
            payload.expires_in_days,
        )
        return {
            "key_id": key_id,
            "app_id": payload.app_id,
            "scope": payload.scope,
            "expires_in_days": payload.expires_in_days,
            "token": token,
            "note": "store this token now - it is not retrievable again",
        }

    @app.post("/v1/keys/{key_id}/rotate")
    def keys_rotate(
        key_id: str,
        expires_in_days: int | None = None,
        _: None = Depends(require_admin),
    ) -> dict:
        rotated = rotate_key(db, key_id, expires_in_days)
        if rotated is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, "unknown or already revoked key"
            )
        new_key_id, token = rotated
        return {
            "key_id": new_key_id,
            "rotated_from": key_id,
            "expires_in_days": expires_in_days,
            "token": token,
            "note": "the previous key is revoked - store this token now",
        }

    @app.post("/v1/keys/{key_id}/revoke")
    def keys_revoke(key_id: str, _: None = Depends(require_admin)) -> dict:
        if not revoke_key(db, key_id):
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, "unknown or already revoked key"
            )
        return {"key_id": key_id, "revoked": True}

    # ------------------------------------------------- read API + dashboard
    # Reads use a separate, read-scoped key so a client-embedded ingest key can
    # never reach them. The dashboard is served by the same process.
    register_query_routes(app, db, settings, require_scope("read"))
    register_log_query_routes(
        app, db, settings, require_scope("read"), require_admin
    )
    register_status_routes(app, db, settings, require_admin)
    register_otlp_routes(app, db, settings, ingest_auth)
    register_export_routes(app, db, settings, require_admin)
    register_team_routes(app, db, settings, require_admin)
    register_advanced_routes(app, db, settings, require_admin)
    register_dashboard_routes(app, db, settings)

    return app
