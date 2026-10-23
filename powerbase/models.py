"""Request models for the ingestion API.

Pydantic does the heavy lifting: type coercion, ISO-8601 timestamp parsing and
per-field length limits. Unknown fields are ignored rather than rejected so an
older server never breaks a newer SDK.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

#: The mobile-analytics `logs` table has no TRACE level. This type must match
#: its CHECK constraint exactly, or a bad value reaches SQLite and comes back as
#: an IntegrityError rather than a validation error.
LogLevel = Literal["DEBUG", "INFO", "WARN", "ERROR", "FATAL"]

#: Log events do have TRACE (migration 0009). Deliberately a separate type: the
#: two tables have two vocabularies, and sharing one type between them is what
#: let a TRACE severity through the analytics path.
LogEventLevel = Literal["TRACE", "DEBUG", "INFO", "WARN", "ERROR", "FATAL"]
CrashKind = Literal["java", "native", "anr", "other"]
EndReason = Literal["background", "crash", "timeout"]

_ID = Field(min_length=1, max_length=128)


def to_iso(dt: datetime) -> str:
    """Normalise any datetime to a UTC ISO-8601 string with millisecond precision."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class _Base(BaseModel):
    model_config = ConfigDict(extra="ignore")


class DeviceInfo(_Base):
    model: str | None = Field(default=None, max_length=128)
    manufacturer: str | None = Field(default=None, max_length=128)
    os_version: str | None = Field(default=None, max_length=64)
    app_version: str | None = Field(default=None, max_length=64)
    sdk_version: str | None = Field(default=None, max_length=32)
    locale: str | None = Field(default=None, max_length=32)


# ---------------------------------------------------------------- events
class EventIn(_Base):
    ts: datetime
    device_id: str = _ID
    session_id: str | None = Field(default=None, max_length=128)
    name: str = Field(min_length=1, max_length=128)
    category: str | None = Field(default=None, max_length=64)
    screen: str | None = Field(default=None, max_length=128)
    properties: dict[str, Any] = Field(default_factory=dict)
    sdk_version: str | None = Field(default=None, max_length=32)


class EventsBatch(_Base):
    batch_id: str = _ID
    app_id: str = Field(min_length=1, max_length=200)
    sdk_version: str | None = Field(default=None, max_length=32)
    sent_at: datetime | None = None
    events: list[EventIn] = Field(min_length=1)


# ------------------------------------------------------------------ logs
class LogIn(_Base):
    ts: datetime
    device_id: str = _ID
    session_id: str | None = Field(default=None, max_length=128)
    level: LogLevel
    tag: str | None = Field(default=None, max_length=128)
    message: str = Field(min_length=1, max_length=8000)
    stacktrace: str | None = Field(default=None, max_length=40000)
    context: dict[str, Any] = Field(default_factory=dict)
    sdk_version: str | None = Field(default=None, max_length=32)


class LogsBatch(_Base):
    batch_id: str = _ID
    app_id: str = Field(min_length=1, max_length=200)
    sdk_version: str | None = Field(default=None, max_length=32)
    sent_at: datetime | None = None
    logs: list[LogIn] = Field(min_length=1)


# --------------------------------------------------------------- crashes
class CrashIn(_Base):
    ts: datetime
    device_id: str = _ID
    session_id: str | None = Field(default=None, max_length=128)
    kind: CrashKind = "java"
    message: str | None = Field(default=None, max_length=8000)
    stacktrace: str | None = Field(default=None, max_length=80000)
    handled: bool = False
    app_version: str | None = Field(default=None, max_length=64)
    os_version: str | None = Field(default=None, max_length=64)


class CrashesBatch(_Base):
    batch_id: str = _ID
    app_id: str = Field(min_length=1, max_length=200)
    sdk_version: str | None = Field(default=None, max_length=32)
    sent_at: datetime | None = None
    crashes: list[CrashIn] = Field(min_length=1)


# -------------------------------------------------------------- sessions
class SessionStartIn(_Base):
    session_id: str = _ID
    device_id: str = _ID
    started_at: datetime
    user_ref: str | None = Field(default=None, max_length=128)
    app_version: str | None = Field(default=None, max_length=64)
    os_version: str | None = Field(default=None, max_length=64)
    country: str | None = Field(default=None, max_length=8)
    device: DeviceInfo | None = None


class SessionEndIn(_Base):
    session_id: str = _ID
    ended_at: datetime
    duration_ms: int = Field(ge=0)
    end_reason: EndReason


# ------------------------------------------------------- observability events
# A different model from EventIn above, on purpose. `events` records what a user
# did; `log_events` records what the software did, which is what a website or a
# backend service has to report.
SourceKind = Literal["browser_sdk", "server_sdk", "android_sdk", "otel", "system"]
HttpMethod = Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE"]


#: What kind of thing produced the event. Platform-shaped, and distinct from
#: `source`, which names the SDK. The explorer filters on this one.
SourceType = Literal["web", "android", "backend"]


class LogEventIn(_Base):
    event_id: str = _ID
    ts: datetime
    source: SourceKind = "browser_sdk"
    severity: LogEventLevel = "INFO"
    event_type: str = Field(min_length=1, max_length=128)

    service_name: str | None = Field(default=None, max_length=128)
    environment: str | None = Field(default=None, max_length=64)
    app_version: str | None = Field(default=None, max_length=64)
    release_id: str | None = Field(default=None, max_length=128)

    request_id: str | None = Field(default=None, max_length=128)
    trace_id: str | None = Field(default=None, max_length=64)
    span_id: str | None = Field(default=None, max_length=32)

    route_template: str | None = Field(default=None, max_length=256)
    http_method: HttpMethod | None = None
    status_code: int | None = Field(default=None, ge=100, le=599)
    duration_ms: int | None = Field(default=None, ge=0)

    error_code: str | None = Field(default=None, max_length=128)
    error_fingerprint: str | None = Field(default=None, max_length=128)

    message: str | None = Field(default=None, max_length=8000)
    attributes: dict[str, Any] = Field(default_factory=dict)
    sdk_version: str | None = Field(default=None, max_length=32)
    # Added in migration 0009, at the Logs Explorer specification's request.
    category: str | None = Field(default=None, max_length=64)
    source_type: SourceType | None = None
    source_id: str | None = Field(default=None, max_length=128)
    platform_version: str | None = Field(default=None, max_length=64)


class LogEventsBatch(_Base):
    batch_id: str = _ID
    app_id: str = Field(min_length=1, max_length=200)
    sdk_version: str | None = Field(default=None, max_length=32)
    sent_at: datetime | None = None
    events: list[LogEventIn] = Field(min_length=1)


# ------------------------------------------------------------- key admin
class KeyCreateIn(_Base):
    app_id: str = Field(min_length=1, max_length=200)
    scope: Literal["ingest", "read"]
    label: str | None = Field(default=None, max_length=128)
    #: Days until the key stops working. None means it never expires.
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)
