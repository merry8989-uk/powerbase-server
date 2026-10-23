#!/usr/bin/env python3
"""A complete Powerbase API walk-through, against a server that is already up.

    PB=http://localhost:8080 ADMIN=your-admin-password \
      python examples/api_client.py com.example.shop

It creates its own keys, so nothing has to be set up by hand first. It only
needs httpx, which is already a dependency.
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timezone

import httpx

BASE = os.environ.get("PB", "http://localhost:8080").rstrip("/")
ADMIN = os.environ.get("ADMIN", "")
APP_ID = sys.argv[1] if len(sys.argv) > 1 else "com.example.shop"

if not ADMIN:
    sys.exit("set ADMIN to your admin password, e.g. ADMIN=... python examples/api_client.py")


def show(label: str, value) -> None:
    print(f"  {label:<34} {value}")


def main() -> int:
    with httpx.Client(base_url=BASE, timeout=20.0) as c:
        health = c.get("/v1/health").json()
        print(f"\nPowerbase {health['version']} at {BASE}\n")

        # ------------------------------------------------------------ keys
        print("keys (admin password)")
        admin = {"X-Admin-Password": ADMIN}
        ingest = c.post(
            "/v1/keys",
            headers=admin,
            json={"app_id": APP_ID, "scope": "ingest", "label": "example", "expires_in_days": 30},
        )
        if ingest.status_code == 404:
            return print(f"  project '{APP_ID}' does not exist. Create it:\n"
                         f"    python scripts/bootstrap.py --app-id {APP_ID} --name 'Example'") or 1
        ingest.raise_for_status()
        ingest_key = ingest.json()["token"]
        read_key = c.post(
            "/v1/keys", headers=admin, json={"app_id": APP_ID, "scope": "read", "label": "example"}
        ).json()["token"]
        show("ingest key", ingest_key[:14] + "…")
        show("read key", read_key[:14] + "…")

        read = {"Authorization": f"Bearer {read_key}"}
        write = {"Authorization": f"Bearer {ingest_key}"}

        # ----------------------------------------------------------- ingest
        print("\ningest")
        now = datetime.now(timezone.utc)
        events = [
            {
                "event_id": str(uuid.uuid4()),
                "ts": now.isoformat().replace("+00:00", "Z"),
                "source": "server_sdk",
                "severity": ["INFO", "WARN", "ERROR"][i % 3],
                "event_type": "http.request",
                "service_name": ["checkout-api", "auth-api"][i % 2],
                "environment": "production",
                "route_template": "/orders/{id}",
                "http_method": "POST",
                "status_code": 500 if i % 3 == 2 else 200,
                "duration_ms": 100 + i * 60,
                "error_fingerprint": "fp-checkout-1" if i % 3 == 2 else None,
                "trace_id": "trace-demo-1" if i < 2 else None,
                "message": f"demo event {i} for amit@example.com",
                "attributes": {"region": "ap-south-1", "retry": i, "password": "never-stored"},
            }
            for i in range(6)
        ]
        accepted = c.post(
            "/v1/ingest/log-events",
            headers=write,
            json={"batch_id": str(uuid.uuid4()), "app_id": APP_ID, "events": events},
        )
        accepted.raise_for_status()
        body = accepted.json()
        show("accepted", body["accepted"])
        show("credential keys dropped", body["dropped_attributes"])

        # ------------------------------------------------------------ query
        print("\nquery")
        page = c.get("/v1/query/log-events", headers=read, params={"limit": 3}).json()
        show("first page", [i["event_id"][:8] for i in page["items"]])
        show("has_more", page["has_more"])

        if page["next_cursor"]:
            page2 = c.get(
                "/v1/query/log-events",
                headers=read,
                params={"limit": 3, "cursor": page["next_cursor"]},
            ).json()
            show("second page", [i["event_id"][:8] for i in page2["items"]])

        errors = c.get("/v1/query/log-events", headers=read, params={"severity": "ERROR"}).json()
        show("severity=ERROR", errors["count"])

        typed = c.post(
            "/v1/query/log-events",
            headers=read,
            json={
                "filters": [{"field": "duration_ms", "operator": "gte", "value": 200}],
                "sort": {"field": "duration_ms", "direction": "desc"},
            },
        ).json()
        show("duration>=200 (JSON form)", [(i["event_id"][:8], i["duration_ms"]) for i in typed["items"]])

        refused = c.post(
            "/v1/query/log-events",
            headers=read,
            json={"filters": [{"field": "severity; DROP TABLE log_events", "operator": "eq", "value": "x"}]},
        )
        show("injection attempt", f"{refused.status_code} {refused.json().get('detail', '')[:40]}")

        stats = c.get("/v1/query/log-events/stats", headers=read).json()
        show("by severity", stats["by_severity"])

        # ----------------------------------------------------------- detail
        print("\ndetail")
        first = page["items"][0]["event_id"]
        detail = c.get(f"/v1/query/log-events/{first}", headers=read).json()
        show("message", (detail["event"]["message"] or "")[:44])
        show("attributes kept", detail["event"]["attributes"])
        show("related events", len(detail.get("related", [])))

        # ---------------------------------------------------------- searches
        print("\nsaved searches")
        c.post(
            "/v1/saved-queries",
            headers=admin,
            json={
                "app_id": APP_ID,
                "name": "slow requests",
                "definition": {"filters": [{"field": "duration_ms", "operator": "gte", "value": 200}]},
            },
        )
        saved = c.get("/v1/saved-queries", headers=admin, params={"app_id": APP_ID}).json()
        show("saved", [item["name"] for item in saved["items"]])

        print("\ndone. Console: " + BASE + "/\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
