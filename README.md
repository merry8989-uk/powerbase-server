# Powerbase — server

A self-hosted diagnostics and usage-log platform in one Python process over one
SQLite file. This package is the **server**: the schema, the ingestion API, and
the dashboard.

> This archive supersedes the earlier `powerbase-phase0-1.zip`. It is the same
> project with Phase 3 (the dashboard and read API) added, plus a fix to the
> redaction rule ordering described below.

```
powerbase/
├── powerbase/                    # the package
│   ├── api.py                    # FastAPI app; wires ingest + read + dashboard
│   ├── query_api.py              # read-only JSON API (read-scoped keys)
│   ├── queries.py                # every read-side SQL statement
│   ├── rollups.py                # raw tables -> metrics_daily, and reading them back
│   ├── maintenance.py            # retention pruning, vacuum, backup, restore
│   ├── scheduler.py              # in-process nightly job runner
│   ├── alerts.py                 # alert rules + optional webhook
│   ├── logging_setup.py          # structured logging (key=value or JSON)
│   ├── dashboard.py              # server-rendered UI routes
│   ├── dashboard_auth.py         # admin password + signed session cookie
│   ├── repository.py             # transactional batch writers (writes)
│   ├── models.py                 # Pydantic request models
│   ├── redaction.py              # PII scrubbing
│   ├── security.py               # token format + Argon2id hashing
│   ├── ratelimit.py              # token bucket per API key
│   ├── db.py                     # SQLite access: WAL, thread-local conns, write lock
│   ├── migrations.py             # forward-only migration runner
│   ├── provision.py              # create apps + issue keys (CLI/tests only)
│   ├── config.py                 # Settings.from_env()
│   ├── templates/                # Jinja2 pages
│   └── static/                   # app.css, app.js
├── migrations/
│   ├── 0001_init.sql             # schema v1 — nine tables + FTS5
│   └── 0002_job_runs.sql         # maintenance ledger
├── scripts/bootstrap.py          # create db, migrate, register app, print keys
├── scripts/seed.py               # insert a fixture dataset
├── scripts/maintenance.py        # run rollups / prune / backup by hand or from cron
├── scripts/migrate.py            # migration status, verify, rollback drill
├── scripts/loadtest.py           # concurrent load test against a real server
├── .github/workflows/ci.yml
├── tests/                        # 240 tests
├── run.py                        # dev entry point
└── requirements.txt
```

## Quickstart

```bash
pip install -r requirements.txt

# 1. create the database and get your keys (printed once)
python scripts/bootstrap.py --app-id com.example.shop --name "Example Shop"

# 2. optional: load fixture data
python scripts/seed.py --app-id com.example.shop

# 3. set a dashboard password and run
export POWERBASE_ADMIN_PASSWORD='choose-something-long'
python run.py
```

Then open **http://localhost:8080** for the dashboard (`/docs` for the API
reference). Without `POWERBASE_ADMIN_PASSWORD` the dashboard is disabled
entirely and the ingestion API keeps working — that is the default, so a
misconfigured deploy cannot expose your data.

## The dashboard

Server-rendered Jinja2 plus HTMX for the interactive parts and Chart.js for the
graphs. No SPA, no build step, no node_modules — one process serves the API and
the UI.

| Page | What it answers |
|---|---|
| **Overview** | DAU, sessions, events, crash-free rate, error count, average session length — plus a trend chart and top screens/events/models |
| **Log explorer** | Filter by level, tag and time range; full-text search over messages; click any row for a drawer with the stacktrace, context JSON and links to the device and session |
| **Crashes** | Crashes grouped by normalised stack fingerprint, ranked by occurrences, with affected-device counts and a drill-down per group |
| **Charts** | Any metric (events, logs, error logs, crashes, sessions, active devices) at hourly or daily granularity, plus a breakdown by screen, event, tag, level, device, country, end reason, model or version |
| **Devices** | Every install with model, OS, app version, locale and session count; drill into a device for its session history |
| **Sessions** | Session list with duration, end reason and per-session event/log/crash counts; drill into one for a full event timeline |
| **Data browser** | The raw tables: browse, sort, filter, and follow foreign keys to connected rows |

Auth is one admin password (`POWERBASE_ADMIN_PASSWORD`) and an HMAC-signed
`HttpOnly` cookie. The signing key is derived from the password, so rotating it
logs everyone out. Login attempts are throttled in-process (5 per 5 minutes).

## The read API

Separate from the write path, and reached only with a **`read`-scoped key** — a
client-embedded `ingest` key is rejected with 403 by construction.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/v1/query/events` | Filter by session, device, name, screen, time; paginate |
| `GET` | `/v1/query/logs` | Level, tag, session, device, `q` (full-text), time |
| `GET` | `/v1/query/logs/{id}` | One log line |
| `GET` | `/v1/query/crashes` | Filter by fingerprint, kind, device, time |
| `GET` | `/v1/query/crash-groups` | Aggregated by fingerprint |
| `GET` | `/v1/query/sessions` | With per-session event/log/crash counts |
| `GET` | `/v1/query/devices` | With session counts |
| `GET` | `/v1/metrics/summary` | The overview KPIs as JSON |
| `GET` | `/v1/metrics/timeseries` | `metric`, `bucket=hour\|day`, time range |
| `GET` | `/v1/metrics/top` | Top values for a dimension |
| `GET` | `/v1/query/tables` | Table list with row counts (data browser) |
| `GET` | `/v1/query/tables/{table}` | Rows, columns and foreign keys |

```bash
curl -H "Authorization: Bearer pb_<read-key>" \
  "http://localhost:8080/v1/metrics/summary?days=7"
```

## The ingestion API

Unchanged from Phase 1. All write endpoints take a JSON batch and return `202`
with the number of rows stored; re-sending a batch returns `200` with
`"duplicate": true` and writes nothing.

| Method | Path | Auth |
|---|---|---|
| `GET` | `/v1/health` | none |
| `GET` | `/v1/` | none — service banner |
| `POST` | `/v1/ingest/events` | ingest key |
| `POST` | `/v1/ingest/logs` | ingest key |
| `POST` | `/v1/ingest/crashes` | ingest key |
| `POST` | `/v1/ingest/session/start` | ingest key |
| `POST` | `/v1/ingest/session/end` | ingest key |

Status codes: `202` accepted · `200` duplicate · `401` bad key · `403` wrong
scope or app mismatch · `404` unknown session · `413` batch too large · `422`
validation · `429` rate limited.

> **Change from Phase 0 + 1:** the JSON service banner moved from `/` to `/v1/`,
> because `/` is now the dashboard.

## Design notes

- **Keys are never stored in plaintext.** A token is `pb_<key_id>_<secret>`;
  `key_id` is an indexed handle, `secret` is stored only as an Argon2id hash.
  Ingest keys are write-only.
- **Exactly one writer.** A process-wide lock plus `BEGIN IMMEDIATE`, so
  `database is locked` cannot happen.
- **Idempotency lives in the same transaction as the rows**, so an SDK retry
  can never double-write.
- **Redaction happens before the write**, so PII never reaches disk.
- **Reads are half-open ranges** (`since <= ts < until`), and a bare `until`
  date is bumped by a day so `until=2026-10-22` includes that whole day.
- **Long-range charts read the rollups.** `queries.timeseries` uses
  `metrics_daily` for the whole days inside a range and the raw tables only for
  the partial days at either end; the two stretches meet on day boundaries so
  nothing is counted twice. The JSON API reports which one it used as
  `"source": "rollup" | "raw"`, and there is a test asserting the switch never
  changes a single point.
- **The dashboard and the JSON API call the same `queries.py` functions**, so
  the UI and the API can never disagree about what a number means.
- **Identifiers are never interpolated from user input.** Table and column
  names are validated against `sqlite_master` / `PRAGMA table_info` first; all
  values are bound parameters. There are tests that pass SQL-injection strings
  through every free-text parameter.
- **Redaction rule order is load-bearing.** The rules overlap — a 16-digit card
  contains a 12-digit run that looks exactly like an Aadhaar, and a `+91` number
  does too. The list runs most-specific-and-longest first (card, then phone,
  then Aadhaar) so neither is mislabelled. `tests/test_redaction.py` pins it.
- **HTMX requests get `HX-Redirect`, not a 3xx**, when unauthenticated —
  otherwise the browser would follow the redirect inside the XHR and swap the
  login page into the fragment.

## Maintenance

Raw rows are pruned at each app's `retention_days` (default 90). Long-range
charts must keep working after that, so every day is folded into
`metrics_daily` **before** its raw rows go away. Three jobs run nightly at
`POWERBASE_ROLLUP_HOUR` (01:00 UTC) on a background thread inside the server:

| Job | What it does |
|---|---|
| **rollup** | Folds yesterday into `metrics_daily` — idempotently, so a retry or a backfill is always safe |
| **prune** | Deletes raw rows past each app's retention window, then `VACUUM`s |
| **backup** | Takes an online backup, **verifies** it, and rotates old copies |

Every run is recorded in `job_runs`, which the data browser page shows as a
Maintenance panel. That ledger is also what the scheduler consults to decide
whether today's pass has already happened.

### The pruning interlock

Pruning will **not** delete a day that has not been rolled up yet. A fresh
install with a year of raw history would otherwise destroy the only copy of it,
so the deletion window is `[first rolled-up day, retention cutoff)` — both
bounds. On a first run with no rollups at all, pruning simply defers and says so.

### Running it by hand, or from cron

```bash
python scripts/maintenance.py --status                 # what ran, and rollup coverage
python scripts/maintenance.py --daily                  # roll up + prune + back up
python scripts/maintenance.py --rollup --days 30       # backfill a month
python scripts/maintenance.py --backup --keep 14
python scripts/maintenance.py --verify backups/powerbase-20261022T010000Z.db
python scripts/maintenance.py --restore backups/... --db ./powerbase.db
```

If you drive this from cron, set `POWERBASE_MAINTENANCE_ENABLED=0` so the
server's own scheduler does not also run it.

### Backups

`verify_backup` runs `PRAGMA integrity_check` and counts the core tables, so a
truncated copy is caught when it is written rather than when it is needed. A
backup nobody has ever opened is a hope, not a backup. `restore` is an
**offline** operation — stop the server first — and it keeps a `.pre-restore`
copy of whatever it is about to overwrite.

## Alerts

A self-hosted tool has no pager, so alerts are a small set of rules evaluated
over the data, shown as a banner on every dashboard page, listed on `/alerts`,
and exposed as JSON at `GET /v1/alerts`. Optionally pushed at a webhook
(`POWERBASE_ALERT_WEBHOOK_URL`) from the nightly pass, so they fire even if
nobody opens the dashboard.

| Rule | Fires when |
|---|---|
| `crash_free_rate` | Crash-free sessions fall below the warning or critical bar |
| `error_rate` | ERROR/FATAL log lines exceed the bar |
| `no_telemetry` | Nothing has been ingested for an app in `POWERBASE_SILENT_MINUTES` |
| `job_failed:*` | The last rollup, prune or backup run failed |
| `rollup_stale` | Rollups are more than `POWERBASE_ROLLUP_STALE_DAYS` behind |

Two properties are deliberate. Every rule is a **pure function** of the database
plus `now`, so evaluating twice gives the same answer and there is no
"already fired" state to get out of step. And rules **stay quiet below a
minimum sample size**: a 0% crash-free rate computed from three sessions is
noise, and an alert that cries wolf is worse than none — people learn to ignore
it.

`no_telemetry` reads `received_at` (when the server accepted the row), not the
client's `ts`. That is the right question: "is this SDK still uploading?" — and
it means a backfill of old events does not look like a live app.

## Migrations and the rollback drill

```bash
python scripts/migrate.py --status    # applied / pending / unknown
python scripts/migrate.py --verify    # integrity, required tables, pending
python scripts/migrate.py --drill     # prove the rollback path works
```

SQLite has no down-migrations, so rolling back a schema change means restoring
the pre-migration backup. `--drill` tests that claim instead of assuming it: it
copies the live database, mutates the copy the way a migration would, restores
the pristine copy over it, and checks the schema *and* the rows came back. The
live database is never touched, and the drill fails if the synthetic migration
changed nothing — a drill that cannot fail proves nothing.

Each migration also runs in a single transaction, so a migration that fails
half-way leaves no partial schema and is not recorded as applied. The failure
propagates, which is deliberate: a server must not come up on a half-migrated
database.

## Structured logging

One formatter, two formats: readable `key=value` for a terminal, or one JSON
object per line for shipping logs elsewhere (`POWERBASE_LOG_JSON=1`).

```
2026-10-22T09:14:02.118000Z INFO    powerbase.http: http_request method=POST \
  path=/v1/ingest/events status=202 duration_ms=31.4 app_id=com.example.shop
```

The access log carries the app id because the auth dependency stashes it on the
request, and every response gets an `X-Response-Time-Ms` header. Static assets
are not logged. `uvicorn`'s own access log is turned off so lines are not
duplicated.

## Performance

The load test drives a real uvicorn server over real sockets — measuring the
ASGI app in-process would skip exactly the parts that break under concurrency:

```bash
python scripts/loadtest.py                       # 10s, 8 workers
python scripts/loadtest.py --workers 2 --batch-size 500
```

It reports throughput, client- and server-side latency percentiles separately,
the status breakdown, and — the number that actually matters — whether every
accepted record is really in the database.

On a **2-core** container, batch 500, two workers:

| | |
|---|---|
| Records accepted | **3,442 / s** |
| Server latency p50 / p95 | 169 ms / 347 ms |
| Rows stored vs accepted | 21,502 / 21,502 — nothing lost |

The load test earned its keep immediately: it found two real problems.

1. **Argon2id on every request.** The hash is deliberately expensive — about
   370 ms here — which is right for a login and catastrophic on an ingest
   endpoint that authenticates on *every* request. Successful verifications are
   now remembered for `POWERBASE_AUTH_CACHE_SECONDS` (default 300). Throughput
   went from 86 to ~850 records/s before the second fix. The key row is still
   re-read every request, so **revocation and scope are never stale** — that is
   pinned by a test.
2. **`PRAGMA journal_mode=WAL` on every connection.** WAL is a persistent
   property of the file, not the connection; issuing it per connection makes
   each one take a lock, and new threadpool threads then block behind the
   current writer. It is now read (cheap) and set only when it is not already
   WAL.

With both fixed, the ingest path is **CPU-bound, not I/O-bound**: the SQLite
write of a 200-event batch takes 17 ms, while the same batch costs about 49 ms
of CPU through the full request path (Pydantic validation, redaction, JSON). So
the right number of concurrent clients is roughly the number of cores — eight
workers on two cores oversubscribe the CPU four-to-one and p50 latency goes
from 106 ms to 603 ms. More workers is not more throughput here.

## Tests

```bash
python -m pytest          # 240 passed
```

Phase 0 + 1 coverage (schema, migrations, auth, ingestion, redaction, FTS,
fingerprints, sessions, idempotency, validation, rate limiting) plus Phase 3:

- Read API auth scoping, filters, pagination, full-text search, aggregates.
- Tenant isolation: a read key cannot read another app's data.
- SQL-injection attempts through `q`, `order_by` and table names.
- Dashboard auth: anonymous redirect, forged cookie, expired cookie, throttling,
  logout, and the disabled-by-default behaviour.
- Every page and every HTMX partial renders, with a deterministic dataset.

Phase 4 coverage:

- Rollups fold a day correctly, are idempotent, write zeros for quiet days, and
  reproduce the seeded dataset exactly when re-read.
- Pruning deletes past the retention window, and **defers** when nothing is
  rolled up; it never crosses the rollup floor.
- A device is only pruned once nothing references it.
- Backups verify, rotate, and round-trip through a restore.
- The scheduler runs once a day, retries a failed backup, and starts/stops with
  the app.
- Switching a chart from raw to rollups produces **identical points**.

Phase 5 coverage:

- Every alert rule fires, stays quiet, and respects its severity thresholds and
  minimum sample size; the report is pure; the webhook delivers and swallows
  failures; the endpoint is read-key scoped.
- The verification cache skips Argon2 without weakening anything: a bad secret
  is never cached, revocation takes effect immediately, and scope is still
  enforced on a cached key.
- Structured logging in both formats, the access-log fields, and that static
  requests are not logged.
- Migration status, verify, and a rollback drill that fails when it should.
- A failing migration rolls back entirely and stays pending.
- A schema contract: every table's columns, every index, and that foreign keys
  are actually enforced.

## Visual verification

Every page was rendered in a headless browser against the seeded fixture and
inspected — see `docs/screenshots/`. That pass caught two bugs the test suite
could not see, both now fixed and pinned by regression tests:

- **The charts page rendered blank.** The template declared `const top`, which
  collides with the browser's built-in `window.top` — a top-level `const` on an
  existing global is a `SyntaxError`, so the whole script died and neither chart
  drew. Renamed to `topValues`.
- **The range selector mislabelled the range.** A `<select>` whose options do
  not include the active value silently displays its first option, so a
  365-day view claimed to be showing "last 1d". The option list now always
  includes the active value.

## Continuous integration

`.github/workflows/ci.yml` runs the suite, then a fresh instance end to end:
bootstrap, migrate, verify, rollback drill, seed, a full maintenance pass, and
the load test in `--quick` mode. The drill is in CI precisely because it is the
thing that would otherwise be skipped until the day someone needs it.

## What is not here yet

- The alert rules are a fixed set. There is no way to add a custom rule or a
  per-app threshold without editing `alerts.py`.
- The KPI summary on the Overview still aggregates the raw tables; only the
  time-series charts read rollups. That is fine at current volumes but is the
  next thing to move once `metrics_daily` is well populated.
- Restore has no CLI-side safety check that the server is stopped.
- Only the light theme has been eyeballed; the dark palette is defined but
  unverified.
- No pagination beyond "next/previous" on the log explorer and data browser.
