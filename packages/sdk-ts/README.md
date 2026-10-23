# powerbase-telemetry

Telemetry SDK for browsers and Node, for a self-hosted [Powerbase](../) server.

Batches events, logs and errors to your own server. Zero runtime dependencies —
`fetch` and nothing else.

```bash
npm install powerbase-telemetry
```

## Browser

```ts
import { PowerbaseTelemetry } from "powerbase-telemetry";

const telemetry = new PowerbaseTelemetry({
  endpoint: "https://logs.example.com",
  projectKey: "pb_…",
  appId: "com.example.shop",
  environment: "production",
  captureUnhandledErrors: true,
});

telemetry.captureError(error, { operation: "checkout.submit" });
telemetry.capture({ event_type: "checkout.completed", attributes: { cart_size: 3 } });
```

Captures uncaught errors and unhandled rejections by default. **Does not** read
the DOM, form values, keystrokes, request bodies, cookies, or full URLs — only
origin and path, never the query string. There is no code path for them.

## Node

```ts
import { PowerbaseNode, traceMiddleware } from "powerbase-telemetry/node";

const telemetry = new PowerbaseNode({
  endpoint: "https://logs.example.com",
  projectKey: process.env.POWERBASE_KEY!,
  appId: "com.acme.api",
  serviceName: "checkout-api",
});

app.use(traceMiddleware(telemetry));       // Express
telemetry.captureQuery({ sql, duration_ms: 1240 });   // shape only, never the statement
```

Carries W3C trace context and request ids into every event through
`AsyncLocalStorage`, so call sites do not have to pass them along. Route
templates, not concrete paths. Credential headers replaced before anything is
recorded.

## Reliability

Bounded queue, batching by size and interval, exponential backoff with full
jitter, `Retry-After` honoured as asked, no retry on 4xx other than 429, a
circuit breaker, a recursion guard, and event ids for deduplication. A failed
batch stays queued rather than being dropped.

`setEnabled(false)` stops collecting **and empties the queue**. Disabled sends
nothing — there is a test for it.

## Scripts

```bash
npm run build     # tsc -> dist/
npm test          # node --test, against the compiled output
```

## Licence

MIT.
