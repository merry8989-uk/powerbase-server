# Export and retention (Step 3)

Getting data out, and making sure it eventually goes away.

## Export

`POST /v1/exports` — admin password. Returns a job, and (by default) runs it
immediately.

```bash
curl -sS -X POST $PB/v1/exports \
  -H "X-Admin-Password: $ADMIN" -H 'Content-Type: application/json' \
  -d '{
    "app_id": "com.example.shop",
    "format": "csv",
    "timeRange": {"from": "2026-10-01T00:00:00Z", "to": "2026-11-01T00:00:00Z"},
    "filters": [{"field": "severity", "operator": "eq", "value": "ERROR"}]
  }'
```

```json
{
  "id": "9ac444c5a23d42fdb52e7e85ff597807",
  "status": "completed",
  "format": "csv",
  "row_count": 3,
  "truncated": true,
  "result_bytes": 1689,
  "expires_at": "2026-10-29T16:09:01.051883Z",
  "download_url": "https://logs.example.com/v1/exports/9ac4…/download?token=…"
}
```

Formats: **`csv`**, **`json`** (a streamed array), **`jsonl`** (one object per
line). The exported columns are fixed and documented — a CSV whose columns
depend on which row happened to come first is not something anyone can parse:

```
event_time, received_at, event_id, source, severity, event_type, service_name,
environment, app_version, release_id, request_id, trace_id, span_id,
route_template, http_method, status_code, duration_ms, error_code,
error_fingerprint, message, attributes
```

Rows are always written **oldest first**, whatever the console was sorted by, so
the file reads chronologically.

### Three things the export path refuses to do

**Never build the result in memory.** Rows are paged with the same keyset cursor
the console uses and written straight out. A large export costs a file, not a
heap.

**Never hand back a file that looks complete but is not.** `POWERBASE_EXPORT_MAX_ROWS`
(default 50,000) caps a single export. Hitting it is reported in three places:

- `truncated: true` on the job
- `X-Powerbase-Truncated: true` on the download response
- `X-Powerbase-Rows` on the download response, so a script that only reads
  headers still has a chance to notice

A silently truncated export is the worst outcome available: it looks
authoritative and is wrong.

**Never let a link outlive its welcome.** The `download_url` carries an HMAC and
an expiry, both checked on every fetch. That is why the download endpoint is the
one route that does not need the admin password — it is a link you can open in a
browser or hand to a colleague.

```bash
curl -sS -OJ "$DOWNLOAD_URL"       # -OJ keeps the filename from the header
```

A tampered token is `401`, a token for a different job is `403`, an expired link
is `410 Gone`, and the file is deleted at the same moment the link dies. A row
that says "expired" while the CSV is still on disk has not expired.

### Large exports

```bash
# queue it and come back
curl -sS -X POST $PB/v1/exports -H "X-Admin-Password: $ADMIN" \
  -H 'Content-Type: application/json' \
  -d '{"app_id":"com.example.shop","format":"jsonl","defer":true}'
```

A deferred job stays `queued` until something finishes it:

- the nightly maintenance pass, which also expires stale results
- `powerbase exports --run`, for now
- `POST /v1/exports/{id}` is **not** a thing — run it with the CLI or wait

### Managing jobs

```bash
powerbase exports                 # list jobs
powerbase exports --run           # finish queued ones
powerbase exports --expire        # expire stale ones and delete their files
```

| Method | Path | Does |
|---|---|---|
| `POST` | `/v1/exports` | Create (and run) an export |
| `GET` | `/v1/exports` | List jobs |
| `GET` | `/v1/exports/{id}` | One job, with a fresh download link |
| `GET` | `/v1/exports/{id}/download?token=` | Stream the file |
| `DELETE` | `/v1/exports/{id}` | Delete the job and its file |

### Redaction, applied again

Rows are redacted at ingest. The export path redacts them **again**, because a
row written while `POWERBASE_DISABLE_REDACTION` was set would otherwise leave
the building unredacted. The export is the last place that can catch it.

## Retention

Retention is per project (`apps.retention_days`) and runs nightly.

```bash
powerbase maintenance --daily     # roll up, prune, compact, back up
```

### The rule that makes it safe

**Never delete a row whose day was never rolled up.**

Raw rows older than the retention window are candidates. But the deletion window
is `[first_rolled_up_day, retention_cutoff)` — it needs **both** bounds. Rows
from before the first rolled-up day survive regardless of their age, because
those days were never folded into `metrics_daily` and deleting them would
destroy history that exists nowhere else.

That is why the prune refuses to run at all when no rollup has happened yet:

```json
{"skipped": true, "reason": "deferred: no rollups yet, nothing is safe to prune"}
```

### `log_events` is covered

Observability events are folded into `metrics_daily` by the **same** daily job as
the analytics tables:

| Metric | Meaning |
|---|---|
| `log_events` | total events that day |
| `log_events_errors` | those at ERROR or FATAL |
| `log_events_by_severity` | one row per severity that occurred |

Because it is the same job, the same `rollup` ledger day protects both. A day
that is safe to prune is safe for everything.

What this buys you: the twelve-month chart still works after the twelve-month-old
rows are gone.

```
$ powerbase maintenance --daily
  rollup   {'day': '2026-10-21', 'apps': 1, 'rows': 8}
  prune    {'deleted': {'com.example.shop': {'log_events': 12, ...}}, 'total': 12}
  vacuum   ok
  backup   {'verified': True, ...}
  exports  {'completed': {'queued': 0}, 'expired': {'expired': 1, 'files_removed': 1}}
```

### Housekeeping rows get their own window

Two tables are not observability data and are not folded into any rollup, so
they are not under the floor above. They are pruned on their own clock:

| Table | What it holds |
|---|---|
| `alert_deliveries` | every attempt to tell somebody about an incident |
| `drain_runs` | every log-drain run, and whether it worked |

```bash
POWERBASE_OPERATIONAL_RETENTION_DAYS=30   # the default
```

**The rule that matters more than the window.** A `pending` or `failed`
delivery is a notification somebody has not been told about yet. Age does not
make it less owed, so those are **never** pruned — only `sent` and `dead` are
finished, and only those are removed. A drain run with no `finished_at` is
still in progress and is left alone.

Deleting an undelivered row would drop an alert silently, which is the exact
failure the delivery queue exists to prevent. There is a test for it.

Setting the window to `0` disables the housekeeping prune entirely.

## Audit trail

Every export — created, downloaded, deleted, expired — and every key event is
recorded in `audit_events`. The reason is practical: when someone asks "who took
a copy of this data, and when", an answer that does not exist is worse than not
having the feature.

```bash
powerbase audit --limit 20
powerbase audit --type export.downloaded
curl -sS -G $PB/v1/audit -H "X-Admin-Password: $ADMIN" \
  --data-urlencode "event_type=export.created"
```

Event types today: `export.created`, `export.downloaded`, `export.deleted`,
`export.expired`, `key.created`, `key.rotated`, `key.revoked`,
`retention.changed`, `project.created`.

**Append-only by construction.** Nothing in this codebase updates or deletes a
row in `audit_events`. An audit log the application can edit is not an audit log.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `POWERBASE_EXPORT_DIR` | `./exports` | Where result files are written |
| `POWERBASE_EXPORT_MAX_ROWS` | `50000` | Rows per export before truncation |
| `POWERBASE_EXPORT_TTL_SECONDS` | `604800` | Download link lifetime (7 days) |

Put `POWERBASE_EXPORT_DIR` on the same volume as the database, and remember that
it grows: expired results are deleted nightly, but a week's worth of exports can
be large.

## Not done yet

- **No S3-compatible archive.** The roadmap lists it as optional and
  infrastructure-dependent; it is not implemented, and the feature matrix should
  say so rather than implying it.
- **No export UI in the console.** The API and the CLI are the ways in.
- **Deferred jobs wait for the maintenance pass** (or `powerbase exports --run`).
  There is no dedicated worker, so a deferred job can sit for up to one
  maintenance interval.
- **`audit_events` is never pruned.** It only grows, deliberately — an audit
  trail you prune is not an audit trail. `alert_deliveries` and `drain_runs`
  *are* pruned; see the housekeeping window above. At this scale the audit
  table is
  negligible; at a larger one it will need its own retention.
