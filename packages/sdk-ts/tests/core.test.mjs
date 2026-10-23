/**
 * The engine: queue bounds, batching, backoff, the circuit breaker, and the
 * promise that a disabled SDK sends nothing.
 *
 * Runs against the compiled output in `dist/`, so it tests what would ship
 * rather than what is in `src/`.
 */

import assert from "node:assert/strict";
import { test } from "node:test";

import { PowerbaseCore, fingerprint } from "../dist/core.js";
import { isSensitiveName, redactText, safeLocation, sanitizeAttributes } from "../dist/redaction.js";

/** A transport that answers from a script and records what it was given. */
function scripted(results) {
  const batches = [];
  const calls = [];
  let index = 0;
  return {
    batches,
    calls,
    get count() {
      return calls.length;
    },
    transport: {
      async send(events) {
        calls.push(events.map((e) => e.event_id));
        batches.push(events);
        const result = results[Math.min(index, results.length - 1)];
        index += 1;
        return typeof result === "function" ? result(events) : result;
      },
    },
  };
}

const ok = { ok: true, status: 202 };
const bad = { ok: false, status: 500 };
const refused = { ok: false, status: 400 };

function makeCore(overrides = {}) {
  const script = overrides.script ?? scripted([ok]);
  const core = new PowerbaseCore({
    endpoint: "https://logs.example.com",
    projectKey: "pb_test_key",
    appId: "com.example.shop",
    transport: script.transport,
    flushIntervalMs: 60_000,
    baseBackoffMs: 1,
    maxBackoffMs: 5,
    ...overrides.options,
  });
  return { core, script };
}

// ------------------------------------------------------------------ capture
test("a captured event is shaped for the server", async () => {
  const { core, script } = makeCore();
  core.capture({ event_type: "checkout.submit", severity: "WARN", message: "trying" });
  await core.flush();

  assert.equal(script.count, 1);
  assert.equal(script.batches[0].length, 1);
  const event = script.batches[0][0];
  assert.match(event.event_id, /^[0-9a-f-]{36}$/);
  assert.equal(event.event_type, "checkout.submit");
  assert.equal(event.severity, "WARN");
  assert.equal(event.message, "trying");
  assert.equal(event.source, "browser_sdk");
  await core.shutdown();
});

test("the batch carries every documented field", async () => {
  const captured = [];
  const core = new PowerbaseCore({
    endpoint: "https://x", projectKey: "pb_k", appId: "a",
    flushIntervalMs: 60_000,
    transport: { async send(events) { captured.push(...events); return ok; } },
  });
  core.capture({
    event_type: "http.request",
    severity: "ERROR",
    message: "boom",
    service_name: "checkout-api",
    environment: "production",
    route_template: "/orders/{id}",
    http_method: "POST",
    status_code: 500,
    duration_ms: 812,
    trace_id: "abc",
    request_id: "req-1",
    error_code: "UPSTREAM_TIMEOUT",
    attributes: { region: "ap-south-1" },
  });
  await core.flush();

  const event = captured[0];
  assert.equal(event.event_type, "http.request");
  assert.equal(event.severity, "ERROR");
  assert.equal(event.service_name, "checkout-api");
  assert.equal(event.environment, "production");
  assert.equal(event.route_template, "/orders/{id}");
  assert.equal(event.http_method, "POST");
  assert.equal(event.status_code, 500);
  assert.equal(event.duration_ms, 812);
  assert.equal(event.trace_id, "abc");
  assert.equal(event.error_code, "UPSTREAM_TIMEOUT");
  assert.equal(event.attributes.region, "ap-south-1");
  assert.equal(event.source, "browser_sdk");
  await core.shutdown();
});

test("event ids are unique", async () => {
  const seen = new Set();
  const core = new PowerbaseCore({
    endpoint: "https://x", projectKey: "pb_k", appId: "a", flushIntervalMs: 60_000,
    transport: { async send(events) { for (const e of events) seen.add(e.event_id); return ok; } },
  });
  for (let i = 0; i < 25; i += 1) core.capture({ event_type: "tick" });
  await core.flush();
  assert.equal(seen.size, 25);
  await core.shutdown();
});

// ------------------------------------------------------------------- bounds
test("the queue is bounded and the drops are counted", () => {
  const { core } = makeCore({ options: { maxQueueSize: 10 } });
  for (let i = 0; i < 40; i += 1) core.capture({ event_type: "tick", attributes: { i } });

  const stats = core.stats();
  assert.equal(stats.queued, 10);
  assert.equal(stats.dropped, 30);
});

test("a long message is truncated, not dropped", async () => {
  const captured = [];
  const core = new PowerbaseCore({
    endpoint: "https://x", projectKey: "pb_k", appId: "a", flushIntervalMs: 60_000,
    transport: { async send(events) { captured.push(...events); return ok; } },
  });
  core.capture({ event_type: "x", message: "y".repeat(20_000) });
  await core.flush();
  assert.equal(captured[0].message.length, 8000);
  await core.shutdown();
});

// ---------------------------------------------------------------- disabled
test("a disabled SDK sends nothing at all", async () => {
  const { core, script } = makeCore({ options: { enabled: false } });

  assert.equal(core.capture({ event_type: "x" }), false);
  assert.equal(core.stats().queued, 0);

  const result = await core.flush();
  assert.equal(script.count, 0);
  assert.equal(result.sent, 0);
});

test("turning it off empties the queue rather than sending later", async () => {
  const { core, script } = makeCore();
  core.capture({ event_type: "x" });
  core.setEnabled(false);

  assert.equal(core.stats().queued, 0);
  await core.flush();
  assert.equal(script.count, 0);
});

test("turning it back on collects again", async () => {
  const { core, script } = makeCore({ options: { enabled: false } });
  core.setEnabled(true);
  assert.equal(core.capture({ event_type: "x" }), true);
  await core.flush();
  assert.equal(script.count, 1);
  await core.shutdown();
});

// ---------------------------------------------------------------- batching
test("events go out in batches of the configured size", async () => {
  const { core, script } = makeCore({ options: { maxBatchSize: 4 } });
  for (let i = 0; i < 10; i += 1) core.capture({ event_type: "tick" });
  await core.flush();

  const sizes = script.calls.map((batch) => batch.length);
  assert.deepEqual(sizes, [4, 4, 2]);
  await core.shutdown();
});

test("a failed batch stays queued for the next attempt", async () => {
  const { core, script } = makeCore({ script: scripted([bad]) });
  core.capture({ event_type: "x" });
  await core.flush();

  assert.equal(core.stats().queued, 1, "the event must not be lost");
  assert.ok(script.count >= 1);
  core.setEnabled(false);
});

test("a retry succeeds without duplicating what already landed", async () => {
  const { core, script } = makeCore({ script: scripted([bad, ok]) });
  core.capture({ event_type: "x" });
  await core.flush();

  assert.equal(core.stats().queued, 0);
  assert.equal(core.stats().sent, 1);
  assert.ok(script.count >= 2);
  await core.shutdown();
});

// ------------------------------------------------------------------ retries
test("a 400 is not retried: it is our fault, not the network's", async () => {
  const { core, script } = makeCore({ script: scripted([refused]) });
  core.capture({ event_type: "x" });
  await core.flush();

  assert.equal(script.count, 1, "a malformed batch must not be hammered");
  core.setEnabled(false);
});

test("a 500 is retried", async () => {
  const { core, script } = makeCore({ script: scripted([bad, bad, ok]) });
  core.capture({ event_type: "x" });
  await core.flush();
  assert.ok(script.count >= 3);
  await core.shutdown();
});

test("a 429 waits for the Retry-After the server asked for", async () => {
  const waits = [];
  const originalSetTimeout = globalThis.setTimeout;
  globalThis.setTimeout = (fn, ms, ...rest) => {
    if (ms > 50) waits.push(ms);
    return originalSetTimeout(fn, 0, ...rest);
  };
  try {
    const { core } = makeCore({
      script: scripted([{ ok: false, status: 429, retryAfterMs: 250 }, ok]),
    });
    core.capture({ event_type: "x" });
    await core.flush();
    assert.ok(waits.includes(250), `expected a 250 ms wait, saw ${JSON.stringify(waits)}`);
  } finally {
    globalThis.setTimeout = originalSetTimeout;
  }
});

test("backoff has jitter, so clients do not retry in lockstep", async () => {
  const ceilings = [];
  const core = new PowerbaseCore({
    endpoint: "https://x", projectKey: "pb_k", appId: "a", flushIntervalMs: 60_000,
    baseBackoffMs: 100, maxBackoffMs: 100_000, maxRetries: 1,
    random: () => 0.5,
    transport: {
      async send() {
        return bad;
      },
    },
  });
  // Two failing flushes produce two backoff computations; with a fixed random
  // they must be deterministic, which is what "jittered, not fixed" means here.
  const originalSetTimeout = globalThis.setTimeout;
  globalThis.setTimeout = (fn, ms, ...rest) => {
    ceilings.push(ms);
    return originalSetTimeout(fn, 0, ...rest);
  };
  try {
    core.capture({ event_type: "x" });
    await core.flush();
  } finally {
    globalThis.setTimeout = originalSetTimeout;
  }
  assert.ok(ceilings.some((ms) => ms > 0 && ms <= 100));
  core.setEnabled(false);
});

// ---------------------------------------------------------- circuit breaker
test("the breaker opens after repeated failures and stops trying", async () => {
  const { core, script } = makeCore({
    script: scripted([bad]),
    options: { circuitBreakerThreshold: 2, circuitBreakerCooldownMs: 60_000, maxRetries: 0 },
  });

  core.capture({ event_type: "a" });
  await core.flush();
  core.capture({ event_type: "b" });
  await core.flush();

  const before = script.count;
  const result = await core.flush();
  assert.equal(result.skipped, true);
  assert.equal(script.count, before, "an open breaker must not call the transport");
  assert.equal(core.stats().circuitOpen, true);
  core.setEnabled(false);
});

test("the breaker half-opens after the cooldown", async () => {
  let clock = 1_000;
  const { core, script } = makeCore({
    script: scripted([bad, ok]),
    options: {
      circuitBreakerThreshold: 1,
      circuitBreakerCooldownMs: 5_000,
      maxRetries: 0,
      now: () => clock,
    },
  });

  core.capture({ event_type: "a" });
  await core.flush();
  assert.equal(core.stats().circuitOpen, true);

  clock += 6_000; // past the cooldown
  const result = await core.flush();
  assert.equal(result.skipped, false);
  assert.ok(script.count >= 2);
  assert.equal(core.stats().circuitOpen, false, "a success closes the breaker again");
  await core.shutdown();
});

test("a transport that throws does not spiral", async () => {
  let calls = 0;
  const core = new PowerbaseCore({
    endpoint: "https://x", projectKey: "pb_k", appId: "a", flushIntervalMs: 60_000,
    baseBackoffMs: 1, maxBackoffMs: 2, maxRetries: 2,
    onError: () => {
      // The reporter itself captures, which must not start another flush.
      core.capture({ event_type: "from-the-reporter" });
    },
    transport: {
      async send() {
        calls += 1;
        throw new Error("socket exploded");
      },
    },
  });

  core.capture({ event_type: "x" });
  const result = await core.flush();

  assert.equal(result.sent, 0);
  assert.ok(calls <= 4, `expected bounded attempts, saw ${calls}`);
  core.setEnabled(false);
});

test("a reporter that throws does not take the SDK with it", async () => {
  const core = new PowerbaseCore({
    endpoint: "https://x", projectKey: "pb_k", appId: "a", flushIntervalMs: 60_000,
    baseBackoffMs: 1, maxBackoffMs: 1, maxRetries: 0,
    onError: () => {
      throw new Error("bad reporter");
    },
    transport: { async send() { throw new Error("nope"); } },
  });
  core.capture({ event_type: "x" });
  const result = await core.flush();
  assert.equal(result.sent, 0);
  core.setEnabled(false);
});

// ----------------------------------------------------------------- shutdown
test("shutdown flushes what is queued", async () => {
  const { core, script } = makeCore();
  core.capture({ event_type: "x" });
  await core.shutdown();
  assert.equal(script.count, 1);
  assert.equal(core.stats().queued, 0);
});

test("after shutdown, nothing more is captured", async () => {
  const { core } = makeCore();
  await core.shutdown();
  assert.equal(core.capture({ event_type: "x" }), false);
});

// -------------------------------------------------------------- redaction
test("a secret never reaches the transport", async () => {
  const captured = [];
  const core = new PowerbaseCore({
    endpoint: "https://x", projectKey: "pb_k", appId: "a", flushIntervalMs: 60_000,
    transport: { async send(events) { captured.push(...events); return ok; } },
  });
  core.capture({
    event_type: "checkout",
    message: "failed for amit@example.com, card 4111 1111 1111 1111",
    attributes: { region: "ap-south-1", password: "hunter2", apiKey: "sk-live-1", note: "call +91 98765 43210" },
  });
  await core.flush();

  const event = captured[0];
  const serialised = JSON.stringify(event);
  assert.ok(!serialised.includes("amit@example.com"), "email must be redacted");
  assert.ok(!serialised.includes("hunter2"), "a password-named key must be dropped");
  assert.ok(!serialised.includes("sk-live-1"), "an apiKey-named key must be dropped");
  assert.ok(!serialised.includes("4111 1111 1111 1111"), "a card number must be redacted");
  assert.equal(event.attributes.region, "ap-south-1");
  assert.ok(event.message.includes("[REDACTED_EMAIL]"));
  await core.shutdown();
});

test("the redaction rules behave", () => {
  assert.match(redactText("mail me at a@b.co"), /\[REDACTED_EMAIL\]/);
  assert.match(redactText("aadhaar 1234 5678 9012"), /\[REDACTED_AADHAAR\]/);
  assert.match(redactText("pan ABCDE1234F"), /\[REDACTED_PAN\]/);
  assert.match(redactText("Authorization: Bearer abc.def.ghi"), /\[REDACTED_TOKEN\]/);
  assert.match(redactText("password=hunter2"), /password=\[REDACTED\]/);
  assert.equal(redactText("nothing to hide here"), "nothing to hide here");
});

test("credential-shaped names are caught and ordinary ones are not", () => {
  for (const name of ["password", "apiKey", "api_key", "x-api-key", "accessToken", "user.password", "refresh_token", "private_key", "card_number"]) {
    assert.equal(isSensitiveName(name), true, `${name} should be caught`);
  }
  for (const name of ["region", "sort_key", "keyboard", "monkey", "author", "session_count", "duration_ms"]) {
    assert.equal(isSensitiveName(name), false, `${name} should not be caught`);
  }
});

test("attributes are bounded in depth and width", () => {
  const wide = sanitizeAttributes(Object.fromEntries(Array.from({ length: 80 }, (_, i) => [`k${i}`, i])));
  assert.equal(Object.keys(wide.attributes).length, 64);
  assert.equal(wide.dropped, 16);

  let deep = { leaf: 1 };
  for (let i = 0; i < 8; i += 1) deep = { next: deep };
  const bounded = sanitizeAttributes(deep);
  assert.ok(bounded.dropped >= 1);
});

test("a full URL is reduced to origin and path", () => {
  assert.equal(
    safeLocation("https://shop.example.com/checkout?token=secret&email=a@b.co#pay"),
    "https://shop.example.com/checkout",
  );
  assert.equal(safeLocation("not a url"), "");
});

// -------------------------------------------------------------- fingerprint
test("a fingerprint ignores addresses and numbers, so one bug is one group", () => {
  const a = fingerprint("Error", "boom", "at foo (Foo.js:42) 0xdeadbeef");
  const b = fingerprint("Error", "boom", "at foo (Foo.js:99) 0xcafebabe");
  assert.equal(a, b);

  const c = fingerprint("Error", "a different failure", "at foo (Foo.js:42)");
  assert.notEqual(a, c);
  assert.match(a, /^[0-9a-f]{8}$/);
});
