"""Shared constants, payload builders and a deterministic dataset for the tests."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from powerbase import repository as repo
from powerbase.models import (
    CrashesBatch,
    CrashIn,
    DeviceInfo,
    EventsBatch,
    EventIn,
    LogIn,
    LogsBatch,
    SessionEndIn,
    SessionStartIn,
)

APP_ID = "com.example.shop"
OTHER_APP_ID = "com.other.app"
ADMIN_PASSWORD = "test-admin-password"

TS = "2026-10-22T09:00:00Z"


def events_payload(batch_id="batch-ev-1", app_id=APP_ID, n=1, device_id="dev-1", session_id="sess-1"):
    return {
        "batch_id": batch_id,
        "app_id": app_id,
        "sdk_version": "0.1.0",
        "sent_at": TS,
        "events": [
            {
                "ts": TS,
                "device_id": device_id,
                "session_id": session_id,
                "name": "screen_view",
                "category": "engagement",
                "screen": "HomeActivity",
                "properties": {"index": i},
            }
            for i in range(n)
        ],
    }


def logs_payload(batch_id="batch-log-1", app_id=APP_ID, device_id="dev-1", session_id="sess-1"):
    return {
        "batch_id": batch_id,
        "app_id": app_id,
        "sdk_version": "0.1.0",
        "logs": [
            {
                "ts": TS,
                "device_id": device_id,
                "session_id": session_id,
                "level": "ERROR",
                "tag": "CheckoutRepo",
                "message": "Payment intent failed for amit@example.com",
                "stacktrace": "java.io.IOException: connection reset",
                "context": {"screen": "CheckoutActivity"},
            }
        ],
    }


def crashes_payload(batch_id="batch-crash-1", app_id=APP_ID, stacktrace=None, device_id="dev-1"):
    return {
        "batch_id": batch_id,
        "app_id": app_id,
        "crashes": [
            {
                "ts": TS,
                "device_id": device_id,
                "session_id": "sess-1",
                "kind": "java",
                "message": "java.lang.NullPointerException",
                "stacktrace": stacktrace
                or "java.lang.NullPointerException\n\tat com.example.Foo.bar(Foo.java:42)",
                "handled": False,
                "app_version": "3.4.1",
                "os_version": "14",
            }
        ],
    }


def session_start_payload(session_id="sess-1", device_id="dev-1", app_id=APP_ID):
    return {
        "session_id": session_id,
        "device_id": device_id,
        "started_at": TS,
        "user_ref": "user_hash_1",
        "app_version": "3.4.1",
        "os_version": "14",
        "country": "IN",
        "device": {
            "model": "Pixel 8",
            "manufacturer": "Google",
            "os_version": "14",
            "app_version": "3.4.1",
            "sdk_version": "0.1.0",
            "locale": "en-IN",
        },
    }


def session_end_payload(session_id="sess-1"):
    return {
        "session_id": session_id,
        "ended_at": "2026-10-22T09:01:01Z",
        "duration_ms": 61000,
        "end_reason": "background",
    }


# --------------------------------------------------------------------------- #
# Deterministic dataset
# --------------------------------------------------------------------------- #

# Written through the same repository functions the API uses, so the fixture
# has exactly the shape of real ingested data. Timestamps are relative to now
# and always in the past, so "last N days" windows behave the same whenever the
# suite runs.
EXPECTED = {
    "devices": 4,
    "sessions": 4,
    "events": 20,
    "logs": 4,
    "error_logs": 2,
    "crashes": 2,
    "crash_free_rate": 0.5,
    "screens": 2,
    "event_names": 2,
    # 5 events per session with name/screen alternating on `i % 2` gives an
    # uneven split: index 0, 2, 4 versus 1, 3.
    "screen_view_events": 12,
    "add_to_cart_events": 8,
    "home_screen_events": 12,
    "checkout_screen_events": 8,
}


def seed_dataset(db, app_id=APP_ID) -> dict:
    base = datetime.now(timezone.utc) - timedelta(days=5)

    for d in range(4):
        device_id = f"dev-{d}"
        session_id = f"sess-{d}"
        started = base + timedelta(days=d, hours=d)

        repo.start_session(
            db,
            app_id,
            SessionStartIn(
                session_id=session_id,
                device_id=device_id,
                started_at=started,
                user_ref=f"user_{d}",
                app_version="3.4.1",
                os_version="14",
                country="IN",
                device=DeviceInfo(
                    model=["Pixel 8", "Redmi Note 12"][d % 2],
                    manufacturer=["Google", "Xiaomi"][d % 2],
                    os_version="14",
                    app_version="3.4.1",
                    sdk_version="0.1.0",
                    locale="en-IN",
                ),
            ),
        )

        repo.write_events(
            db,
            app_id,
            EventsBatch(
                batch_id=f"seed-events-{d}",
                app_id=app_id,
                events=[
                    EventIn(
                        ts=started + timedelta(seconds=i * 5),
                        device_id=device_id,
                        session_id=session_id,
                        name=["screen_view", "add_to_cart"][i % 2],
                        category="engagement",
                        screen=["HomeActivity", "CheckoutActivity"][i % 2],
                        properties={"index": i},
                        sdk_version="0.1.0",
                    )
                    for i in range(5)
                ],
            ),
            redact=True,
        )

        repo.write_logs(
            db,
            app_id,
            LogsBatch(
                batch_id=f"seed-logs-{d}",
                app_id=app_id,
                logs=[
                    LogIn(
                        ts=started + timedelta(seconds=3),
                        device_id=device_id,
                        session_id=session_id,
                        level="ERROR" if d % 2 else "INFO",
                        tag="CheckoutRepo" if d % 2 else "AuthManager",
                        message=f"payment failed for user{d}@example.com",
                        context={"screen": "CheckoutActivity"},
                        sdk_version="0.1.0",
                    )
                ],
            ),
            redact=True,
        )

        if d % 2 == 0:
            repo.write_crashes(
                db,
                app_id,
                CrashesBatch(
                    batch_id=f"seed-crashes-{d}",
                    app_id=app_id,
                    crashes=[
                        CrashIn(
                            ts=started + timedelta(seconds=9),
                            device_id=device_id,
                            session_id=session_id,
                            kind="java",
                            message="java.lang.NullPointerException",
                            stacktrace=(
                                "java.lang.NullPointerException\n"
                                "\tat com.example.Foo.bar(Foo.java:42)"
                            ),
                            app_version="3.4.1",
                            os_version="14",
                        )
                    ],
                ),
                redact=True,
            )

        repo.end_session(
            db,
            app_id,
            SessionEndIn(
                session_id=session_id,
                ended_at=started + timedelta(minutes=2),
                duration_ms=120_000,
                end_reason="crash" if d % 2 == 0 else "background",
            ),
        )

    return dict(EXPECTED)


# --------------------------------------------------------------------------- #
# A fixed-date dataset for rollup and retention tests
# --------------------------------------------------------------------------- #

# Fixed absolute dates (not "now"-relative) so day boundaries are predictable.
DATED_START = date(2026, 1, 4)
DATED_DAYS = 7
DATED_PER_DAY = 3


def dated_day(offset: int) -> date:
    return DATED_START + timedelta(days=offset)


def dated_day_str(offset: int) -> str:
    return dated_day(offset).isoformat()


def dated_range() -> list[str]:
    return [dated_day_str(i) for i in range(DATED_DAYS)]


def seed_dated(db, app_id=APP_ID, days=DATED_DAYS, per_day=DATED_PER_DAY) -> dict:
    """Events at 06:00, 12:00 and 18:00 UTC on each of `days` consecutive days.

    Two devices, alternating per day, so device pruning has something to chew on.
    """
    for offset in range(days):
        day = dated_day(offset)
        events = [
            EventIn(
                ts=datetime(day.year, day.month, day.day, 6 + i * 6, tzinfo=timezone.utc),
                device_id=f"dated-dev-{offset % 2}",
                name="screen_view",
                screen="HomeActivity",
                properties={"index": i},
                sdk_version="0.1.0",
            )
            for i in range(per_day)
        ]
        repo.write_events(
            db,
            app_id,
            EventsBatch(batch_id=f"dated-{offset}", app_id=app_id, events=events),
            redact=True,
        )
    return {"days": days, "per_day": per_day, "total": days * per_day}


def set_retention(db, retention_days: int, app_id=APP_ID) -> None:
    with db.write() as conn:
        conn.execute("UPDATE apps SET retention_days = ? WHERE id = ?", (retention_days, app_id))


# ------------------------------------------------- observability log events
def log_event(**overrides) -> dict:
    """One log_events row in the observability shape."""
    event = {
        "event_id": "evt-1",
        "ts": TS,
        "source": "server_sdk",
        "severity": "ERROR",
        "event_type": "http.request",
        "service_name": "checkout-api",
        "environment": "production",
        "app_version": "1.4.0",
        "release_id": "rel-2026-10-22",
        "request_id": "req-1",
        "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
        "span_id": "00f067aa0ba902b7",
        "route_template": "/orders/{id}",
        "http_method": "POST",
        "status_code": 500,
        "duration_ms": 812,
        "error_code": "UPSTREAM_TIMEOUT",
        "error_fingerprint": "fp-checkout-1",
        "message": "upstream timeout for amit@example.com",
        "attributes": {"region": "ap-south-1", "retry": 2},
    }
    event.update(overrides)
    return event


def log_events_payload(batch_id="batch-obs-1", app_id=APP_ID, events=None):
    return {
        "batch_id": batch_id,
        "app_id": app_id,
        "sdk_version": "0.1.0",
        "events": events if events is not None else [log_event()],
    }
