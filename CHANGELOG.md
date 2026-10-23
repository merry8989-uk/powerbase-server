# Changelog

All notable changes to Powerbase. The version lives in `powerbase/version.py`
and is read from there by the build, by `/healthz`, and by the dashboard — one
source of truth, so the metadata and the running server cannot disagree.

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
