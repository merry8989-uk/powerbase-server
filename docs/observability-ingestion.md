# Observability ingestion (Step 1)

This adds a second event model next to the mobile-analytics one. They are two
models, not two versions of one:

| | `events` | `log_events` |
|---|---|---|
| Answers | what are users doing | what is the software doing |
| Shape | `name`, `category`, `screen`, `properties` | `severity`, `route_template`, `status_code`, `duration_ms`, `trace_id`, `error_fingerprint` |
| Endpoint | `POST /v1/ingest/events` | `POST /v1/ingest/log-events` |
| Who sends it | the Android SDK | browser, server, OpenTelemetry |

A browser or backend log had nowhere to live in the first model, which is why
websites could not be integrated before this.

## `POST /v1/ingest/log-events`

Authenticated with an **ingest-scoped** key. The `app_id` in the body must match
the key's app or the request is refused with `403` — that is the tenant boundary.

```json
{
  "batch_id": "b-2026-10-22-1",
  "app_id": "com.example.shop",
  "sdk_version": "1.0.0",
  "events": [
    {
      "event_id": "0f8b1c2e-...",
      "ts": "2026-10-22T09:00:00Z",
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
      "message": "upstream timeout",
      "attributes": { "region": "ap-south-1", "retry": 2 }
    }
  ]
}
```

Response:

```json
{
  "batch_id": "b-2026-10-22-1",
  "kind": "log_events",
  "duplicate": false,
  "accepted": 1,
  "skipped_duplicates": 0,
  "dropped_attributes": 0
}
```

`202` means something was accepted; `200` means the whole batch was a retry.

### Allowed values

- `source` — `browser_sdk`, `server_sdk`, `android_sdk`, `otel`, `system`
- `severity` — `DEBUG`, `INFO`, `WARN`, `ERROR`, `FATAL`
- `http_method` — one of the standard verbs
- `status_code` — 100–599
- `duration_ms` — zero or more

Anything else is a `422`.

### Idempotency

Two layers, because retries happen at two levels:

1. `batch_id` is recorded in the same transaction as the rows. Re-sending a batch
   is a no-op.
2. `(app_id, event_id)` is unique. A *different* batch containing an event id we
   have already stored stores only the genuinely new rows and reports the rest as
   `skipped_duplicates`.

### Attribute rules

Attributes are bounded and filtered before storage:

- A key whose **name** names a credential is dropped entirely, value and all.
  This covers `password`, `secret`, `token`, `authorization`, `cookie`,
  `private_key`, `api_key`, `database_url`, `card_number`, `cvv`, `ssn`,
  `aadhaar`, `otp` and the usual compound spellings — `apiKey`, `api_key`,
  `x-api-key`, `accessToken`, `user.password`.
  `sort_key`, `keyboard`, `monkey`, `author` and `session_count` are *not*
  caught, because a bare `key` is deliberately not on the list.
- Values are then redacted as everywhere else in Powerbase (emails, phone
  numbers, Aadhaar, PAN, card numbers, bearer tokens).
- At most 64 keys per object, 4 levels of nesting, 2048 characters per value.
  Values past the length cap are truncated; keys past the count cap are dropped.
- Everything dropped is counted in `dropped_attributes` so an integrator can see
  that something they sent was refused rather than silently losing it.

`redaction_version` is stored per row: `1` is the current rule set, `0` means the
install has redaction disabled. A stored row can therefore always be traced to
the rules that produced it.

### Rate limits

Two token buckets, both must allow:

| Bucket | Keyed by | Settings |
|---|---|---|
| Per key | `key_id` | `POWERBASE_RATE_BURST`, `POWERBASE_RATE_PER_SECOND` |
| Per project | `app_id` | `POWERBASE_PROJECT_RATE_BURST`, `POWERBASE_PROJECT_RATE_PER_SECOND` |

The per-key bucket stops one key hammering the server. The per-project bucket
stops one project minting many keys and spending everyone else's headroom. Both
answer `429` with a `Retry-After` header.

## Key administration

`/v1/keys` is guarded by the **admin password**, sent as `X-Admin-Password`. No
API key can reach it — not an ingest key and not a read key. Otherwise a leaked
key could renew itself.

| Method | Path | Does |
|---|---|---|
| `GET` | `/v1/keys?app_id=` | List key metadata. Never returns a hash or a secret. |
| `POST` | `/v1/keys` | Create. Returns the token **once**. |
| `POST` | `/v1/keys/{key_id}/rotate` | Mint a replacement, then revoke the old key. |
| `POST` | `/v1/keys/{key_id}/revoke` | Revoke. |

Rotation mints before revoking, so there is no window in which neither key works
— a deploy can pick up the new token while the old one is still briefly valid.
The old row is kept with `revoked_at` set and the new row records `rotated_from`,
so rotation is auditable rather than destructive.

Keys may carry `expires_in_days`. An expired key is refused with `401`, and the
check happens before the Argon2 verify, so refusing one costs nothing.

If no admin password is configured, the whole key-administration surface answers
`503` rather than falling open.

## Not done yet

Honest scope note for this step:

- `log_events` is **not** yet covered by retention pruning or rollups. That is
  Step 3.
- There is no console UI for `log_events` yet. The existing dashboard reads the
  analytics tables. That is Step 2.
- No OpenTelemetry wire protocol (OTLP) yet — this endpoint takes the
  observability *shape*, but an OTLP collector cannot talk to it. That is Step 6.
