# SDKs and OpenTelemetry (Step 4)

Three ways to get data in: a browser SDK, a Node/server SDK, and OTLP. All three
write to the same `log_events` table through the same ingest path, so they get
the same redaction, the same attribute rules and the same rate limits.

| | Package | Entry point |
|---|---|---|
| Browser | `powerbase-telemetry` | `powerbase-telemetry` |
| Node / server | `powerbase-telemetry` | `powerbase-telemetry/node` |
| Anything with an OTLP exporter | — | `POST /v1/otlp/v1/logs` |

## Install

```bash
npm install powerbase-telemetry
```

From this repository, build it first:

```bash
cd packages/sdk-ts
npm install
npm run build
npm test
```

---

## Browser

```ts
import { PowerbaseTelemetry } from "powerbase-telemetry";

const telemetry = new PowerbaseTelemetry({
  endpoint: "https://logs.example.com",
  projectKey: "pb_…",              // the ingest key — public by design
  appId: "com.example.shop",
  environment: "production",
  appVersion: "1.4.0",
  captureUnhandledErrors: true,
  capturePerformance: false,
});

telemetry.captureError(error, { operation: "checkout.submit", code: "CHECKOUT_FAILED" });
telemetry.capture({ event_type: "checkout.completed", attributes: { cart_size: 3 } });

telemetry.setEnabled(false);      // stops collecting and empties the queue
await telemetry.shutdown();       // flushes, then stops
```

### What it will not collect

These are not defaults you can turn on. The SDK has no code path for them:

- **DOM contents, form values, keystrokes.** It never reads the document.
- **Full URLs.** Only origin and path: `https://shop.example.com/checkout`, never
  `?token=…`. Script URLs from stack traces are treated the same way.
- **Request or response bodies.** `fetch` and `XMLHttpRequest` are not wrapped.
- **Cookies.** The request goes out with `credentials: "omit"`.

### Turning it off

`setEnabled(false)` empties the queue and stops the timer. Anything already
captured is discarded rather than sent later — that is what "stop collecting"
has to mean. There is a test asserting nothing is sent, including uncaught
errors, while it is off.

---

## Node / server

```ts
import { PowerbaseNode, traceMiddleware, handleRequest, sqlFingerprint } from "powerbase-telemetry/node";

const telemetry = new PowerbaseNode({
  endpoint: "https://logs.example.com",
  projectKey: process.env.POWERBASE_KEY!,   // secret: never in a client bundle
  appId: "com.acme.api",
  serviceName: "checkout-api",
  environment: process.env.NODE_ENV ?? "development",
});

// Express
app.use(traceMiddleware(telemetry));

// Or, with no framework
await handleRequest(telemetry, { method: "GET", originalUrl: req.url, headers: req.headers }, () =>
  doTheWork(),
);
```

### Correlation

`traceMiddleware` reads a W3C `traceparent` header, or generates a context when
there is none, and puts it in an `AsyncLocalStorage`. Every event captured inside
the request — however deep — carries its `trace_id`, `span_id` and `request_id`
without a single call site passing them along.

The `route_template` is a template, never a concrete path: `/orders/8842?token=…`
becomes `/orders/:id`. Concrete paths carry ids and query strings, which is
exactly what should not be indexed.

### Headers

`redactHeaders` replaces `authorization`, `cookie`, `set-cookie`, `x-api-key`,
`x-csrf-token` and friends with `[REDACTED]` — it keeps the header name so you
can still see that one was present.

### SQL

```ts
telemetry.captureQuery({ sql: "SELECT * FROM users WHERE email = 'a@b.co'", duration_ms: 1240 });
```

Only the shape is sent:

```
db.query  statement: "SELECT * FROM users WHERE email = ?"  fingerprint: 1a2b3c4d
```

The statement itself is never recorded. `sqlFingerprint(sql)` gives you the same
transformation if you want to log it yourself.

### Requests

`captureRequest` records method, route template, status and duration — and no
body. Status decides the severity: 5xx is `ERROR`, 4xx is `WARN`, otherwise
`INFO`.

---

## OpenTelemetry

Any OTLP exporter can point at Powerbase. Configure the collector's base
endpoint at `/v1/otlp`:

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT="https://logs.example.com/v1/otlp"
export OTEL_EXPORTER_OTLP_HEADERS="authorization=Bearer pb_…"
export OTEL_EXPORTER_OTLP_PROTOCOL="http/json"     # protobuf is not implemented
```

The project comes from the key. A key is bound to one project, so an exporter
does not have to be told which one — and cannot write to another.

### What maps to what

| OTLP | Powerbase |
|---|---|
| `resource.attributes["service.name"]` | `service_name` |
| `resource.attributes["service.version"]` | `app_version` |
| `resource.attributes["deployment.environment"]` | `environment` |
| `severityText` (if it is one of ours), else the `severityNumber` band | `severity` |
| `body` | `message` |
| `attributes["http.route"]` | `route_template` |
| `attributes["http.method"]` | `http_method` (upper-cased) |
| `attributes["http.status_code"]` | `status_code` |
| `attributes["error.type"]` | `error_code` |
| `traceId` / `spanId` | `trace_id` / `span_id` |
| `timeUnixNano` | `event_time` |
| everything else | `attributes` |

### Two things worth knowing

**`db.statement` is never stored as written.** OTLP carries it as an attribute,
and a statement with literals in it is exactly what must not be indexed. It is
reduced to its shape first and stored as `db.statement_shape`. The same applies
to `db.query.text` and `db.query`.

**Retries are idempotent.** OTLP exporters retry, and a retried export must not
double-write. The batch id is derived from the payload *and the project*, and
each event id from the record's own `traceId`/`spanId`, so a retry is a no-op at
both levels.

### Partial success

A malformed record does not cost the exporter the other nine hundred. Bad
records are counted and the rest are stored:

```json
{"partialSuccess": {"rejectedLogRecords": 1}}
```

### Protobuf

Not implemented. A request with `Content-Type: application/protobuf` is refused
with `415` and a message telling you to set
`OTEL_EXPORTER_OTLP_PROTOCOL=http/json`. Refusing is better than mis-parsing.

---

## Reliability, in both SDKs

| Behaviour | Detail |
|---|---|
| **Batching** | by size and on an interval |
| **Bounded queue** | oldest dropped first, and the drops are counted in `stats()` |
| **Exponential backoff with full jitter** | so a fleet of clients does not retry in lockstep |
| **`Retry-After` honoured as asked** | capped only by a 5-minute ceiling, not by our own backoff |
| **4xx other than 429 is not retried** | a malformed batch is our fault, not the network's |
| **Circuit breaker** | opens after repeated failures, half-opens after a cooldown |
| **Recursion guard** | a transport that throws while sending cannot spiral |
| **Event ids** | every event carries one, so a retry deduplicates |
| **Size caps** | message and attributes bounded before they are queued |
| **Disabled sends nothing** | tested explicitly |

Two behaviours are worth calling out because they are easy to get wrong:

- **A failed batch stays queued.** It is not dropped and not retried in a tight
  loop. The next flush picks it up.
- **`Retry-After` is not clamped to our own backoff ceiling.** `maxBackoffMs` is
  tuned for our retry schedule; applying it to the server's instruction would
  mean retrying far sooner than we were told, which turns a soft rate limit into
  a hard one.

## Settings

| Option | Default | Meaning |
|---|---|---|
| `maxQueueSize` | 500 | Events held before the oldest are dropped |
| `maxBatchSize` | 50 | Events per request |
| `flushIntervalMs` | 5000 | How often a flush is attempted |
| `maxRetries` | 5 | Attempts per batch |
| `baseBackoffMs` / `maxBackoffMs` | 500 / 30000 | Jittered backoff range |
| `circuitBreakerThreshold` | 5 | Consecutive failures before the breaker opens |
| `circuitBreakerCooldownMs` | 60000 | How long it stays open |
| `requestTimeoutMs` | 15000 | Per-request timeout |

## Not done yet

- **No Kotlin changes.** The Android SDK already exists and already speaks the
  ingest API; nothing in this step changed it.
- **OTLP metrics and traces are not ingested.** Logs only. `resourceMetrics` and
  `resourceSpans` are ignored rather than half-parsed.
- **Protobuf is not supported** — JSON only.
- **The package is not published to npm.** It builds and tests from this
  repository; publishing is a separate decision.
