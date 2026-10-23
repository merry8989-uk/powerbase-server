/** The server-side entry point: correlation, header redaction, SQL shapes. */

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  PowerbaseNode,
  contextFor,
  formatTraceparent,
  handleRequest,
  parseTraceparent,
  redactHeaders,
  routeOf,
  sqlFingerprint,
  traceMiddleware,
  withTraceContext,
  currentTrace,
} from "../dist/node.js";

const ok = { ok: true, status: 202 };

function makeNode(overrides = {}) {
  const captured = [];
  const node = new PowerbaseNode({
    endpoint: "https://logs.example.com",
    projectKey: "pb_server_key",
    appId: "com.example.api",
    flushIntervalMs: 60_000,
    fetchImpl: async () => {
      throw new Error("no network in tests");
    },
    ...overrides,
  });
  // Replace the transport so nothing leaves the process.
  node.core.options.transport = {
    async send(events) {
      captured.push(...events);
      return ok;
    },
  };
  return { node, captured };
}

// ------------------------------------------------------------ trace context
test("a well-formed traceparent is parsed", () => {
  const parsed = parseTraceparent("00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01");
  assert.deepEqual(parsed, {
    trace_id: "4bf92f3577b34da6a3ce929d0e0e4736",
    span_id: "00f067aa0ba902b7",
  });
});

test("a malformed traceparent is refused rather than guessed at", () => {
  for (const bad of [
    undefined, null, "", "not-a-traceparent",
    "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7",       // short
    "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01-extra",
    "00-00000000000000000000000000000000-00f067aa0ba902b7-01",   // all-zero trace id
  ]) {
    assert.equal(parseTraceparent(bad), null, `${bad} should be refused`);
  }
});

test("a trace context round-trips", () => {
  const trace = { trace_id: "4bf92f3577b34da6a3ce929d0e0e4736", span_id: "00f067aa0ba902b7" };
  assert.equal(
    formatTraceparent(trace),
    "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
  );
  assert.equal(formatTraceparent({}), undefined);
});

test("the context prefers an upstream traceparent and falls back to a request id", () => {
  const context = contextFor({
    method: "POST",
    originalUrl: "/orders/8842?token=secret",
    headers: {
      traceparent: "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
      "x-request-id": "req-abc",
    },
  });
  assert.equal(context.trace_id, "4bf92f3577b34da6a3ce929d0e0e4736");
  assert.equal(context.request_id, "req-abc");
  assert.equal(context.route, "/orders/:id", "the concrete id must not be kept");
});

test("the ambient context is available without threading it through", () => {
  withTraceContext(
    { headers: { traceparent: "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01" } },
    () => {
      assert.equal(currentTrace().trace_id, "4bf92f3577b34da6a3ce929d0e0e4736");
    },
  );
  assert.deepEqual(currentTrace(), {}, "and it is gone afterwards");
});

test("events captured inside a request carry its trace", async () => {
  const { node, captured } = makeNode();

  await withTraceContext(
    {
      headers: {
        traceparent: "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
        "x-request-id": "req-1",
      },
      originalUrl: "/orders/8842",
    },
    async () => {
      node.capture({ event_type: "checkout.submit" });
      await node.flush();
    },
  );

  assert.equal(captured.length, 1);
  assert.equal(captured[0].trace_id, "4bf92f3577b34da6a3ce929d0e0e4736");
  assert.equal(captured[0].request_id, "req-1");
  assert.equal(captured[0].route_template, "/orders/:id");
  assert.equal(captured[0].source, "server_sdk");
  await node.shutdown();
});

// ---------------------------------------------------------------- routes
test("a route template keeps its shape and loses its identifiers", () => {
  assert.equal(routeOf({ originalUrl: "/orders/8842/items/9?debug=1" }), "/orders/:id/items/:id");
  assert.equal(routeOf({ url: "/health" }), "/health");
  assert.equal(routeOf({ route: { path: "/orders/:id" }, originalUrl: "/orders/1" }), "/orders/:id");
  assert.equal(
    routeOf({ originalUrl: "/sessions/4bf92f35-77b3-4da6-a3ce-929d0e0e4736" }),
    "/sessions/:id",
  );
  assert.equal(routeOf({ originalUrl: "/a/verylongtokenvalue1234567890" }), "/a/:id");
  assert.equal(routeOf({}), undefined);
});

// --------------------------------------------------------------- headers
test("credential-bearing headers are replaced, not removed", () => {
  const headers = redactHeaders({
    authorization: "Bearer secret-token",
    cookie: "session=abc",
    "set-cookie": "session=abc; HttpOnly",
    "x-api-key": "sk-live-1",
    "content-type": "application/json",
    "x-request-id": "req-1",
  });

  assert.equal(headers.authorization, "[REDACTED]");
  assert.equal(headers.cookie, "[REDACTED]");
  assert.equal(headers["set-cookie"], "[REDACTED]");
  assert.equal(headers["x-api-key"], "[REDACTED]");
  assert.equal(headers["content-type"], "application/json");
  assert.equal(headers["x-request-id"], "req-1");
  assert.ok(!JSON.stringify(headers).includes("secret-token"));
});

test("redactHeaders copes with arrays and undefined", () => {
  const headers = redactHeaders({ "accept-encoding": ["gzip", "br"], "x-gone": undefined });
  assert.equal(headers["accept-encoding"], "gzip, br");
  assert.equal("x-gone" in headers, false);
  assert.deepEqual(redactHeaders(undefined), {});
});

// ------------------------------------------------------------------- SQL
test("a SQL fingerprint keeps the shape and drops every literal", () => {
  const { template, fingerprint } = sqlFingerprint(
    "SELECT * FROM orders WHERE email = 'amit@example.com' AND id = 8842 LIMIT 20",
  );

  assert.ok(!template.includes("amit@example.com"), "the literal must be gone");
  assert.ok(!template.includes("8842"));
  assert.ok(!template.includes("20"));
  assert.match(template, /^SELECT \* FROM orders WHERE email = \? AND id = \? LIMIT \?$/);
  assert.match(fingerprint, /^[0-9a-f]{8}$/);
});

test("two queries of the same shape share a fingerprint", () => {
  const a = sqlFingerprint("SELECT * FROM orders WHERE id = 1");
  const b = sqlFingerprint("SELECT * FROM orders WHERE id = 99999");
  assert.equal(a.fingerprint, b.fingerprint);
});

test("a query is recorded by shape, never as a statement", async () => {
  const { node, captured } = makeNode();
  node.captureQuery({
    sql: "SELECT * FROM users WHERE email = 'amit@example.com'",
    duration_ms: 1240,
  });
  await node.flush();

  const serialised = JSON.stringify(captured[0]);
  assert.ok(!serialised.includes("amit@example.com"));
  assert.equal(captured[0].event_type, "db.query");
  assert.equal(captured[0].duration_ms, 1240);
  assert.match(captured[0].attributes.statement, /email = \?/);
  await node.shutdown();
});

// -------------------------------------------------------------- requests
test("a request is recorded with its status and duration, and no body", async () => {
  const { node, captured } = makeNode();
  node.captureRequest({
    method: "post",
    route_template: "/orders/:id",
    status_code: 500,
    duration_ms: 812.4,
  });
  await node.flush();

  assert.equal(captured[0].event_type, "http.request");
  assert.equal(captured[0].severity, "ERROR");
  assert.equal(captured[0].http_method, "POST");
  assert.equal(captured[0].status_code, 500);
  assert.equal(captured[0].duration_ms, 812);
  assert.equal(captured[0].error_code, "HTTP_500");
  assert.equal(captured[0].message, undefined, "a request record carries no body");
  await node.shutdown();
});

test("status decides the severity", async () => {
  const { node, captured } = makeNode();
  for (const [status, expected] of [[200, "INFO"], [404, "WARN"], [503, "ERROR"]]) {
    node.captureRequest({ method: "GET", route_template: "/x", status_code: status, duration_ms: 1 });
  }
  await node.flush();
  assert.deepEqual(captured.map((e) => e.severity), ["INFO", "WARN", "ERROR"]);
  await node.shutdown();
});

// ------------------------------------------------------------- middleware
test("the express middleware records on finish, inside the request's trace", async () => {
  const { node, captured } = makeNode();
  const listeners = {};
  const request = {
    method: "GET",
    originalUrl: "/orders/42",
    headers: { "x-request-id": "req-7" },
  };
  const response = {
    statusCode: 404,
    on(event, listener) {
      listeners[event] = listener;
    },
  };

  let nextCalled = false;
  traceMiddleware(node)(request, response, () => {
    nextCalled = true;
    // A call site deep inside the handler still gets the trace.
    node.capture({ event_type: "inside.handler" });
  });

  assert.equal(nextCalled, true);
  listeners.finish();
  await node.flush();

  const byType = Object.fromEntries(captured.map((e) => [e.event_type, e]));
  assert.equal(byType["inside.handler"].request_id, "req-7");
  assert.equal(byType["http.request"].status_code, 404);
  assert.equal(byType["http.request"].route_template, "/orders/:id");
  await node.shutdown();
});

test("the bare handler wrapper records success and failure", async () => {
  const { node, captured } = makeNode();

  await handleRequest(node, { method: "GET", originalUrl: "/ok" }, async () => "done");
  await assert.rejects(
    handleRequest(node, { method: "GET", originalUrl: "/bad" }, async () => {
      throw new Error("kaboom");
    }),
  );
  await node.flush();

  const types = captured.map((e) => e.event_type);
  assert.ok(types.includes("http.request"));
  assert.ok(types.includes("error"));
  const error = captured.find((e) => e.event_type === "error");
  assert.ok(error.message.includes("kaboom"));
  assert.equal(error.route_template, "/ok".slice(0, 3) === "/ok" ? error.route_template : "/bad");
  await node.shutdown();
});

// --------------------------------------------------------------- disabled
test("a disabled server SDK sends nothing", async () => {
  const { node, captured } = makeNode({ enabled: false });
  assert.equal(node.capture({ event_type: "x" }), false);
  assert.equal(node.captureError(new Error("nope")), false);
  await node.flush();
  assert.equal(captured.length, 0);
});
