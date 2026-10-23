#!/usr/bin/env python3
"""Insert a small fixture dataset so the database is not empty while developing.

    python scripts/seed.py --app-id com.example.shop

It writes through the same repository functions the API uses, so seeded data
has exactly the same shape as real ingested data.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from powerbase import repository as repo  # noqa: E402
from powerbase.config import Settings  # noqa: E402
from powerbase.db import Database  # noqa: E402
from powerbase.migrations import run_migrations  # noqa: E402
from powerbase.models import (  # noqa: E402
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
from powerbase.provision import provision_app  # noqa: E402

SCREENS = ["HomeActivity", "SearchActivity", "ProductActivity", "CheckoutActivity", "ProfileActivity"]
EVENTS = ["screen_view", "button_tap", "add_to_cart", "purchase", "search"]
LEVELS = ["DEBUG", "INFO", "WARN", "ERROR"]
TAGS = ["CheckoutRepo", "NetworkClient", "AuthManager", "ImageLoader"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed a Powerbase instance")
    parser.add_argument("--db", default=None)
    parser.add_argument("--app-id", default="com.example.shop")
    parser.add_argument("--devices", type=int, default=8)
    parser.add_argument("--sessions-per-device", type=int, default=3)
    parser.add_argument("--events-per-session", type=int, default=12)
    args = parser.parse_args()

    settings = Settings.from_env()
    db = Database(args.db or settings.db_path)
    run_migrations(db, settings.migrations_dir)
    provision_app(db, args.app_id, "Example Shop", "android")
    print(f"seeding {args.app_id} into {args.db or settings.db_path}")

    base = _now() - timedelta(days=7)
    total_events = total_logs = total_crashes = 0

    for d in range(args.devices):
        device_id = f"seed-device-{d:03d}"
        for s in range(args.sessions_per_device):
            session_id = f"seed-session-{d:03d}-{s}"
            started = base + timedelta(days=d * 0.5, hours=s, minutes=d * 3)
            duration = 60_000 + (d * 977 + s * 613) % 900_000

            repo.start_session(
                db,
                args.app_id,
                SessionStartIn(
                    session_id=session_id,
                    device_id=device_id,
                    started_at=started,
                    user_ref=f"user_{d % 3}",
                    app_version="3.4.1",
                    os_version="14",
                    country="IN",
                    device=DeviceInfo(
                        model=["Pixel 8", "Redmi Note 12", "Galaxy S23"][d % 3],
                        manufacturer=["Google", "Xiaomi", "Samsung"][d % 3],
                        os_version="14",
                        app_version="3.4.1",
                        sdk_version="0.1.0",
                        locale="en-IN",
                    ),
                ),
            )

            events = [
                EventIn(
                    ts=started + timedelta(seconds=i * 7),
                    device_id=device_id,
                    session_id=session_id,
                    name=EVENTS[(d + i) % len(EVENTS)],
                    category="engagement",
                    screen=SCREENS[(d + i) % len(SCREENS)],
                    properties={"index": i, "variant": "A" if d % 2 else "B"},
                    sdk_version="0.1.0",
                )
                for i in range(args.events_per_session)
            ]
            repo.write_events(
                db,
                args.app_id,
                EventsBatch(batch_id=f"seed-ev-{d}-{s}", app_id=args.app_id, events=events),
                redact=True,
            )
            total_events += len(events)

            logs = [
                LogIn(
                    ts=started + timedelta(seconds=i * 11),
                    device_id=device_id,
                    session_id=session_id,
                    level=LEVELS[(d + i) % len(LEVELS)],
                    tag=TAGS[(d + i) % len(TAGS)],
                    message=f"step {i}: operation completed for user{(d % 3)}@example.com",
                    context={"screen": SCREENS[(d + i) % len(SCREENS)]},
                    sdk_version="0.1.0",
                )
                for i in range(4)
            ]
            repo.write_logs(
                db,
                args.app_id,
                LogsBatch(batch_id=f"seed-log-{d}-{s}", app_id=args.app_id, logs=logs),
                redact=True,
            )
            total_logs += len(logs)

            if d % 4 == 0:
                repo.write_crashes(
                    db,
                    args.app_id,
                    CrashesBatch(
                        batch_id=f"seed-crash-{d}-{s}",
                        app_id=args.app_id,
                        crashes=[
                            CrashIn(
                                ts=started + timedelta(seconds=duration / 1000),
                                device_id=device_id,
                                session_id=session_id,
                                kind="java",
                                message="java.lang.NullPointerException",
                                stacktrace=(
                                    "java.lang.NullPointerException: attempt to invoke virtual method\n"
                                    "\tat com.example.shop.checkout.PaymentViewModel.apply(PaymentViewModel.kt:88)"
                                ),
                                app_version="3.4.1",
                                os_version="14",
                            )
                        ],
                    ),
                    redact=True,
                )
                total_crashes += 1

            repo.end_session(
                db,
                args.app_id,
                SessionEndIn(
                    session_id=session_id,
                    ended_at=started + timedelta(milliseconds=duration),
                    duration_ms=duration,
                    end_reason="crash" if d % 4 == 0 else "background",
                ),
            )

    print(
        f"done: {args.devices} devices, "
        f"{args.devices * args.sessions_per_device} sessions, "
        f"{total_events} events, {total_logs} logs, {total_crashes} crashes"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
