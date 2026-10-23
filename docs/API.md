# Powerbase API

Base URL: `https://<your-host>/v1` — or `http://localhost:8080/v1` locally.

- **Interactive docs:** `/docs` (Swagger UI)
- **Machine-readable spec:** `/openapi.json` (OpenAPI 3.1)

Everything below is authenticated. There are exactly two kinds of credential,
and they are not interchangeable.

| Credential | Header | Can |
|---|---|---|
| **Ingest key** (`pb_…`, scope `ingest`) | `Authorization: Bearer pb_…` | Write events into **its own project**. Cannot read. Cannot administer. |
| **Read key** (`pb_…`, scope `read`) | `Authorization: Bearer pb_…` | Read **its own project**. Cannot write. Cannot administer. |
| **Admin password** | `X-Admin-Password: …` | Key administration and saved searches. Not a key; no key can reach these. |

A key is bound to one project. Sending an `app_id` that is not the key's own is a
`403`, not a silent retarget.

```bash
export PB=http://localhost:8080
export INGEST=pb_xxxxxxxxxxxx_yyyy...
export READ=pb_aaaaaaaaaaaa_zzzz...
export ADMIN=your-admin-password
```

---

## Health

```bash
curl -sS $PB/v1/health
```

```json
{"status":"ok","db":"ok","journal_mode":"wal","version":"0.7.0"}
```

`503` if the database is unreadable.

---

## Ingest

Batches only. Every ingest response carries `batch_id` and `duplicate`.

### `POST /v1/ingest/log-events` — observability events

The shape for browsers, backends and OpenTelemetry. See
[observability-ingestion.md](observability-ingestion.md) for the full field
reference.

```bash
curl -sS -X POST $PB/v1/ingest/log-events \
  -H "Authorization: Bearer $INGEST" -H 'Content-Type: application/json' \
  -d '{
    "batch_id": "b-2026-10-22-1",
    "app_id": "com.example.shop",
    "events": [{
      "event_id": "0f8b1c2e-...",
      "ts": "2026-10-22T09:00:00Z",
      "source": "server_sdk",
      "severity": "ERROR",
      "event_type": "http.request",
      "service_name": "checkout-api",
      "environment": "production",
      "route_template": "/orders/{id}",
      "http_method": "POST",
      "status_code": 500,
      "duration_ms": 812,
      "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
      "error_fingerprint": "fp-checkout-1",
      "message": "upstream timeout",
      "attributes": {"region": "ap-south-1", "retry": 2}
    }]
  }'
```

```json
{"batch_id":"b-2026-10-22-1","kind":"log_events","duplicate":false,
 "accepted":1,"skipped_duplicates":0,"dropped_attributes":0}
```

`202` when something was accepted, `200` when the whole batch was a retry.
`dropped_attributes` counts keys that named a credential and were refused.

### The mobile endpoints

| Endpoint | Body |
|---|---|
| `POST /v1/ingest/events` | analytics events (`name`, `screen`, `properties`) |
| `POST /v1/ingest/logs` | `level`, `tag`, `message`, `stacktrace` |
| `POST /v1/ingest/crashes` | `kind`, `message`, `stacktrace` |
| `POST /v1/ingest/session/start` | session + device info |
| `POST /v1/ingest/session/end` | `duration_ms`, `end_reason` |

These are the shapes the shipped Android SDK sends. They are a different model
from `log_events`, not an older version of it, so the paths stay separate.

---

## Query

### `GET /v1/query/log-events`

```bash
curl -sS -G $PB/v1/query/log-events \
  -H "Authorization: Bearer $READ" \
  --data-urlencode "severity=ERROR" \
  --data-urlencode "min_status=500" \
  --data-urlencode "limit=50"
```

Filters: `severity`, `source`, `event_type`, `service_name`, `environment`,
`route_template`, `app_version`, `trace_id`, `request_id`, `error_fingerprint`,
`status_code`, `min_status`, `max_status`, `min_duration_ms`, `q`.
Also `since`, `until`, `sort`, `direction`, `limit`, `cursor`.

```json
{"items":[{ "...": "..." }],"count":2,"limit":50,"next_cursor":"MjAyNi0xMC0yMlQw…",
 "has_more":true,"truncated":true,"elapsed_ms":3,
 "sort":{"field":"event_time","direction":"desc"}}
```

### `POST /v1/query/log-events`

The same query as validated JSON. Unknown keys are refused.

```bash
curl -sS -X POST $PB/v1/query/log-events \
  -H "Authorization: Bearer $READ" -H 'Content-Type: application/json' \
  -d '{
    "timeRange": {"from": "2026-10-22T00:00:00Z", "to": "2026-10-23T00:00:00Z"},
    "filters": [
      {"field": "severity", "operator": "eq", "value": "ERROR"},
      {"field": "duration_ms", "operator": "gte", "value": 500}
    ],
    "sort": {"field": "duration_ms", "direction": "desc"},
    "limit": 100
  }'
```

Fields and operators are allowlisted; a bad one is a `400` with the reason.
Sortable: `event_time`, `received_at`, `severity`, `duration_ms`, `status_code`.

### Pagination

```bash
curl -sS -G $PB/v1/query/log-events -H "Authorization: Bearer $READ" \
  --data-urlencode "limit=100" --data-urlencode "cursor=$NEXT"
```

Keyset, not offset: the next page is anchored to the last row of the current
one, so events arriving mid-read cannot shift the window.

### `GET /v1/query/log-events/stats`

```bash
curl -sS -G $PB/v1/query/log-events/stats -H "Authorization: Bearer $READ" \
  --data-urlencode "bucket=hour"
```

```json
{"bucket":"hour","buckets":[{"bucket":"2026-10-22T09","count":412,"errors":7}],
 "by_severity":{"INFO":3000,"ERROR":37},"total":3157,"elapsed_ms":4}
```

### `GET /v1/query/log-events/{event_id}`

The full row plus `related` — events sharing a trace id or an error fingerprint.
`?related=false` skips that lookup.

### The analytics read API

`/v1/query/events`, `/logs`, `/crashes`, `/crash-groups`, `/sessions`,
`/devices`, `/tables`, each with a `/{id}` detail route, plus
`/v1/metrics/summary`, `/timeseries` and `/top`.

---

## Keys

Admin password only. No API key can reach these, which is what stops a leaked
ingest key from renewing itself.

```bash
# create — the token comes back once
curl -sS -X POST $PB/v1/keys -H "X-Admin-Password: $ADMIN" \
  -H 'Content-Type: application/json' \
  -d '{"app_id":"com.example.shop","scope":"ingest","label":"website","expires_in_days":365}'

# list — metadata only, never a hash or a secret
curl -sS -G $PB/v1/keys -H "X-Admin-Password: $ADMIN" \
  --data-urlencode "app_id=com.example.shop"

# rotate — mints the replacement, then revokes the old one
curl -sS -X POST $PB/v1/keys/pb_abc123/rotate -H "X-Admin-Password: $ADMIN"

# revoke
curl -sS -X POST $PB/v1/keys/pb_abc123/revoke -H "X-Admin-Password: $ADMIN"
```

Rotation mints before revoking, so there is no window in which neither key
works. The old row keeps `revoked_at` and the new one records `rotated_from`, so
rotation is auditable rather than destructive.

---

## Saved searches

Admin password only.

```bash
curl -sS -X POST $PB/v1/saved-queries -H "X-Admin-Password: $ADMIN" \
  -H 'Content-Type: application/json' \
  -d '{"app_id":"com.example.shop","name":"slow checkout",
       "definition":{"filters":[{"field":"duration_ms","operator":"gte","value":800}]}}'

curl -sS -G $PB/v1/saved-queries -H "X-Admin-Password: $ADMIN" \
  --data-urlencode "app_id=com.example.shop"
```

A definition is validated on the way in and again on the way out, so a saved
search can never hold something the live query path would refuse.

---

## Alerts

```bash
curl -sS -G $PB/v1/alerts -H "Authorization: Bearer $READ"
```

---

## Errors

| Status | Means |
|---|---|
| `400` | The query or body was refused, with the reason in `detail` |
| `401` | Missing, malformed, unknown, revoked or expired credential |
| `403` | The credential is valid but not allowed here — wrong scope, or another project's `app_id` |
| `404` | No such row |
| `413` | Batch larger than `POWERBASE_MAX_BATCH_SIZE` |
| `422` | Schema violation (wrong type, unknown key, bad enum) |
| `429` | Rate limited; `Retry-After` in seconds |
| `503` | Database unreadable, or the admin API has no configured password |

Rate limiting is two token buckets and both must allow: one per key, one per
project. The second exists so one project cannot mint many keys and spend
everyone else's headroom.

---

## A runnable example

`examples/api_client.py` does the whole flow against a live server — create a
key, ingest, query, page, read a detail, save a search:

```bash
PB=http://localhost:8080 ADMIN=your-admin-password python examples/api_client.py
```
