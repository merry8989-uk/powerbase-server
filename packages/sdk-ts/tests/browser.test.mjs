/** The browser entry point: the transport, the global handlers, and the things
 *  it must never collect. */

import assert from "node:assert/strict";
import { test } from "node:test";

import { PowerbaseTelemetry, makeTransport, parseRetryAfter } from "../dist/browser.js";

/** A window just real enough for the SDK to attach to. */
function fakeWindow(href = "https://shop.example.com/checkout?token=secret#pay") {
  const listeners = new Map();
  return {
    location: { href },
    document: { readyState: "complete" },
    performance: { getEntriesByType: () => [] },
    addEventListener(type, listener) {
      if (!listeners.has(type)) listeners.set(type, []);
      listeners.get(type).push(listener);
    },
    removeEventListener(type, listener) {
      const list = listeners.get(type) ?? [];
      listeners.set(type, list.filter((l) => l !== listener));
    },
    /** Fire an event the way a browser would. */
    fire(type, event) {
      for (const listener of listeners.get(type) ?? []) listener(event);
    },
    listenerCount(type) {
      return (listeners.get(type) ?? []).length;
    },
  };
}

function makeSdk({ target = fakeWindow(), responses = [{ ok: true, status: 202 }], ...options } = {}) {
  const requests = [];
  let index = 0;
  const sdk = new PowerbaseTelemetry({
    endpoint: "https://logs.example.com",
    projectKey: "pb_public_key",
    appId: "com.example.shop",
    target,
    flushIntervalMs: 60_000,
    fetchImpl: async (url, init) => {
      requests.push({ url, init, body: JSON.parse(init.body) });
      const response = responses[Math.min(index, responses.length - 1)];
      index += 1;
      return {
        ok: response.ok,
        status: response.status,
        headers: { get: (name) => (name.toLowerCase() === "retry-after" ? response.retryAfter ?? null : null) },
      };
    },
    ...options,
  });
  return { sdk, requests, target };
}

// ------------------------------------------------------------- transport
test("a batch goes to the ingest endpoint with the project key", async () => {
  const { sdk, requests } = makeSdk();
  sdk.capture({ event_type: "checkout.submit" });
  await sdk.flush();

  assert.equal(requests.length, 1);
  const { url, init, body } = requests[0];
  assert.equal(url, "https://logs.example.com/v1/ingest/log-events");
  assert.equal(init.method, "POST");
  assert.equal(init.headers.Authorization, "Bearer pb_public_key");
  assert.equal(init.credentials, "omit", "no cookies travel with telemetry");
  assert.equal(body.app_id, "com.example.shop");
  assert.equal(body.events.length, 1);
  assert.ok(body.batch_id, "every batch is idempotent");
  await sdk.shutdown();
});

test("a trailing slash on the endpoint does not double up", async () => {
  const { sdk, requests } = makeSdk({ endpoint: "https://logs.example.com/" });
  sdk.capture({ event_type: "x" });
  await sdk.flush();
  assert.equal(requests[0].url, "https://logs.example.com/v1/ingest/log-events");
  await sdk.shutdown();
});

test("retry-after is understood in both forms the spec allows", () => {
  assert.equal(parseRetryAfter("120"), 120_000);
  assert.equal(parseRetryAfter(null), undefined);
  assert.equal(parseRetryAfter("nonsense"), undefined);
  const soon = new Date(Date.now() + 5_000).toUTCString();
  const parsed = parseRetryAfter(soon);
  assert.ok(parsed !== undefined && parsed > 0 && parsed <= 5_000);
});

// ------------------------------------------------------------- the window
test("an uncaught error is captured with its location", async () => {
  const { sdk, requests, target } = makeSdk();
  const error = new Error("undefined is not a function");

  target.fire("error", {
    error,
    message: error.message,
    filename: "https://shop.example.com/app.js?v=3",
    lineno: 42,
    colno: 7,
  });
  await sdk.flush();

  const event = requests[0].body.events[0];
  assert.equal(event.event_type, "error");
  assert.equal(event.severity, "ERROR");
  assert.ok(event.message.includes("undefined is not a function"));
  assert.equal(event.error_code, "Error");
  assert.ok(event.error_fingerprint);
  // The script URL is reduced the same way a page URL is.
  assert.equal(event.attributes.source_file, "https://shop.example.com/app.js");
  assert.equal(event.attributes.line, 42);
  await sdk.shutdown();
});

test("an unhandled rejection is captured", async () => {
  const { sdk, requests, target } = makeSdk();
  target.fire("unhandledrejection", { reason: new Error("promise said no") });
  await sdk.flush();

  const event = requests[0].body.events[0];
  assert.equal(event.event_type, "error");
  assert.equal(event.attributes.kind, "unhandledrejection");
  assert.ok(event.message.includes("promise said no"));
  await sdk.shutdown();
});

test("a rejected non-Error is still captured", async () => {
  const { sdk, requests, target } = makeSdk();
  target.fire("unhandledrejection", { reason: "just a string" });
  await sdk.flush();
  assert.ok(requests[0].body.events[0].message.includes("just a string"));
  await sdk.shutdown();
});

test("the handlers are removed on shutdown", async () => {
  const { sdk, target } = makeSdk();
  assert.equal(target.listenerCount("error"), 1);
  await sdk.shutdown();
  assert.equal(target.listenerCount("error"), 0);
  assert.equal(target.listenerCount("unhandledrejection"), 0);
});

test("global handlers can be turned off", async () => {
  const { sdk, target } = makeSdk({ captureUnhandledErrors: false });
  assert.equal(target.listenerCount("error"), 0);
  await sdk.shutdown();
});

// ------------------------------------------------ what it must not collect
test("the page route never carries the query string", async () => {
  const { sdk, requests } = makeSdk();
  sdk.capture({ event_type: "page.view" });
  await sdk.flush();

  const serialised = JSON.stringify(requests[0].body);
  assert.ok(!serialised.includes("token=secret"), "a query string is not telemetry");
  assert.ok(!serialised.includes("#pay"));
  assert.equal(requests[0].body.events[0].route_template, "https://shop.example.com/checkout");
  await sdk.shutdown();
});

test("a captured error records the route, not the URL", async () => {
  const { sdk, requests } = makeSdk();
  sdk.captureError(new Error("boom"), { operation: "checkout.submit" });
  await sdk.flush();

  const event = requests[0].body.events[0];
  assert.equal(event.attributes.route, "https://shop.example.com/checkout");
  assert.equal(event.attributes.operation, "checkout.submit");
  assert.ok(!JSON.stringify(event).includes("token=secret"));
  await sdk.shutdown();
});

test("no DOM, form or keystroke surface is touched", async () => {
  const { sdk, requests, target } = makeSdk();
  // If the SDK ever reached for the DOM, these would be the methods it used.
  let touched = false;
  for (const trap of ["querySelector", "querySelectorAll", "getElementById", "activeElement"]) {
    Object.defineProperty(target, trap, {
      get() {
        touched = true;
        return () => null;
      },
    });
  }
  Object.defineProperty(target, "document", {
    get() {
      return { readyState: "complete", cookie: (touched = true, "") };
    },
  });

  sdk.capture({ event_type: "x" });
  await sdk.flush();
  assert.equal(touched, false, "the SDK must not read the DOM");
  assert.equal(requests.length, 1);
  await sdk.shutdown();
});

// ---------------------------------------------------------------- disabled
test("a disabled browser SDK sends nothing, including uncaught errors", async () => {
  const { sdk, requests, target } = makeSdk({ enabled: false });

  assert.equal(sdk.capture({ event_type: "x" }), false);
  target.fire("error", { error: new Error("boom"), message: "boom" });
  target.fire("unhandledrejection", { reason: new Error("nope") });
  await sdk.flush();

  assert.equal(requests.length, 0);
  assert.equal(sdk.stats().queued, 0);
});

// ------------------------------------------------------------- navigation
test("navigation timing is captured only when asked for", async () => {
  const quiet = makeSdk();
  quiet.sdk.capture({ event_type: "only.this" });
  await quiet.sdk.flush();
  assert.equal(quiet.requests[0].body.events.length, 1);

  const target = fakeWindow();
  target.performance = {
    getEntriesByType: () => [
      { duration: 412.5, startTime: 0, loadEventEnd: 412.5, domContentLoadedEventEnd: 180, transferSize: 24_000, type: "navigate" },
    ],
  };
  const { sdk, requests } = makeSdk({ target, capturePerformance: true });
  await sdk.flush();

  const event = requests[0].body.events[0];
  assert.equal(event.event_type, "navigation");
  assert.equal(event.duration_ms, 413);
  assert.equal(event.attributes.dom_content_loaded_ms, 180);
  assert.equal(event.attributes.transfer_size, 24_000);
  await sdk.shutdown();
  await quiet.sdk.shutdown();
});

// ---------------------------------------------------------------- failures
test("a 401 from the server leaves the event queued rather than lost", async () => {
  const { sdk, requests } = makeSdk({ responses: [{ ok: false, status: 401 }] });
  sdk.capture({ event_type: "x" });
  await sdk.flush();

  assert.equal(requests.length, 1, "a 401 is not retried");
  assert.equal(sdk.stats().queued, 1, "and the event is not thrown away");
  sdk.setEnabled(false);
});

test("a network error surfaces as a failed send, not an exception", async () => {
  // The realistic case: fetch exists but the request never completes. The
  // transport must report it, because the core's retry path depends on a
  // result rather than on catching.
  const transport = makeTransport("https://logs.example.com", "pb_k", "app", async () => {
    throw new TypeError("Failed to fetch");
  });
  const result = await transport.send([], new AbortController().signal);
  assert.equal(result.ok, false);
  assert.equal(result.status, 0);
});
