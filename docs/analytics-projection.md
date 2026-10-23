# Analytics and crashes, in the diagnostics log

Powerbase grew two data models that did not know about each other.

| | tables | built for |
|---|---|---|
| **analytics** | `events`, `logs`, `crashes`, `sessions`, `devices` | the mobile SDK, and the dashboard pages that came with it |
| **diagnostics** | `log_events`, `spans`, `metric_points` | the Logs Explorer, traces and RED metrics |

The consequence was a hole: **a crash, or a product analytics event, did not
appear in the Logs Explorer at all.** Answering "what happened in this session"
meant looking in two places, and the screen built for exactly that question
could not answer it.

A crash *is* a diagnostic event. So is an analytics event, at the level of
detail anyone needs during an incident. They are now projected into
`log_events`.

## What arrives

| Source | `event_type` | `severity` | Notes |
|---|---|---|---|
| `crashes` | `crash` | `FATAL` unhandled, `ERROR` handled | fingerprint reused from the crash row; the first stack frame and the device and session ids travel as attributes |
| `events` | `analytics.event` | `INFO` | the event name becomes the message; `screen` becomes `route_template`; `properties` become attributes |
| `logs` | `analytics.log` | the log's own level | `tag` and `context` travel as attributes |

An unhandled crash is what the user saw. A handled one was caught and the app
carried on. They are not the same thing and do not share a colour.

## It is a projection, not a move

- The **source table stays the source of truth** for its own dashboard page. The
  crashes page, the data page and the sessions page read what they always read.
- **No client changes.** The SDKs keep posting to the endpoints they post to.
- The `log_events` row is written **in the same transaction** as the source row,
  so the two cannot diverge through a crash or a rollback: either both land or
  neither does. A test asserts this by failing the projection and checking that
  the source row is gone too.

## The cost, stated plainly

**Each crash and each analytics event is now stored twice.** On a self-hosted
SQLite instance that is real: the row count for those paths doubles, and so does
the disk they take.

That is the price of making the explorer complete without breaking every
installed client. Consolidating the two models into one is a larger change, and
should be made deliberately rather than as a side effect of this one.

## Why not a view

A `UNION ALL` view over the three tables would avoid the duplication entirely,
and it breaks pagination. `log_events` paginates on `(event_time, id)` and relies
on `id` being unique; ids collide across tables, so a union would need a
synthetic key and the keyset cursor would stop being exact. Duplication is the
cheaper mistake.

## Idempotency

The projected `event_id` is derived — `crash:<app_id>:<row id>` — not random.
The batch ledger already refuses a replayed batch, so the projection does not
run twice; the derived id is the second line of defence, and it means a
collision would be a bug rather than a coin flip. The insert is a plain `INSERT`,
not `OR IGNORE`, so a collision fails loudly instead of being swallowed.

## Redaction

The projection takes the **already-redacted** message, stack trace and
properties from the writer, and the already-computed fingerprint. Redacting
again would be harmless and pointless; using the raw values would put in the
explorer exactly what the crash page hides.

## A known limitation

Projected rows are tagged `source = android_sdk`, because the analytics
endpoints are the mobile SDK's contract — `sdk_version` on those payloads is the
Android SDK's. A web client posting to the same endpoints would be mislabelled.
Fixing that properly means adding a value to the `source` vocabulary, which is a
table rebuild; it is not worth one today, and it is written down here so it is
not a surprise.

## Not projected

`sessions` and `devices` are not diagnostics logs in the same sense — they are
dimensions, not events. A session's start and end are already visible through
the events and crashes that reference it.
