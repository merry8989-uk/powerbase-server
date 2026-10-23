#!/usr/bin/env python3
"""Load test the ingestion path.

Runs a real uvicorn server in-process on a free port, then drives it from
several threads over real HTTP — sockets, auth, the SQLite writer lock and all.
Measuring the ASGI app in-process would skip exactly the parts that break under
concurrency.

    python scripts/loadtest.py                      # 10s, 8 workers
    python scripts/loadtest.py --duration 30 --workers 16 --batch-size 500
    python scripts/loadtest.py --quick              # short run, for CI

Reports throughput, latency percentiles, the status breakdown, and — the number
that actually matters — whether every accepted row is really in the database.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import statistics
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
import uvicorn  # noqa: E402

from powerbase.api import create_app  # noqa: E402
from powerbase.config import Settings  # noqa: E402
from powerbase.db import Database  # noqa: E402
from powerbase.provision import issue_key, provision_app  # noqa: E402

APP_ID = "com.example.shop"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def event_payload(batch_id: str, worker: int, size: int) -> dict:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return {
        "batch_id": batch_id,
        "app_id": APP_ID,
        "sdk_version": "loadtest",
        "events": [
            {
                "ts": now,
                "device_id": f"load-dev-{worker}",
                "session_id": None,
                "name": "screen_view" if i % 2 else "add_to_cart",
                "category": "load",
                "screen": ["HomeActivity", "CheckoutActivity"][i % 2],
                "properties": {"i": i, "worker": worker},
            }
            for i in range(size)
        ],
    }


def log_payload(batch_id: str, worker: int, size: int) -> dict:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return {
        "batch_id": batch_id,
        "app_id": APP_ID,
        "logs": [
            {
                "ts": now,
                "device_id": f"load-dev-{worker}",
                "level": "ERROR" if i % 5 == 0 else "INFO",
                "tag": "LoadTest",
                "message": f"load line {i} for user{worker}@example.com",
                "context": {"i": i},
            }
            for i in range(size)
        ],
    }


def crash_payload(batch_id: str, worker: int) -> dict:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return {
        "batch_id": batch_id,
        "app_id": APP_ID,
        "crashes": [
            {
                "ts": now,
                "device_id": f"load-dev-{worker}",
                "kind": "java",
                "message": "java.lang.NullPointerException",
                "stacktrace": "java.lang.NullPointerException\n\tat com.example.Foo.bar(Foo.java:42)",
            }
        ],
    }


def worker_loop(
    url: str,
    key: str,
    worker: int,
    deadline: float,
    batch_size: int,
    results: dict,
) -> None:
    latencies: list[float] = []
    server_times: list[float] = []
    statuses: dict[str, int] = {}
    accepted = 0
    sent = 0
    headers = {"Authorization": f"Bearer {key}"}

    with httpx.Client(base_url=url, timeout=30.0, headers=headers) as client:
        n = 0
        while time.monotonic() < deadline:
            n += 1
            batch_id = f"{worker}-{n}-{uuid.uuid4().hex[:8]}"
            roll = n % 20
            if roll == 0:
                path, payload = "/v1/ingest/crashes", crash_payload(batch_id, worker)
            elif roll < 4:
                path, payload = "/v1/ingest/logs", log_payload(batch_id, worker, batch_size)
            else:
                path, payload = "/v1/ingest/events", event_payload(batch_id, worker, batch_size)

            started = time.perf_counter()
            try:
                response = client.post(path, json=payload)
                elapsed = (time.perf_counter() - started) * 1000
                # The server reports its own handling time, which separates
                # "the server is slow" from "the client or the queue is slow".
                server_ms = response.headers.get("x-response-time-ms")
                if server_ms is not None:
                    server_times.append(float(server_ms))
                statuses[str(response.status_code)] = statuses.get(str(response.status_code), 0) + 1
                if response.status_code in (200, 202):
                    accepted += response.json().get("accepted", 0)
            except Exception as exc:  # noqa: BLE001
                elapsed = (time.perf_counter() - started) * 1000
                key_name = f"error:{type(exc).__name__}"
                statuses[key_name] = statuses.get(key_name, 0) + 1
            latencies.append(elapsed)
            sent += 1

    results[worker] = {
        "latencies": latencies,
        "server_times": server_times,
        "statuses": statuses,
        "accepted": accepted,
        "requests": sent,
    }


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round((pct / 100) * (len(ordered) - 1))))
    return ordered[index]


def main() -> int:
    parser = argparse.ArgumentParser(description="Powerbase ingestion load test")
    parser.add_argument("--duration", type=float, default=10.0, help="seconds to run")
    parser.add_argument("--workers", type=int, default=8, help="concurrent client threads")
    parser.add_argument("--batch-size", type=int, default=200, help="records per batch")
    parser.add_argument("--quick", action="store_true", help="short run for CI")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args()

    if args.quick:
        args.duration, args.workers, args.batch_size = 3.0, 4, 100

    workdir = tempfile.mkdtemp(prefix="powerbase-loadtest-")
    db_path = os.path.join(workdir, "loadtest.db")
    port = free_port()

    settings = Settings(
        db_path=db_path,
        host="127.0.0.1",
        port=port,
        # The production default is a deliberate throttle; a load test has to
        # raise it or it measures the rate limiter instead of the writer.
        rate_burst=10_000_000,
        rate_per_second=10_000_000.0,
        max_batch_size=1000,
    )
    db = Database(db_path)
    app = create_app(settings, db)
    provision_app(db, APP_ID, "Example Shop")
    key = issue_key(db, APP_ID, "ingest")

    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", access_log=False)
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    url = f"http://127.0.0.1:{port}"
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    if not server.started:
        print("server failed to start", file=sys.stderr)
        return 1

    if not args.json:
        print(f"load test: {args.workers} workers, batch={args.batch_size}, "
              f"{args.duration:g}s against {url}")

    results: dict = {}
    deadline = time.monotonic() + args.duration
    threads = [
        threading.Thread(
            target=worker_loop,
            args=(url, key, index, deadline, args.batch_size, results),
            daemon=True,
        )
        for index in range(args.workers)
    ]

    started = time.monotonic()
    for worker in threads:
        worker.start()
    for worker in threads:
        worker.join()
    wall = time.monotonic() - started

    server.should_exit = True
    thread.join(timeout=10)

    latencies: list[float] = []
    server_times: list[float] = []
    statuses: dict[str, int] = {}
    accepted = 0
    requests = 0
    for result in results.values():
        latencies.extend(result["latencies"])
        server_times.extend(result["server_times"])
        accepted += result["accepted"]
        requests += result["requests"]
        for code, count in result["statuses"].items():
            statuses[code] = statuses.get(code, 0) + count

    stored = {
        table: db.scalar(f"SELECT COUNT(*) FROM {table}")
        for table in ("events", "logs", "crashes", "devices")
    }
    rows_written = stored["events"] + stored["logs"] + stored["crashes"]

    summary = {
        "workers": args.workers,
        "batch_size": args.batch_size,
        "wall_seconds": round(wall, 2),
        "requests": requests,
        "accepted_records": accepted,
        "records_per_second": round(accepted / wall, 1) if wall else 0,
        "requests_per_second": round(requests / wall, 1) if wall else 0,
        "latency_ms": {
            "p50": round(percentile(latencies, 50), 2),
            "p95": round(percentile(latencies, 95), 2),
            "p99": round(percentile(latencies, 99), 2),
            "max": round(max(latencies), 2) if latencies else 0,
            "mean": round(statistics.fmean(latencies), 2) if latencies else 0,
        },
        "server_ms": {
            "p50": round(percentile(server_times, 50), 2),
            "p95": round(percentile(server_times, 95), 2),
            "p99": round(percentile(server_times, 99), 2),
        },
        "statuses": dict(sorted(statuses.items())),
        "stored_rows": stored,
        "rows_written": rows_written,
        "all_rows_accounted_for": rows_written == accepted,
    }

    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print()
        print(f"  wall time          {summary['wall_seconds']}s")
        print(f"  requests           {summary['requests']:,} ({summary['requests_per_second']:,}/s)")
        print(f"  records accepted   {summary['accepted_records']:,} ({summary['records_per_second']:,}/s)")
        print(f"  client p50/p95/p99   {summary['latency_ms']['p50']} / "
              f"{summary['latency_ms']['p95']} / {summary['latency_ms']['p99']} ms")
        print(f"  server p50/p95/p99   {summary['server_ms']['p50']} / "
              f"{summary['server_ms']['p95']} / {summary['server_ms']['p99']} ms")
        print(f"  statuses           {summary['statuses']}")
        print(f"  rows in database   {rows_written:,} (accepted {accepted:,})")
        print(f"  every row stored   {summary['all_rows_accounted_for']}")

    return 0 if summary["all_rows_accounted_for"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
