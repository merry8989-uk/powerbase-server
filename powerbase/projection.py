"""Project analytics events and crash reports into the diagnostics log.

Powerbase grew two data models that do not know about each other:

* the **analytics** path — `events`, `logs`, `crashes` — built first, for the
  mobile SDK, with its own dashboard pages;
* the **diagnostics** path — `log_events` — built for the Logs Explorer.

The consequence was that a crash, or a product analytics event, **did not appear
in the Logs Explorer at all**. Answering "what happened in this session" meant
looking in two places, and the explorer — the screen built for exactly that
question — could not answer it.

A crash *is* a diagnostic event. So is an analytics event, at the level of
detail anyone actually needs during an incident. This module turns one into the
other.

## It is a projection, not a move

The source table stays the source of truth for its own dashboard page, and no
client changes. A `log_events` row is written **in the same transaction** as the
source row, so the two cannot diverge through a crash or a rollback — either
both land or neither does.

The cost is honest and worth stating: **each crash and analytics event is now
stored twice.** That is the price of the explorer being complete without
breaking every installed client. Consolidating the two models is a larger change
that should be made deliberately, not as a side effect of this one.

## Why not a view

A `UNION ALL` view over the three tables would avoid the duplication, and it
breaks the cursor. `log_events` paginates on `(event_time, id)` and relies on
`id` being unique; ids collide across tables, so a union would need a synthetic
key and the keyset cursor would stop being exact. Duplication is the cheaper
mistake.
"""

from __future__ import annotations

from typing import Any

#: Which SDK the analytics endpoints serve. They are the mobile SDK's contract —
#: `sdk_version` on those payloads is the Android SDK's — so a projected row is
#: tagged accordingly. A web client posting to the same endpoints would be
#: mislabelled, which is a known and documented limitation.
ANALYTICS_SOURCE = "android_sdk"

#: Crash severities. An unhandled crash is what the user saw; a handled one was
#: caught and the app carried on. They are not the same thing and should not
#: share a colour in the explorer.
CRASH_SEVERITY = {True: "ERROR", False: "FATAL"}


def event_id_for(table: str, app_id: str, row_id: int) -> str:
    """A deterministic id, so a replayed batch cannot double-project.

    The batch ledger already refuses a replayed batch, so this is defence in
    depth rather than the only guard — but a derived id means the second line of
    defence actually holds.
    """
    return f"{table}:{app_id}:{row_id}"


def from_crash(
    crash,
    *,
    row_id: int,
    app_id: str,
    ts: str,
    message: str | None,
    stacktrace: str | None,
    fingerprint: str,
) -> dict[str, Any]:
    """A crash, as a diagnostics log event.

    Takes the already-redacted `message` and `stacktrace`, and the already
    computed `fingerprint`, because the writer computed them. Redacting
    again here would be harmless and pointless; using the raw values would
    put in the explorer exactly what the crash page hides.
    """
    handled = bool(crash.handled)
    stack = stacktrace or ""
    # The first *frame*, not the first line. A Java trace opens with the
    # exception ("java.lang.RuntimeException: boom"); the line a person reads is
    # the `at ...` under it, and that is the one worth carrying.
    head = None
    for line in stack.splitlines():
        stripped = line.strip()
        if stripped.startswith("at ") or stripped.startswith("\tat "):
            head = stripped
            break
    if head is None and stack.strip():
        head = stack.strip().splitlines()[0].strip()

    attributes: dict[str, Any] = {"crash_kind": crash.kind, "handled": handled}
    if crash.device_id:
        attributes["device_id"] = crash.device_id
    if crash.session_id:
        attributes["session_id"] = crash.session_id
    if head:
        # The first frame is what a person reads; the whole trace is on the
        # crash's own page, which this projection does not replace.
        attributes["stack_head"] = head[:500]

    return {
        "event_id": event_id_for("crash", app_id, row_id),
        "event_time": ts,
        "source": ANALYTICS_SOURCE,
        "severity": CRASH_SEVERITY[handled],
        "event_type": "crash",
        "service_name": None,
        "environment": None,
        "app_version": crash.app_version,
        "platform_version": crash.os_version,
        "message": message,
        "error_code": crash.kind,
        "error_fingerprint": fingerprint,
        "attributes": attributes,
        "sdk_version": None,
        "category": "crash",
        "source_type": "android",
    }


def from_event(
    event, *, row_id: int, app_id: str, ts: str, properties: dict | None
) -> dict[str, Any]:
    """An analytics event, as a diagnostics log event."""
    attributes: dict[str, Any] = dict(properties or {})
    if event.device_id:
        attributes["device_id"] = event.device_id
    if event.session_id:
        attributes["session_id"] = event.session_id

    return {
        "event_id": event_id_for("event", app_id, row_id),
        "event_time": ts,
        "source": ANALYTICS_SOURCE,
        "severity": "INFO",
        # Named so the explorer can tell a product event from an HTTP request
        # without a filter on `source`.
        "event_type": "analytics.event",
        "service_name": None,
        "environment": None,
        "app_version": None,
        "platform_version": None,
        "message": event.name,
        "error_code": None,
        "error_fingerprint": None,
        "attributes": attributes,
        "sdk_version": event.sdk_version,
        "category": event.category,
        "source_type": "android",
        # The screen an event happened on is the nearest thing an analytics
        # event has to a route template, and it is what people filter on.
        "route_template": event.screen,
    }


def from_log(
    log, *, row_id: int, app_id: str, ts: str, message: str, context: dict | None
) -> dict[str, Any]:
    """An analytics log line, as a diagnostics log event."""
    attributes: dict[str, Any] = dict(context or {})
    if log.device_id:
        attributes["device_id"] = log.device_id
    if log.session_id:
        attributes["session_id"] = log.session_id
    if log.tag:
        attributes["tag"] = log.tag

    return {
        "event_id": event_id_for("log", app_id, row_id),
        "event_time": ts,
        "source": ANALYTICS_SOURCE,
        "severity": log.level,
        "event_type": "analytics.log",
        "service_name": None,
        "environment": None,
        "app_version": None,
        "platform_version": None,
        "message": message,
        "error_code": None,
        "error_fingerprint": None,
        "attributes": attributes,
        "sdk_version": None,
        "category": "log",
        "source_type": "android",
    }


#: The columns `repository._insert_projected` writes, in order.
COLUMNS = (
    "event_id", "app_id", "event_time", "source", "severity", "event_type",
    "service_name", "environment", "app_version", "platform_version",
    "request_id", "trace_id", "span_id", "route_template", "http_method",
    "status_code", "duration_ms", "error_code", "error_fingerprint", "message",
    "attributes", "redaction_version", "sdk_version", "category",
    "source_type", "source_id",
)
