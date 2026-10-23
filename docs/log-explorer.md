# Log explorer and query API (Step 2)

The console for observability events, and the API behind it. Both call the same
`logquery` functions, so the UI and the JSON API cannot disagree about what a
filter means.

## The rule

**No client string ever reaches SQL as SQL.** A filter field is looked up in an
allowlist, an operator must be one the field's type supports, and every value is
a bound parameter. A request naming an unknown field, an operator the type does
not allow, or a value of the wrong type is a `400` with the reason — never a
silently empty result. A filter that quietly does nothing is worse than an
error, because the results look plausible.

## Allowed fields

| Field | Type | Operators |
|---|---|---|
| `severity` | text | `eq`, `neq`, `contains`, `prefix`, `suffix`, `in` |
| `source` | text | same |
| `event_type` | text | same |
| `service_name` | text | same |
| `environment` | text | same |
| `app_version` | text | same |
| `release_id` | text | same |
| `route_template` | text | same |
| `http_method` | text | same |
| `trace_id` | text | same |
| `request_id` | text | same |
| `span_id` | text | same |
| `error_code` | text | same |
| `error_fingerprint` | text | same |
| `message` | text | same |
| `status_code` | int | `eq`, `neq`, `gt`, `gte`, `lt`, `lte`, `in` |
| `duration_ms` | int | same |

Sortable: `event_time`, `received_at`, `severity`, `duration_ms`, `status_code`.
`direction` is `asc` or `desc`. Anything else is refused.

Free-text `q` searches `message`, `error_code`, `error_fingerprint` and
`route_template` only. It stays inside the allowlist: the pattern is a bound
parameter and the columns are fixed.

`LIKE` wildcards in a value are escaped (`%`, `_`, `\`), so searching for `100%`
does not match everything.

## `GET /v1/query/log-events`

Named parameters, for shareable links and simple clients:

```
?app_id=&since=&until=&severity=&source=&event_type=&service_name=&environment=
&route_template=&app_version=&trace_id=&request_id=&error_fingerprint=
&status_code=&min_status=&max_status=&min_duration_ms=&q=
&sort=&direction=&limit=&cursor=
```

## `POST /v1/query/log-events`

The roadmap's validated JSON format. Unknown keys are refused (`extra="forbid"`),
because an unknown key is a typo or an injection attempt and either way must not
be ignored.

```json
{
  "app_id": "com.example.shop",
  "timeRange": { "from": "2026-10-22T00:00:00Z", "to": "2026-10-23T00:00:00Z" },
  "filters": [
    { "field": "severity", "operator": "eq", "value": "ERROR" },
    { "field": "status_code", "operator": "gte", "value": 500 }
  ],
  "sort": { "field": "event_time", "direction": "desc" },
  "limit": 100
}
```

## Cursor pagination

`LIMIT/OFFSET` re-scans everything before the offset and can skip or repeat rows
when new events land mid-pagination. A cursor over `(event_time, id)` does
neither: the next page is anchored to the last row of the current one.

The response carries `next_cursor` and `has_more`. Pass `cursor` back to continue.
`id` is the tiebreaker that makes the ordering total, which is what makes the
cursor exact.

## Query timeout

SQLite has no statement timeout, but it does let us install a progress handler
that is called every N virtual-machine steps, and returning non-zero aborts the
statement. That makes the budget a real timeout rather than a measurement after
the fact. The default is 5 s; overrunning it is a `400` telling the caller to
narrow the range.

The handler is always cleared afterwards, because the connection is per-thread
and reused — leaving one installed would abort unrelated later queries.

Ranges are capped at 92 days.

## `GET /v1/query/log-events/stats`

Volume per bucket plus a severity breakdown for the same filter set.

```json
{
  "bucket": "hour",
  "buckets": [{ "bucket": "2026-10-22T09", "count": 412, "errors": 7 }],
  "by_severity": { "INFO": 3000, "WARN": 120, "ERROR": 37 },
  "total": 3157,
  "elapsed_ms": 4
}
```

`bucket` is `hour` or `day`.

## `GET /v1/query/log-events/{event_id}`

The full row, plus `related` — events sharing a trace id or an error
fingerprint. One error is rarely interesting on its own; the other rows on the
same trace usually explain it. `?related=false` skips the lookup.

This route is registered **after** `/stats` on purpose: FastAPI matches in
declaration order, so the other way round would swallow it.

## Saved searches

| Method | Path | Does |
|---|---|---|
| `GET` | `/v1/saved-queries?app_id=` | List |
| `POST` | `/v1/saved-queries` | Create or replace by name |
| `GET` | `/v1/saved-queries/{id}` | Fetch one |
| `DELETE` | `/v1/saved-queries/{id}` | Delete |

Admin-guarded: a saved search is console configuration, not client data. A read
key cannot reach them.

A definition is **validated on the way in and again on the way out**. On the way
in, so the console cannot save a search that will not run. On the way out, so a
definition written by an older version cannot become a way around the allowlist.
A definition that no longer validates degrades to "no filters" rather than
breaking the page.

Definitions are stored in their canonical form — typed filter values — so a saved
search means the same thing every time it is read back.

## The console

`/explorer` — "Observability" in the nav, separate from the older "Log explorer"
which reads the analytics `logs` table.

- Selectors: severity, source, service, environment, route, trace id, minimum
  status, free-text search, range, bucket, sort, order, limit.
- Service and environment dropdowns are filled from what has actually been
  ingested, not from a hard-coded list.
- A histogram of volume over time, with error-bearing buckets in red.
- A results table; clicking a row opens a detail drawer with the full row, the
  surviving attributes, and the related events.
- Cursor pagination through a "Next page" button.
- Save the current filters under a name; load or delete a saved search from the
  same toolbar.
- A refused query says why, in place of the table. It does not look like an
  empty result.

## Not done yet

- `log_events` is still not covered by retention pruning or rollups. Step 3.
- The histogram is computed on every request rather than read from a rollup, so
  a very wide range over a large table is slower than it needs to be.
- No live tail yet.
- Saved searches are instance-scoped by project, with no per-user ownership
  until project roles land.
