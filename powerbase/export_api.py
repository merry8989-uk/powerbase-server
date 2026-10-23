"""The export API, and the audit view.

Exports are admin-guarded, and every one of them writes an audit row. The
download link is the one endpoint that is *not* admin-guarded — it carries a
signed, expiring token instead, which is what makes it a link you can hand to
someone (or open in a browser) without also handing over the admin password.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from . import audit, exports
from .config import Settings
from .db import Database

DOWNLOAD_CHUNK = 64 * 1024


class ExportIn(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    app_id: str
    format: str = "csv"
    time_range: dict[str, Any] | None = Field(default=None, alias="timeRange")
    filters: list[dict[str, Any]] | None = None
    q: str | None = None
    #: Write the job but do not run it now. The maintenance pass finishes it.
    defer: bool = False
    #: Override the default link lifetime, in seconds.
    ttl_seconds: int | None = Field(default=None, ge=60, le=90 * 24 * 60 * 60)


def register_export_routes(
    app, db: Database, settings: Settings, require_admin
) -> None:
    def public_base(request: Request) -> str:
        """The base a browser should use.

        Honours X-Forwarded-Proto/Host, because behind a proxy the app sees
        `http://127.0.0.1:8080` while the user is on `https://logs.example.com`,
        and a download link that points at the wrong scheme is a broken link.
        """
        proto = request.headers.get("x-forwarded-proto", request.url.scheme)
        host = request.headers.get("x-forwarded-host") or request.headers.get("host")
        return f"{proto}://{host}" if host else str(request.base_url).rstrip("/")

    def with_download(request: Request, job: dict) -> dict:
        if job.get("status") == "completed" and not job.get("expired"):
            token = exports.sign_download_token(job["id"], job["expires_at"], settings)
            job["download_url"] = (
                f"{public_base(request)}/v1/exports/{job['id']}/download?token={token}"
            )
        return job

    def definition_from(body: ExportIn) -> dict:
        window = body.time_range or {}
        unknown = set(window) - {"from", "to"}
        if unknown:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, f"unknown timeRange keys: {sorted(unknown)}"
            )
        for label, value in (("from", window.get("from")), ("to", window.get("to"))):
            if value is not None and not isinstance(value, str):
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST, f"timeRange.{label} must be a string"
                )
        # Validate before anything is written, so a bad filter never leaves a
        # job row behind.
        from . import logquery

        try:
            logquery.parse_filters(body.filters)
        except logquery.QueryError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None

        return {
            "filters": body.filters or [],
            "timeRange": {"from": window.get("from"), "to": window.get("to")},
            "q": body.q,
        }

    # ------------------------------------------------------------------ create
    @app.post("/v1/exports", status_code=status.HTTP_201_CREATED, tags=["exports"])
    def create_export(
        body: ExportIn, request: Request, _: None = Depends(require_admin)
    ) -> dict:
        definition = definition_from(body)

        try:
            job = exports.create_job(
                db,
                settings,
                app_id=body.app_id,
                fmt=body.format,
                definition=definition,
                requested_by="admin",
                ttl_seconds=body.ttl_seconds,
            )
        except exports.ExportError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None

        if not body.defer:
            try:
                job = exports.run_job(db, settings, job["id"])
            except exports.ExportError as exc:
                raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None

        return with_download(request, job)

    # -------------------------------------------------------------------- list
    @app.get("/v1/exports", tags=["exports"])
    def list_exports(
        request: Request,
        app_id: str | None = None,
        limit: int = Query(default=50, ge=1, le=200),
        _: None = Depends(require_admin),
    ) -> dict:
        jobs = [with_download(request, job) for job in exports.list_jobs(db, app_id, limit)]
        return {"count": len(jobs), "items": jobs}

    @app.get("/v1/exports/{job_id}", tags=["exports"])
    def get_export(job_id: str, request: Request, _: None = Depends(require_admin)) -> dict:
        job = exports.get_job(db, job_id)
        if job is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such export")
        return with_download(request, job)

    @app.delete("/v1/exports/{job_id}", tags=["exports"])
    def delete_export(job_id: str, _: None = Depends(require_admin)) -> dict:
        if not exports.delete_job(db, settings, job_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such export")
        return {"id": job_id, "deleted": True}

    # ---------------------------------------------------------------- download
    @app.get("/v1/exports/{job_id}/download", tags=["exports"], include_in_schema=False)
    def download_export(
        job_id: str,
        token: str = Query(default=""),
        x_forwarded_for: str | None = Header(default=None),
    ):
        """Stream the result file.

        The token is the credential: it is signed, it names one job, and it
        expires. That is deliberate — a download link that needed the admin
        password could not be handed to anyone.
        """
        verified = exports.verify_download_token(token, settings)
        if verified is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid download token")
        token_job, token_expiry = verified
        if token_job != job_id:
            # A token for another job is not a token for this one.
            raise HTTPException(status.HTTP_403_FORBIDDEN, "token is for a different export")

        job = exports.get_job(db, job_id)
        if job is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such export")
        # Expiry is checked before status, because an expired job's status is
        # itself "expired" — testing status first would answer 409 "not
        # downloadable" for what is really a 410 "this link is gone", and the
        # difference matters to whoever is holding the link.
        if job.get("expired") or job["expires_at"] != token_expiry:
            raise HTTPException(status.HTTP_410_GONE, "this download link has expired")
        if job["status"] != "completed":
            raise HTTPException(
                status.HTTP_409_CONFLICT, f"export is {job['status']}, not downloadable"
            )

        path = Path(job["result_path"] or "")
        if not path.is_file():
            raise HTTPException(status.HTTP_410_GONE, "the export file is no longer available")

        audit.record(
            db,
            audit.EXPORT_DOWNLOADED,
            app_id=job["app_id"],
            actor=x_forwarded_for or "download-link",
            target_type="export_job",
            target_id=job_id,
            metadata={"format": job["format"], "row_count": job.get("row_count")},
        )

        filename = f"powerbase-{job['app_id']}-{job_id[:8]}.{job['format']}"

        def stream():
            with open(path, "rb") as handle:
                while True:
                    chunk = handle.read(DOWNLOAD_CHUNK)
                    if not chunk:
                        break
                    yield chunk

        headers = {
            "Content-Disposition": f'attachment; filename="{filename}"',
            # A truncated export says so in the file itself, so a script that
            # only reads headers still has a chance to notice.
            "X-Powerbase-Truncated": "true" if job.get("truncated") else "false",
            "X-Powerbase-Rows": str(job.get("row_count") or 0),
        }
        return StreamingResponse(
            stream(), media_type=exports.CONTENT_TYPES.get(job["format"], "application/octet-stream"),
            headers=headers,
        )

    # ------------------------------------------------------------------- audit
    @app.get("/v1/audit", tags=["audit"])
    def list_audit(
        app_id: str | None = None,
        event_type: str | None = None,
        outcome: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        _: None = Depends(require_admin),
    ) -> dict:
        return audit.list_events(
            db,
            app_id=app_id,
            event_type=event_type,
            outcome=outcome,
            since=since,
            until=until,
            limit=limit,
            offset=offset,
        )

    @app.get("/v1/audit/types", tags=["audit"])
    def audit_types(_: None = Depends(require_admin)) -> dict:
        rows = db.query("SELECT DISTINCT event_type FROM audit_events ORDER BY event_type")
        return {"items": [row["event_type"] for row in rows]}
