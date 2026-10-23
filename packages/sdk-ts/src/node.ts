/**
 * Node / server entry point.
 *
 * Server telemetry is different from browser telemetry in three ways that
 * matter, and this entry point is shaped by them:
 *
 * 1. **Correlation.** A request has an id and, usually, a W3C trace context.
 *    Both are carried into every event so one request can be followed across
 *    services. `withTraceContext` and `traceMiddleware` do that with
 *    `AsyncLocalStorage`, so call sites do not have to thread ids around.
 * 2. **Secrets in headers.** `authorization`, `cookie` and friends are dropped
 *    by `redactHeaders` before anything is recorded.
 * 3. **SQL.** The SDK never logs a statement. `sqlFingerprint` produces a
 *    template with the literals removed, so a slow-query event groups by shape
 *    without carrying the customer's data.
 *
 * The server key stays in the environment. It is never in a client bundle.
 */

import { AsyncLocalStorage } from "node:async_hooks";

import {
  PowerbaseCore,
  fingerprint,
  type CoreOptions,
  type LogEvent,
  type LogEventInput,
  type Transport,
  type TransportResult,
} from "./core.js";
import { parseRetryAfter } from "./browser.js";

export interface NodeOptions extends Omit<CoreOptions, "transport" | "source"> {
  fetchImpl?: typeof fetch;
}

export interface TraceContext {
  trace_id?: string;
  span_id?: string;
  request_id?: string;
  route?: string;
}

const storage = new AsyncLocalStorage<TraceContext>();

/** The trace context of the request being handled, if there is one. */
export function currentTrace(): TraceContext {
  return storage.getStore() ?? {};
}

const TRACEPARENT = /^[\da-f]{2}-([\da-f]{32})-([\da-f]{16})-([\da-f]{2})$/i;

/** Parse a W3C `traceparent` header. Returns null when it is malformed. */
export function parseTraceparent(value: string | undefined | null): TraceContext | null {
  if (!value) return null;
  const match = TRACEPARENT.exec(value.trim());
  if (!match) return null;
  const [, traceId, spanId] = match;
  if (!traceId || !spanId) return null;
  // An all-zero trace id is explicitly invalid in the spec.
  if (/^0+$/.test(traceId)) return null;
  return { trace_id: traceId.toLowerCase(), span_id: spanId.toLowerCase() };
}

export function formatTraceparent(trace: TraceContext): string | undefined {
  if (!trace.trace_id || !trace.span_id) return undefined;
  return `00-${trace.trace_id}-${trace.span_id}-01`;
}

export interface HeaderLike {
  [key: string]: string | string[] | number | undefined;
}

/** Header names whose values never belong in a log line. */
const SENSITIVE_HEADERS = new Set([
  "authorization", "proxy-authorization", "cookie", "set-cookie",
  "x-api-key", "x-auth-token", "x-csrf-token", "x-xsrf-token",
  "x-amz-security-token", "x-goog-iap-jwt-assertion",
]);

export function redactHeaders(headers: HeaderLike | undefined): Record<string, string> {
  const out: Record<string, string> = {};
  if (!headers) return out;
  for (const [key, value] of Object.entries(headers)) {
    if (value === undefined) continue;
    const lowered = key.toLowerCase();
    out[lowered] = SENSITIVE_HEADERS.has(lowered)
      ? "[REDACTED]"
      : Array.isArray(value)
        ? value.join(", ")
        : String(value);
  }
  return out;
}

export interface SqlFingerprint {
  /** The statement with every literal replaced by `?`. */
  template: string;
  /** A short, stable handle for the shape. */
  fingerprint: string;
}

/**
 * Reduce a SQL statement to its shape.
 *
 * Never returns the original. A statement carrying an email address in a
 * literal is the reason this exists.
 */
export function sqlFingerprint(sql: string): SqlFingerprint {
  const template = sql
    .replace(/'(?:[^']|'')*'/g, "?")
    .replace(/"(?:[^"]|"")*"/g, "?")
    .replace(/\$\d+/g, "?")
    .replace(/\b\d+(?:\.\d+)?\b/g, "?")
    .replace(/\s+/g, " ")
    .trim();
  return { template, fingerprint: fingerprint("sql", template) };
}

export function makeTransport(
  endpoint: string,
  projectKey: string,
  appId: string,
  fetchImpl?: typeof fetch,
): Transport {
  const url = `${endpoint.replace(/\/+$/, "")}/v1/ingest/log-events`;
  const doFetch = fetchImpl ?? globalThis.fetch;

  return {
    async send(events: LogEvent[], signal: AbortSignal): Promise<TransportResult> {
      if (typeof doFetch !== "function") return { ok: false, status: 0 };
      try {
        const response = await doFetch(url, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${projectKey}`,
          },
          body: JSON.stringify({
            batch_id: `b-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`,
            app_id: appId,
            events,
          }),
          signal,
        });
        return {
          ok: response.ok,
          status: response.status,
          retryAfterMs: parseRetryAfter(response.headers.get("retry-after")),
        };
      } catch {
        // Same contract as the browser transport: a failed send is a result,
        // not an exception. A refused connection and a timeout are the same
        // thing to the retry logic.
        return { ok: false, status: 0 };
      }
    },
  };
}

export class PowerbaseNode {
  readonly core: PowerbaseCore;

  constructor(options: NodeOptions) {
    this.core = new PowerbaseCore({
      ...options,
      source: "server_sdk",
      transport: makeTransport(options.endpoint, options.projectKey, options.appId, options.fetchImpl),
    });
  }

  /** Capture an event, with the ambient trace context filled in. */
  capture(event: LogEventInput): boolean {
    const trace = currentTrace();
    return this.core.capture({
      trace_id: trace.trace_id,
      span_id: trace.span_id,
      request_id: trace.request_id,
      route_template: event.route_template ?? trace.route,
      ...event,
    });
  }

  captureError(error: unknown, context: Record<string, unknown> = {}): boolean {
    return this.core.captureError(error, { ...currentTrace(), ...context });
  }

  /** Record an HTTP request. Status and duration, never the body. */
  captureRequest(input: {
    method: string;
    route_template: string;
    status_code: number;
    duration_ms: number;
    request_id?: string;
  }): boolean {
    const severity = input.status_code >= 500 ? "ERROR" : input.status_code >= 400 ? "WARN" : "INFO";
    return this.capture({
      event_type: "http.request",
      severity,
      http_method: input.method.toUpperCase(),
      route_template: input.route_template,
      status_code: input.status_code,
      duration_ms: Math.round(input.duration_ms),
      request_id: input.request_id,
      error_code: input.status_code >= 400 ? `HTTP_${input.status_code}` : undefined,
    });
  }

  /** Record a query by shape. The statement itself is never sent. */
  captureQuery(input: { sql: string; duration_ms: number; error?: string }): boolean {
    const shape = sqlFingerprint(input.sql);
    return this.capture({
      event_type: "db.query",
      severity: input.error ? "ERROR" : "INFO",
      duration_ms: Math.round(input.duration_ms),
      error_code: input.error ? "DB_ERROR" : undefined,
      error_fingerprint: shape.fingerprint,
      attributes: { statement: shape.template },
    });
  }

  setEnabled(value: boolean): void {
    this.core.setEnabled(value);
  }

  flush() {
    return this.core.flush();
  }

  stats() {
    return this.core.stats();
  }

  shutdown() {
    return this.core.shutdown();
  }
}

export interface RequestLike {
  method?: string;
  url?: string;
  originalUrl?: string;
  headers?: HeaderLike;
  route?: { path?: string };
}

/**
 * Run `fn` with the request's trace context in scope.
 *
 * Prefers a real W3C `traceparent` from an upstream service; falls back to
 * generating one so a request still has a usable id when nothing upstream set
 * it.
 */
export function withTraceContext<T>(request: RequestLike, fn: () => T): T {
  return storage.run(contextFor(request), fn);
}

export function contextFor(request: RequestLike): TraceContext {
  const headers = request.headers ?? {};
  const header = (name: string): string | undefined => {
    const value = headers[name] ?? headers[name.toLowerCase()];
    if (value === undefined) return undefined;
    return Array.isArray(value) ? value[0] : String(value);
  };

  const parsed = parseTraceparent(header("traceparent")) ?? {};
  const incomingRequestId =
    header("x-request-id") ?? header("x-correlation-id") ?? header("request-id");

  return {
    ...parsed,
    request_id: incomingRequestId,
    route: routeOf(request),
  };
}

/**
 * The route *template*, never the concrete path.
 *
 * `/orders/8842?token=…` becomes `/orders/:id`. A concrete path carries ids and
 * query strings, which is exactly what should not be indexed.
 */
export function routeOf(request: RequestLike): string | undefined {
  if (request.route?.path) return request.route.path;

  const raw = request.originalUrl ?? request.url;
  if (!raw) return undefined;
  const path = raw.split("?")[0] ?? "";
  return (
    path
      .split("/")
      .map((segment) => {
        if (!segment) return segment;
        if (/^\d+$/.test(segment)) return ":id";
        if (/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(segment)) {
          return ":id";
        }
        if (segment.length >= 16 && /^[\w-]+$/.test(segment)) return ":id";
        return segment;
      })
      .join("/") || "/"
  );
}

export interface MiddlewareRequest extends RequestLike {
  on?: (event: string, listener: () => void) => void;
}

export type NextFunction = (error?: unknown) => void;

/**
 * Express-compatible middleware. Records status and duration on response finish.
 *
 * Reads no body, so there is nothing to redact and nothing to leak.
 */
export function traceMiddleware(sdk: PowerbaseNode) {
  return function powerbaseTrace(
    request: MiddlewareRequest,
    response: { statusCode?: number; on?: (event: string, listener: () => void) => void },
    next: NextFunction,
  ): void {
    const started = Date.now();
    const context = contextFor(request);

    storage.run(context, () => {
      response.on?.("finish", () => {
        sdk.captureRequest({
          method: request.method ?? "GET",
          route_template: context.route ?? "/",
          status_code: response.statusCode ?? 0,
          duration_ms: Date.now() - started,
          request_id: context.request_id,
        });
      });
      next();
    });
  };
}

/** Wrap a bare handler when there is no framework. */
export async function handleRequest<T>(
  sdk: PowerbaseNode,
  request: RequestLike,
  fn: () => Promise<T> | T,
): Promise<T> {
  const context = contextFor(request);
  const started = Date.now();
  return storage.run(context, async () => {
    try {
      const result = await fn();
      sdk.captureRequest({
        method: request.method ?? "GET",
        route_template: context.route ?? "/",
        status_code: 200,
        duration_ms: Date.now() - started,
        request_id: context.request_id,
      });
      return result;
    } catch (error) {
      sdk.captureError(error, {
        route: context.route,
        request_id: context.request_id,
        duration_ms: Date.now() - started,
      });
      throw error;
    }
  });
}

export { PowerbaseCore, fingerprint } from "./core.js";
export type { CoreOptions, LogEvent, LogEventInput, Severity, Transport } from "./core.js";
export { isSensitiveName, redactText, sanitizeAttributes, safeLocation } from "./redaction.js";
