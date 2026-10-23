"""Exporting filtered events, and the links that fetch the result.

Three things this module refuses to do, each for a reason:

* **Never build the whole result in memory.** Rows are paged with the same
  keyset cursor the console uses and written straight out, so a large export
  costs a file, not a heap.
* **Never hand back a file that looks complete but is not.** The row cap is
  recorded on the job and reported in the response. A silently truncated export
  is the worst outcome available: it looks authoritative and is wrong.
* **Never let a download link outlive its welcome.** The link carries an HMAC and
  an expiry, and both are checked on every fetch — which is what makes it safe to
  hand out a URL that does not require the admin password.

Exports are always written in ascending time order, whatever the console was
sorted by, so the file reads chronologically.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import hmac
import io
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from . import audit, logquery
from .config import Settings
from .db import Database
from .migrations import utcnow_iso
from .redaction import redact_text, redact_value

FORMATS = ("csv", "json", "jsonl")

#: The exported schema. Fixed and documented on purpose: a CSV whose columns
#: depend on which row happened to come first is not something anyone can parse.
EXPORT_COLUMNS = (
    "event_time",
    "received_at",
    "event_id",
    "source",
    "severity",
    "event_type",
    "service_name",
    "environment",
    "app_version",
    "release_id",
    "request_id",
    "trace_id",
    "span_id",
    "route_template",
    "http_method",
    "status_code",
    "duration_ms",
    "error_code",
    "error_fingerprint",
    "message",
    "attributes",
    "category",
    "source_type",
    "source_id",
    "platform_version",
)

CONTENT_TYPES = {
    "csv": "text/csv; charset=utf-8",
    "json": "application/json",
    "jsonl": "application/x-ndjson",
}

_BATCH = 200


class ExportError(ValueError):
    """Something about the request means we should not run it."""


# --------------------------------------------------------------------------- #
# Download tokens
# --------------------------------------------------------------------------- #
def _signing_key(settings: Settings) -> bytes:
    """Derived from the admin password, so it rotates with it.

    Not the password itself: if a token is ever leaked, it must not help anyone
    guess the thing that signs it.
    """
    base = (settings.admin_password or "") + "|powerbase-export-download|v1"
    return hashlib.sha256(base.encode("utf-8")).digest()


def sign_download_token(job_id: str, expires_at: str, settings: Settings) -> str:
    payload = f"{job_id}|{expires_at}"
    mac = hmac.new(_signing_key(settings), payload.encode("utf-8"), hashlib.sha256).hexdigest()[:40]
    return base64.urlsafe_b64encode(f"{payload}|{mac}".encode("utf-8")).decode("ascii").rstrip("=")


def verify_download_token(token: str, settings: Settings) -> tuple[str, str] | None:
    """Return ``(job_id, expires_at)`` when the token is genuine, else ``None``."""
    if not token:
        return None
    padded = token + "=" * (-len(token) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded).decode("utf-8")
    except Exception:
        return None
    job_id, separator, rest = raw.partition("|")
    expires_at, separator2, mac = rest.rpartition("|")
    if not (job_id and separator and separator2 and expires_at and mac):
        return None
    expected = hmac.new(
        _signing_key(settings), f"{job_id}|{expires_at}".encode("utf-8"), hashlib.sha256
    ).hexdigest()[:40]
    # Constant time: a token comparison that leaks timing is a token comparison
    # that can be guessed a character at a time.
    if not hmac.compare_digest(mac, expected):
        return None
    return job_id, expires_at


def _expired(expires_at: str | None) -> bool:
    return bool(expires_at and expires_at <= utcnow_iso())


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #
def _batched(iterable: Iterable[Any], size: int = _BATCH) -> Iterator[list[Any]]:
    batch: list[Any] = []
    for item in iterable:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def iter_events(
    db: Database,
    *,
    app_id: str,
    since: str | None = None,
    until: str | None = None,
    filters: list[logquery.Filter] | None = None,
    q: str | None = None,
    max_rows: int | None = None,
    chunk: int = 1000,
    timeout_ms: int = logquery.QUERY_TIMEOUT_MS,
) -> Iterator[dict]:
    """Yield matching events in ascending time order, one keyset page at a time.

    Bounded memory regardless of how many rows match: the page is consumed and
    discarded before the next one is fetched.
    """
    produced = 0
    cursor: str | None = None

    while True:
        page = logquery.query_log_events(
            db,
            app_id=app_id,
            since=since,
            until=until,
            filters=filters or [],
            q=q,
            sort="event_time",
            direction="asc",
            limit=chunk,
            cursor=cursor,
            timeout_ms=timeout_ms,
        )
        if not page.items:
            return

        for item in page.items:
            yield item
            produced += 1
            if max_rows is not None and produced >= max_rows:
                return

        cursor = page.next_cursor
        if not cursor:
            return


def _redact(row: dict, enabled: bool) -> dict:
    """Redaction, applied again on the way out.

    Defence in depth: rows are already redacted at ingest, but a row written
    while redaction was switched off would otherwise leave the building
    unredacted. The export path is the last place that can catch it.
    """
    if not enabled:
        return row
    out = dict(row)
    if out.get("message"):
        out["message"] = redact_text(str(out["message"]))
    if isinstance(out.get("attributes"), dict):
        out["attributes"] = redact_value(out["attributes"])
    return out


def _cell(row: dict, column: str) -> Any:
    value = row.get(column)
    if column == "attributes":
        return json.dumps(value or {}, separators=(",", ":"), ensure_ascii=False, default=str)
    if value is None:
        return ""
    return value


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #
def stream_rows(rows: Iterable[dict], fmt: str) -> Iterator[str]:
    """Format rows as text, in chunks, without holding the whole set."""
    if fmt == "csv":
        yield from _stream_csv(rows)
    elif fmt == "jsonl":
        yield from _stream_jsonl(rows)
    elif fmt == "json":
        yield from _stream_json(rows)
    else:
        raise ExportError(f"unsupported format {fmt!r}")


def _stream_csv(rows: Iterable[dict]) -> Iterator[str]:
    # The header goes out before the first row is known, so an export that
    # matches nothing is still a parseable CSV that describes its own columns.
    header = io.StringIO()
    csv.writer(header).writerow(EXPORT_COLUMNS)
    yield header.getvalue()

    for batch in _batched(rows):
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        for row in batch:
            writer.writerow([_cell(row, column) for column in EXPORT_COLUMNS])
        yield buffer.getvalue()


def _stream_jsonl(rows: Iterable[dict]) -> Iterator[str]:
    for batch in _batched(rows):
        buffer = io.StringIO()
        for row in batch:
            buffer.write(json.dumps(_ordered(row), ensure_ascii=False, default=str))
            buffer.write("\n")
        yield buffer.getvalue()


def _stream_json(rows: Iterable[dict]) -> Iterator[str]:
    """A JSON array, streamed. Valid JSON without ever being valid in memory."""
    first = True
    wrote_anything = False
    for batch in _batched(rows):
        buffer = io.StringIO()
        if first:
            buffer.write("[\n")
            first = False
        for row in batch:
            if wrote_anything:
                buffer.write(",\n")
            buffer.write("  " + json.dumps(_ordered(row), ensure_ascii=False, default=str))
            wrote_anything = True
        yield buffer.getvalue()
    yield "\n]\n" if wrote_anything else "[]\n"


def _ordered(row: dict) -> dict:
    return {column: row.get(column) for column in EXPORT_COLUMNS}


# --------------------------------------------------------------------------- #
# Jobs
# --------------------------------------------------------------------------- #
def export_dir(settings: Settings) -> Path:
    path = Path(settings.export_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path


def result_path(settings: Settings, job_id: str, fmt: str) -> Path:
    return export_dir(settings) / f"{job_id}.{fmt}"


def create_job(
    db: Database,
    settings: Settings,
    *,
    app_id: str,
    fmt: str,
    definition: dict,
    requested_by: str | None = None,
    ttl_seconds: int | None = None,
) -> dict:
    """Record an export. The file is written by :func:`run_job`."""
    if fmt not in FORMATS:
        raise ExportError(f"format must be one of {', '.join(FORMATS)}")
    if db.one("SELECT id FROM apps WHERE id = ?", (app_id,)) is None:
        raise ExportError(f"unknown project: {app_id}")

    job_id = uuid.uuid4().hex
    now = datetime.now(timezone.utc)
    ttl = ttl_seconds if ttl_seconds is not None else settings.export_ttl_seconds
    expires_at = (now + timedelta(seconds=ttl)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    with db.write() as conn:
        conn.execute(
            """
            INSERT INTO export_jobs
                (id, app_id, requested_by, format, filter_definition, status,
                 created_at, expires_at)
            VALUES (?, ?, ?, ?, ?, 'queued', ?, ?)
            """,
            (
                job_id,
                app_id,
                requested_by,
                fmt,
                json.dumps(definition, separators=(",", ":"), default=str),
                utcnow_iso(),
                expires_at,
            ),
        )
        # Same transaction: a job recorded as created that does not exist would
        # be a worse audit entry than none at all.
        audit.record_in(
            conn,
            audit.EXPORT_CREATED,
            app_id=app_id,
            actor=requested_by,
            target_type="export_job",
            target_id=job_id,
            metadata={"format": fmt, "definition": definition},
        )

    return get_job(db, job_id)  # type: ignore[return-value]


def run_job(db: Database, settings: Settings, job_id: str) -> dict:
    """Write the file for a queued job.

    Synchronous and bounded: the row cap in `settings.export_max_rows` stops it,
    and hitting the cap is recorded rather than hidden.
    """
    job = get_job(db, job_id)
    if job is None:
        raise ExportError(f"no export job {job_id}")
    if job["status"] not in ("queued", "running"):
        raise ExportError(f"job {job_id} is {job['status']}, not runnable")

    with db.write() as conn:
        conn.execute(
            "UPDATE export_jobs SET status = 'running', started_at = ? WHERE id = ?",
            (utcnow_iso(), job_id),
        )

    definition = job["definition"]
    try:
        filters = logquery.parse_filters(definition.get("filters"))
        window = definition.get("timeRange") or {}
        since = window.get("from")
        until = window.get("to")
    except logquery.QueryError as exc:
        return _fail(db, job_id, str(exc))

    max_rows = settings.export_max_rows
    target = result_path(settings, job_id, job["format"])

    rows_written = 0
    bytes_written = 0
    truncated = False

    try:
        rows = iter_events(
            db,
            app_id=job["app_id"],
            since=since,
            until=until,
            filters=filters,
            q=definition.get("q"),
            # One row past the cap, so "exactly at the cap" is distinguishable
            # from "there was more".
            max_rows=max_rows + 1,
        )
        redacted = (_redact(row, settings.redaction_enabled) for row in rows)

        def limited():
            """Stop at the cap, and record that there was more.

            Counting here rather than re-reading the finished file: csv.writer
            quotes fields containing newlines, so a row can span several lines
            and a line count would be wrong.
            """
            nonlocal rows_written, truncated
            for row in redacted:
                if rows_written >= max_rows:
                    truncated = True
                    return
                rows_written += 1
                yield row

        with open(target, "w", encoding="utf-8", newline="") as handle:
            for chunk in stream_rows(limited(), job["format"]):
                handle.write(chunk)
                bytes_written += len(chunk.encode("utf-8"))
    except logquery.QueryError as exc:
        return _fail(db, job_id, str(exc))
    except Exception as exc:  # pragma: no cover - disk full, permissions, ...
        return _fail(db, job_id, f"{type(exc).__name__}: {exc}")

    with db.write() as conn:
        conn.execute(
            """
            UPDATE export_jobs
               SET status = 'completed', completed_at = ?, row_count = ?,
                   result_bytes = ?, result_path = ?, truncated = ?
             WHERE id = ?
            """,
            (
                utcnow_iso(),
                rows_written,
                bytes_written,
                str(target),
                1 if truncated else 0,
                job_id,
            ),
        )

    return get_job(db, job_id)  # type: ignore[return-value]


def _fail(db: Database, job_id: str, message: str) -> dict:
    with db.write() as conn:
        conn.execute(
            """
            UPDATE export_jobs
               SET status = 'failed', completed_at = ?, sanitized_error = ?
             WHERE id = ?
            """,
            (utcnow_iso(), message[:500], job_id),
        )
    return get_job(db, job_id)  # type: ignore[return-value]


# --------------------------------------------------------------------------- #
# Reading jobs back
# --------------------------------------------------------------------------- #
def get_job(db: Database, job_id: str) -> dict | None:
    row = db.one("SELECT * FROM export_jobs WHERE id = ?", (job_id,))
    return _job_row(row) if row is not None else None


def list_jobs(db: Database, app_id: str | None = None, limit: int = 50) -> list[dict]:
    params: tuple = ()
    sql = "SELECT * FROM export_jobs"
    if app_id:
        sql += " WHERE app_id = ?"
        params = (app_id,)
    sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
    rows = db.query(sql, params + (max(1, min(int(limit), 200)),))
    return [_job_row(row) for row in rows]


def _job_row(row) -> dict:
    out = dict(row)
    raw = out.pop("filter_definition", None)
    try:
        out["definition"] = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        out["definition"] = {}
    out["truncated"] = bool(out.get("truncated"))
    out["expired"] = _expired(out.get("expires_at"))
    return out


def delete_job(db: Database, settings: Settings, job_id: str) -> bool:
    job = get_job(db, job_id)
    if job is None:
        return False
    if job.get("result_path"):
        Path(job["result_path"]).unlink(missing_ok=True)
    with db.write() as conn:
        conn.execute("DELETE FROM export_jobs WHERE id = ?", (job_id,))
        audit.record_in(
            conn,
            audit.EXPORT_DELETED,
            app_id=job["app_id"],
            target_type="export_job",
            target_id=job_id,
            metadata={"format": job["format"], "row_count": job.get("row_count")},
        )
    return True


def expire_jobs(db: Database, settings: Settings, now: datetime | None = None) -> dict:
    """Mark past-expiry jobs expired and delete their files.

    The file is the point. A row that says "expired" while the CSV is still on
    disk has not expired.
    """
    moment = now or datetime.now(timezone.utc)
    stamp = moment.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    rows = db.query(
        """
        SELECT id, app_id, result_path, format FROM export_jobs
         WHERE status = 'completed' AND expires_at IS NOT NULL AND expires_at <= ?
        """,
        (stamp,),
    )
    removed_files = 0
    for row in rows:
        # `Path.unlink(missing_ok=True)` returns None, so it cannot be used as
        # its own success test — `if path.unlink(...)` counts zero deletions
        # while deleting the files, and the nightly report then lies.
        path = Path(row["result_path"]) if row["result_path"] else None
        if path is not None and path.is_file():
            path.unlink(missing_ok=True)
            removed_files += 1
        with db.write() as conn:
            conn.execute(
                "UPDATE export_jobs SET status = 'expired', result_path = NULL WHERE id = ?",
                (row["id"],),
            )
            audit.record_in(
                conn,
                audit.EXPORT_EXPIRED,
                app_id=row["app_id"],
                target_type="export_job",
                target_id=row["id"],
                metadata={"format": row["format"]},
            )
    return {"expired": len(rows), "files_removed": removed_files}


def complete_queued(db: Database, settings: Settings, limit: int = 5) -> dict:
    """Finish jobs that were deferred. Called by the maintenance pass."""
    rows = db.query(
        "SELECT id FROM export_jobs WHERE status = 'queued' ORDER BY created_at LIMIT ?",
        (max(1, min(int(limit), 50)),),
    )
    done, failed = 0, 0
    for row in rows:
        result = run_job(db, settings, row["id"])
        if result and result.get("status") == "completed":
            done += 1
        else:
            failed += 1
    return {"queued": len(rows), "completed": done, "failed": failed}
