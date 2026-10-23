# Powerbase 1.5.0

The Logs Explorer specification's request and response envelope, and four bugs
fixed — two of them in the release tooling itself.

## What is in this release

**The specification's request body now works in full.**

```bash
curl -sS -X POST $PB/v1/query/log-events \
  -H "Authorization: Bearer $READ_KEY" --data-urlencode "app_id=com.acme.shop" \
  -H 'Content-Type: application/json' -d '{
    "filters": {
      "platform": "all", "severity": "all", "category": "all",
      "environment": "all", "search": null, "time_range": "7d"
    },
    "sort": {"field": "timestamp", "direction": "desc"},
    "pagination": {"limit": 50, "cursor": null}
  }'
```

```json
{
  "items": [ … ],
  "count": 2, "limit": 50, "next_cursor": null, "has_more": false,
  "sort": {"field": "event_time", "direction": "desc"},
  "meta": {"query_id": "52ae69f64acc4ef0", "duration_ms": 1,
           "sort": {"field": "event_time", "direction": "desc"}},
  "pagination": {"next_cursor": null, "has_more": false, "limit": 50}
}
```

- **`query_id`** is new. Queries slower than a second are logged with that id, so
  a slow query can be named rather than shrugged at.
- **`time_range`** accepts a duration — `"30m"`, `"24h"`, `"7d"`, `"2w"` —
  resolved against the **server** clock, so a client with a wrong clock cannot
  ask for a window in the future. Each unit has a cap.
- **Sort** accepts the specification's `timestamp` (and `time`) as names for
  `event_time`, and `id` as the stable tie-break.
- The **flat keys stay**, and the general filter list the console sends still
  works. `items` is the specification's `data`; it is not duplicated under a
  second key, because that would double the payload of every request.

**Four fields the specification asked for** are now on log events, filterable
and included in exports: `category`, `source_type`, `source_id`,
`platform_version` — plus a `TRACE` severity level throughout.

## Bugs fixed

1. **A release build carried the wrong version.** `pyproject.toml` hardcoded
   `0.5.0` while the code was at `1.5.0`. The version is now read from
   `powerbase/version.py` — the same value `/healthz` and the dashboard report —
   so the metadata and the running server cannot disagree. Verified in the built
   artifacts.
2. **`?app_id=` was ignored on `POST /v1/query/log-events`.** The POST verb read
   `app_id` from the body while the GET verb read it from the query string, so a
   request that appeared to ask for one project answered with another. It never
   leaked across tenants — a key is bound to one project — but a request that
   looks like it asks for one thing and quietly answers with another is not
   something to leave in place. It now refuses with `403`.
3. **Two `500`s on the refusal path.** An unknown filter name crashed the error
   message itself (`dict | set`), and a non-object `time_range` was iterated
   (`set(7)`). Both are reached by making a typo, and both now answer `400` with
   a useful message.
4. **Retention did not cover `spans` or `metric_points`.** They were added in
   1.3.0 but left out of the rollup and the prune, so they grew without a bound.
   Both are now folded into the same nightly job.

Also: the OTLP severity band for numbers 1–4 was reported as `DEBUG`; per the
specification it is `TRACE`.

## Verifying this release

```bash
sha256sum -c SHA256SUMS
```

Then, from the extracted tree:

```bash
pip install fastapi "pydantic>=2" pytest argon2-cffi httpx jinja2 \
            python-multipart "uvicorn[standard]"
python3 -m pytest tests          # 668 tests
```

The suite takes a few minutes; it is normally run in chunks.

## Installing

```bash
# bare metal, with a systemd unit and Caddy
sudo ./scripts/install.sh --domain logs.example.com --email you@example.com

# or with Docker
docker compose up -d
```

Bootstrap the first account, or nobody can sign in:

```bash
powerbase owner --email you@example.com
```

## Not in this release

- **OTLP protobuf.** JSON only, refused with a clear message.
- **An S3-compatible archive and SSO.** Both deferred by the roadmap.
- **A formal security review.** The isolation tests and the load test are not a
  substitute for one.
- **Realtime database subscriptions.** Out of scope by decision: Powerbase is an
  observability platform. Neither kind of subscription exists — not database
  subscriptions, and not paid ones.

## Free forever

No billing, no paid tier, no usage paywall, no subscription checkout — and a
test that fails the build if any of that changes. See `docs/free-forever.md`.
