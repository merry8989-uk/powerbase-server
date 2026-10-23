# Changelog

All notable changes to Powerbase. The version lives in `powerbase/version.py`
and is read from there by the build, by `/healthz`, and by the dashboard — one
source of truth, so the metadata and the running server cannot disagree.

## 1.10.0

Multi-worker deployment: the build says what it supports, instead of leaving an
operator to work it out from rate limits that do not add up.

### Added
- **A startup warning and a `/v1/status` field for the deployment shape.** Two
  things in this build are in-process and count in memory, and neither said so
  where an operator would look:
  - **Rate limits multiply.** The per-project token bucket is in-process, so N
    workers give each project N independent budgets — the configured limit is
    effectively N times what it says. Nothing breaks; the number is just wrong.
  - **The scheduler runs N times.** The job ledger interlocks the daily pass, so
    the rollup and prune do not happen twice a day, but every interval pass runs
    once per worker.
- **`POWERBASE_WORKERS`**, falling back to `WEB_CONCURRENCY` (what uvicorn and
  gunicorn set), falling back to 1. `POWERBASE_WORKERS` wins, so an operator can
  say "I know, it is one process".
- `docs/multi-worker.md` — the arithmetic, the log lines, and what to do instead.
- `powerbase/deployment.py`, a pure function so the check is testable without
  starting a server.

### Notes
- A single-process deployment gets an empty warnings list and must stay silent:
  a warning that fires on the supported shape is wallpaper.
- The warning names the **arithmetic**, not just the problem — "10/s becomes
  40/s" is actionable in a way that "rate limits are per process" is not.
- No shared rate-limit store and no scheduler leader election. Both are written
  down as not done rather than implied.

## 1.9.0

The two deployment paths that had never been run.

Both `Dockerfile` and `scripts/install.sh` were written and **never executed** —
the development sandbox has no Docker, no root and no systemd. A missing `COPY`,
a wrong base image, or a typo in a path would have been found by the first
self-hoster to try it. CI can run both, so now it does.

### Added
- **A `docker` CI job**: builds the image, boots it, waits for `/healthz`,
  asserts the version inside the image matches `powerbase/version.py`, asserts
  the container reports itself healthy, and asserts the schema was created on
  boot — a container that starts empty and 500s on every request would otherwise
  pass a health check. Logs are printed on failure.
- **An `install-script` CI job**: `shellcheck`, then `--dry-run` with and without
  a domain, then assertions on every rendered artefact, then
  `systemd-analyze verify` on the unit.
- **`scripts/install.sh --dry-run`** — writes every file the script would write
  into a staging directory and runs none of the commands that change the system.
  Every system-changing call goes through a `run` helper, so the dry run is a
  property of the script rather than a second script that drifts from it.

### What this does and does not prove
- **Docker**: a real build, a real boot, a real health check. Verified.
- **install.sh**: the **artefacts** are verified — the unit's syntax and every
  hardening directive, the environment file, the bind-address branch, the CLI
  wrapper, and that a real admin password is generated. The **install itself is
  still not proven**, because that needs a real host with root and systemd. The
  script says so when it finishes a dry run, and this entry says so too.

## 1.8.0

Analytics events and crashes now appear in the Logs Explorer.

### Added
- **Crashes and analytics events are projected into `log_events`**, so the
  explorer can finally answer "what happened in this session". Before this they
  went only into `events` / `logs` / `crashes` and were **invisible to the
  explorer** — the one screen built for that question. A crash is a diagnostic
  event; it was the one thing the diagnostics view could not show.
  - `crash` at `FATAL` when unhandled and `ERROR` when handled, with the crash
    row's fingerprint reused and the first stack frame carried as an attribute.
  - `analytics.event` at `INFO`, with the event name as the message, `screen` as
    `route_template`, and `properties` as attributes.
  - `analytics.log` at the log's own level, with `tag` and `context` as
    attributes.
- `docs/analytics-projection.md` — what it does, and what it costs.

### Design notes
- **A projection, not a move.** The source table stays the source of truth for
  its own dashboard page and no client changes. The `log_events` row is written
  in the **same transaction**, so the two cannot diverge: either both land or
  neither does.
- **The cost is stated rather than hidden: each crash and analytics event is now
  stored twice.** That is the price of a complete explorer without breaking
  every installed client. Consolidating the two models is a larger change and
  should be deliberate.
- **Not a view.** A `UNION ALL` view would avoid the duplication and break the
  cursor: `log_events` paginates on `(event_time, id)` and ids collide across
  tables, so a union needs a synthetic key and the keyset cursor stops being
  exact.
- The projected `event_id` is derived, not random, so a collision is a bug
  rather than a coin flip — and the insert is a plain `INSERT`, not
  `OR IGNORE`, so it fails loudly.
- Redaction is carried through: the projection uses the writer's already-redacted
  values, not the raw ones.

### Known limitation
- Projected rows are tagged `source = android_sdk`, because the analytics
  endpoints are the mobile SDK's contract. A web client posting to the same
  endpoints would be mislabelled. Adding a value to the `source` vocabulary is a
  table rebuild; documented rather than done.

## 1.7.0

The read path, measured. `scripts/querybench.py` is new and immediately found
two real problems.

### Fixed
- **Deep pagination was about seven times more expensive than it should be.**
  The keyset cursor was written as
  `event_time < ? OR (event_time = ? AND id < ?)`. The equivalent row-value
  comparison `(event_time, id) < (?, ?)` means the same thing and plans
  completely differently: as an OR, SQLite chose `MULTI-INDEX OR` and had to
  sort everything after the cursor before the LIMIT could apply. On 60,000 rows
  page 1 cost 32 ms and every page after it 120-170 ms, growing with the table.
  It is now a single index seek: pages sit at 16-28 ms and the cost no longer
  grows with depth (5.4x growth to 1.4x).
- **Every explorer query was paying for a sort.** The time-ordered indexes were
  declared `(app_id, event_time DESC)` while the query orders by
  `event_time DESC, id DESC`, so the planner could not satisfy the ordering from
  the index and reported `USE TEMP B-TREE FOR RIGHT PART OF ORDER BY` on all
  five scenarios. Migration 0010 declares `id DESC` as the last index column and
  the sort step is gone. (The wall-clock benefit of this one is not
  demonstrable at the sizes tested — the sort was not the bottleneck there — so
  the claim is about the plan, which is provable, and not about a speedup.)

### Added
- **`scripts/querybench.py`** — seeds a table of a realistic size, times five
  scenarios (default, wide, filtered, narrow, free text), walks keyset
  pagination to depth, and prints `EXPLAIN QUERY PLAN` for each. `--compare`
  measures the pre-0010 index shapes side by side. `--quick` runs in CI.
- Three tests that assert the **plan** rather than a timing, because a timing
  test would pass on a fast machine with the bug still in place.

### Notes
- Free-text search (`q=`) remains a `LIKE` scan no index can serve — 21-36 ms on
  60,000 rows. Documented rather than fixed.

## 1.6.0

A bound on the housekeeping tables.

### Added
- **`alert_deliveries` and `drain_runs` are now pruned.** Both were written on
  every maintenance pass and nothing ever removed them, so they grew without a
  bound. They are not observability data and are not folded into any rollup, so
  they have their own window: `POWERBASE_OPERATIONAL_RETENTION_DAYS`, default
  30 days, `0` to disable.

### The rule that matters more than the window
- **A notification that has not been delivered is never pruned.** Only `sent`
  and `dead` deliveries are finished; `pending` and `failed` are still owed to
  somebody, and age does not make them less owed. A drain run with no
  `finished_at` is still in progress and is left alone. Deleting an undelivered
  row would drop an alert silently, which is the exact failure the delivery
  queue exists to prevent. There is a test for it.

### Notes
- Housekeeping is pruned whether or not the observability tables can be, so an
  instance whose rollups have stopped does not also accumulate these rows.
- `audit_events` remains unpruned, deliberately: an audit trail you prune is not
  an audit trail.

## 1.5.0

The Logs Explorer specification's request and response envelope.

### Added
- **`query_id`** on every log query, returned in a new `meta` block with
  `duration_ms` and the resolved `sort`. Queries slower than a second are
  logged with that id, so "which query was slow" has an answer.
- **A `pagination` block** (`next_cursor`, `has_more`, `limit`) alongside the
  existing flat keys, matching the specification's shape.
- **The specification's request body**, in full: a named filter object
  (`platform`, `severity`, `category`, `environment`, `search`, `time_range`)
  and a nested `pagination`. The general filter list the console sends still
  works.
- **`time_range` as a duration string** — `"30m"`, `"24h"`, `"7d"`, `"2w"` —
  resolved against the server clock, with a per-unit cap. A `{from, to}` object
  is still accepted.
- **Sort field aliases**: `timestamp` and `time` map to `event_time`, and `id`
  is sortable as the stable tie-break the specification asks for.
- `category`, `source_type`, `source_id` and `platform_version` on log events,
  filterable and included in exports.
- A `TRACE` severity level, in the ingest API, the OTLP severity bands, and the
  drain severity ordering.

### Fixed
- **A release build carried the wrong version.** `pyproject.toml` hardcoded
  `0.5.0` while the code was at `1.5.0`; the version is now read from
  `powerbase/version.py`, and the built metadata says `1.5.0`.
- **`?app_id=` was ignored on `POST /v1/query/log-events`.** The POST verb read
  `app_id` from the body while the GET verb read it from the query string, so a
  request that appeared to ask for one project answered with another. It never
  leaked across tenants (a key is bound to one project), and it now refuses with
  `403`, as the GET verb already did.
- **Two 500s on the refusal path.** An unknown filter name crashed the error
  message itself (`dict | set`), and a non-object `time_range` was iterated
  (`set(7)`). Both answered `500` for what a user reaches by making a typo, and
  both now answer `400`.
- **Retention did not cover `spans` or `metric_points`.** Added in 1.3.0 but
  left out of the rollup and the prune, so they grew without a bound. Both are
  now folded into the same job.
- The OTLP severity band for numbers 1–4 was reported as `DEBUG`; per the
  specification it is `TRACE`.

## 1.4.0

Free forever, made testable; the last field gaps from the Logs Explorer spec.

### Added
- `LICENSE` (MIT). **It was missing**, and a test found that.
- `docs/free-forever.md` and `docs/privacy.md`.
- `tests/test_free_forever.py` — fails the build if monetisation vocabulary
  appears, if a permission becomes plan-shaped, if a route names a permission
  that does not exist, if the licence goes missing, or if a fresh install starts
  making outbound calls by itself.

### Fixed
- **A shared type let a bad value reach the database.** `LogLevel` was used by
  both the mobile-analytics path (no `TRACE` in its table) and the log-event
  path (which gained one), so adding `TRACE` to the shared type made it valid
  for the table that has no such column — turning a validation error into a
  `500` from a `CHECK` constraint. The two vocabularies are now two types.

## 1.3.0

Traces, metrics, capacity and drains.

### Added
- **OTLP traces** (`POST /v1/otlp/v1/traces`) and **OTLP metrics**
  (`POST /v1/otlp/v1/metrics`), JSON only. Span kinds and statuses are named,
  `http.route` is promoted to a column, durations are computed from the two
  timestamps rather than trusted, and both deduplicate on retry.
- **The trace join**: `/v1/traces/{id}` returns the spans *and* the log events
  sharing the trace id, so a slow request and the error it logged stop being two
  unrelated rows.
- **RED metrics** (`/v1/metrics/red`) — rate, errors and duration per service,
  operation or route template, with exact percentiles.
- **Capacity** (`/v1/capacity`) — sizes, rows per day, measured bytes per day,
  backup ages, the last run of each job, and a warnings list.
- **Log drains** — forward ingested events to an HTTP target with a keyset
  cursor that advances only after the target accepts the batch.
- `powerbase/authz.py`, so authorisation lives in one place.

## 1.2.0

Alerts and team access.

### Added
- **Incidents** with a cooldown, deduplication by `(project, rule)`, escalation
  that notifies inside the cooldown, acknowledgement that silences, and a
  recovery notice. A continuing problem no longer re-notifies on every pass.
- **A delivery queue** with exponential backoff and a visible dead-letter state,
  plus email over the standard library's `smtplib`.
- **Users, roles, sessions, invites and project membership.** Four roles,
  permissions as a table, lockout after five failures, single-use expiring
  invites, and a last-owner rule.
- The admin password remains as break-glass.

### Fixed
- **A lockout answered `401` instead of `429`**, because the status was chosen
  by matching a substring in an error *message* — and the message said "too many
  failed attempts", which does not contain "locked". Refusals now carry a
  machine-readable reason and the status is chosen from that.

## 1.1.0

SDKs and OpenTelemetry logs.

### Added
- `powerbase-telemetry`, a TypeScript SDK with browser, Node and core entry
  points and **zero runtime dependencies**: bounded queue, batching, exponential
  backoff with full jitter, `Retry-After` honoured, circuit breaker, and a
  disabled mode that sends nothing.
- **OTLP/HTTP JSON log ingestion**, with `db.statement` reduced to its shape.

### Fixed
- **A cross-tenant hole in the idempotency ledger.** It was keyed on `batch_id`
  alone, globally, so a key for one project could send another project's batch
  id first and have that project's genuine batch **silently discarded as a
  duplicate**. Now keyed on `(app_id, batch_id)`, with a regression test.
- An awaited backoff could never resolve (an unref'd timer); `Retry-After` was
  clamped by our own maximum; built-in transports threw instead of returning
  their result type.

## 1.0.0

Export and retention.

### Added
- Streaming CSV, JSON and JSONL exports with signed, expiring download tokens,
  a row cap reported in three places, and redaction re-applied on the way out.
- `export_jobs` and `audit_events`; the audit trail became queryable.
- `log_events` folded into the nightly rollup and the prune.

### Fixed
- `Path.unlink(missing_ok=True)` returns `None`, so the deletion count was
  always zero; an empty CSV had no header row; an expired link answered `409`
  instead of `410`.

## 0.9.0

Own-platform installation.

### Added
- `scripts/install.sh` — bare-metal install with a systemd unit, Caddy, and an
  idempotent re-run.
- The `powerbase` management CLI.
- `/healthz`, `/v1/status`, and a public status page.
- `docs/RUNBOOK.md`.

### Fixed
- **`powerbase status` reported "up" when it was not.** An unrelated service
  answered `200` on the same port. It now verifies identity and distinguishes
  "up", "not answering" and "port is busy with something else".

## 0.8.0

The API, written down.

### Added
- `openapi.json` (OpenAPI 3.1.0), `docs/API.md`, and a worked
  `examples/api_client.py` proven against a live server.
- `Dockerfile`, `docker-compose.yml`, `deploy/Caddyfile`.

## 0.7.0

The console and the query API.

### Added
- An allowlisted query language: no SQL from a client, ever. Bound parameters,
  keyset pagination, and a real SQLite progress-handler query timeout.
- Saved searches, and the `/explorer` console page with a histogram and a
  detail drawer.

## 0.6.0

Secure ingestion.

### Added
- `POST /v1/ingest/log-events`, a 25-column observability table with eight
  indexes, and `powerbase/attributes.py`: credential-named keys dropped whole,
  value redaction, and caps on depth, count and length with the drops reported.
- API key administration with rotation, revocation and expiry.
- Per-project rate limiting.

## 0.5.0 and earlier

The Phase 0 + 1 foundation this was built on: the mobile-analytics ingestion
API, the dashboard, crash grouping, sessions, devices, migrations, backups,
retention and the alert rules.
