# Traces, metrics, capacity and drains (Step 6)

The parts of observability that are not logs.

## Traces

Any OTLP exporter can send spans; they land in `spans` and are queryable.

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT="https://logs.example.com/v1/otlp"
export OTEL_EXPORTER_OTLP_HEADERS="authorization=Bearer pb_…"
export OTEL_EXPORTER_OTLP_PROTOCOL="http/json"     # protobuf is not implemented
```

`POST /v1/otlp/v1/traces` — resourceSpans. `spanKind` becomes a name
(`server`, `client`, …), `status.code` becomes `ok`/`error`/`unset`, `http.route`
is promoted to `route_template`, and the duration is computed from the two
timestamps rather than trusted.

**A retried export does not double-write.** `(app_id, span_id)` is unique, so an
exporter's retry is a no-op.

### The join that makes traces worth having

```bash
curl -sS -G $PB/v1/traces/4bf92f3577b34da6a3ce929d0e0e4736 \
  -H "X-Powerbase-Session: $TOKEN" --data-urlencode "app_id=com.acme.api"
```

```json
{"trace_id": "4bf9…", "root_operation": "POST /orders", "duration_ms": 812,
 "span_count": 2, "error_count": 1, "services": ["checkout-api"],
 "spans": [{"operation": "POST /orders", "span_kind": "server", "status": "error", …},
           {"operation": "SELECT orders", "span_kind": "client", "status": "ok", …}],
 "logs":  [{"severity": "ERROR", "message": "upstream timeout", …}]}
```

A slow request and the error it logged stop being two unrelated rows. The join
is on `trace_id`, which both sides already carry.

## RED metrics

**Rate, Errors, Duration** — the smallest set of numbers that says whether a
service is healthy — derived from spans rather than asked of the client. A
service that sends spans gets RED for free.

```bash
curl -sS -G $PB/v1/metrics/red -H "X-Powerbase-Session: $TOKEN" \
  --data-urlencode "app_id=com.acme.api" --data-urlencode "group_by=service"
```

```json
{"groups": [{"label": "checkout-api", "spans": 20, "errors": 4, "error_rate": 0.2,
             "rate_per_second": 0.0002, "avg_ms": 480.0,
             "p50_ms": 500, "p95_ms": 860, "p99_ms": 860, "max_ms": 860}]}
```

`group_by` is `service`, `operation` or `route`. **Percentiles are exact**, not
estimated: a bounded number of groups means one indexed lookup each, which is
cheaper than being wrong about p95.

## OTLP metrics

`POST /v1/otlp/v1/metrics` — gauges, sums and histograms, kept raw. A histogram
keeps its sum in `value`, its count alongside, and its buckets in `attributes`,
because how they are read depends on the aggregation.

Points deduplicate on `(app_id, metric_name, point_time, dimension_hash)` — a
hash of the attribute set, so a retried export does not double-count.

```bash
curl -sS -G $PB/v1/otel-metrics -H "X-Powerbase-Session: $TOKEN" --data-urlencode "app_id=com.acme.api"
curl -sS -G $PB/v1/otel-metrics/queue.depth -H "X-Powerbase-Session: $TOKEN" --data-urlencode "app_id=com.acme.api"
```

Note the `/v1/otel-metrics` path: `/v1/metrics/*` is the mobile-analytics
rollups, which are a different thing.

## Capacity

```bash
curl -sS $PB/v1/capacity -H "X-Powerbase-Session: $TOKEN" | python3 -m json.tool
powerbase capacity
```

Reports the database size (with WAL), rows and rows-per-day per growing table,
measured bytes-per-day, the newest backups and their age, the last run of each
maintenance job, per-project retention, and a `warnings` list.

The warnings are the useful part: no rollup has ever run, the newest backup is
older than two days, the scheduler is off so pruning will not happen, dead alert
deliveries waiting. Each one is a thing that is about to become a problem.

## Log drains

Forward what was ingested to a SIEM, a data lake, an archive. The reason to have
this is that a Powerbase instance is rarely the only place logs need to be.

```bash
curl -sS -X POST $PB/v1/log-drains -H "X-Powerbase-Session: $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"name":"to-siem","target":"https://siem.example.com/ingest","min_severity":"WARN"}'
```

Records are POSTed as `{"records":[…]}` with the same columns an export writes.
`min_severity` decides the floor; an `app_id` scopes the drain to one project.

### At-least-once, and it says so

The cursor is a keyset position `(event_time, id)` that advances **only after the
target accepts the batch**. A failure leaves it where it was, so the next pass
retries the same records rather than skipping them.

```
flush 1  →  sent 1, ok       cursor advances
flush 2  →  sent 1, FAILED   cursor unchanged
flush 3  →  sent 1, ok       the same record again, then the cursor advances
```

The consequence is honest and worth stating: a crash between the target
accepting a batch and the cursor being written re-sends it. Exactly-once across a
network is not achievable without cooperation from the target, and pretending
otherwise would be worse than saying it plainly. A consumer that cares should
deduplicate on `event_id`, which every record carries.

```bash
curl -sS -X POST $PB/v1/log-drains/{id}/flush -H "X-Powerbase-Session: $TOKEN"
curl -sS $PB/v1/log-drains/{id}/runs    -H "X-Powerbase-Session: $TOKEN"
```

## Not done yet

- **No S3-compatible archive.** The roadmap lists it as optional and
  infrastructure-dependent. It is not implemented, and this file should say so
  rather than implying it.
- **No SSO.** The roadmap puts it last, after a protocol is implemented and
  tested.
- **OTLP protobuf is not supported** — JSON only, refused with a clear message.
- **No console page for traces or capacity.** The API and the CLI are the ways in.
- **`spans` and `metric_points` have no retention.** They grow unbounded like
  `log_events` did before Step 3.
- **Drains are HTTP only.** No Kafka, no S3, no syslog.
