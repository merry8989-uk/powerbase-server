/**
 * Browser entry point.
 *
 * What this deliberately does **not** collect, and why:
 *
 * - **DOM contents, form values, keystrokes.** A telemetry SDK that can read a
 *   form can read a password field. It does not get the ability.
 * - **Full URLs.** Query strings carry tokens, emails and ids. Only the origin
 *   and path are sent, via `safeLocation`.
 * - **Request or response bodies.** The SDK never wraps `fetch` or
 *   `XMLHttpRequest` to inspect payloads.
 * - **Anything at all when it is switched off.** `setEnabled(false)` empties the
 *   queue and stops the timer; there is a test for it.
 *
 * The project key is public by design. It is extractable from any bundle, which
 * is exactly why it can write to its own project and nothing else.
 */

import {
  PowerbaseCore,
  type CoreOptions,
  type LogEvent,
  type LogEventInput,
  type Transport,
  type TransportResult,
} from "./core.js";
import { safeLocation } from "./redaction.js";

export interface BrowserOptions extends Omit<CoreOptions, "transport" | "source"> {
  /** Capture `window.onerror` and unhandled promise rejections. Default true. */
  captureUnhandledErrors?: boolean;
  /** Capture navigation timing. Off by default. */
  capturePerformance?: boolean;
  /** Injected for tests. */
  fetchImpl?: typeof fetch;
  target?: Window;
}

export function parseRetryAfter(value: string | null): number | undefined {
  if (!value) return undefined;
  const seconds = Number(value);
  if (Number.isFinite(seconds)) return Math.max(0, seconds * 1000);
  const date = Date.parse(value);
  if (!Number.isNaN(date)) return Math.max(0, date - Date.now());
  return undefined;
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
      if (typeof doFetch !== "function") {
        return { ok: false, status: 0 };
      }
      try {
        const response = await doFetch(url, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${projectKey}`,
          },
          body: JSON.stringify({
            batch_id: batchId(),
            app_id: appId,
            events,
          }),
          signal,
          // No cookies, and nothing about the user goes along for the ride.
          credentials: "omit",
          mode: "cors",
          // keepalive would let the browser send this after the page unloads, but
          // it caps the body at 64 KB and silently fails beyond that. Not worth
          // the surprise.
          keepalive: false,
        });
        return {
          ok: response.ok,
          status: response.status,
          retryAfterMs: parseRetryAfter(response.headers.get("retry-after")),
        };
      } catch {
        // A transport that returns TransportResult must not also throw: an
        // offline browser, a blocked request and a timeout all arrive here as
        // the same thing — a send that did not happen. The core still catches
        // thrown transports, because a custom one might.
        return { ok: false, status: 0 };
      }
    },
  };
}

function batchId(): string {
  const globalCrypto = (globalThis as { crypto?: Crypto }).crypto;
  if (globalCrypto?.randomUUID) return globalCrypto.randomUUID();
  return `b-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

export class PowerbaseTelemetry {
  readonly core: PowerbaseCore;
  private readonly options: BrowserOptions;
  private teardown: Array<() => void> = [];

  constructor(options: BrowserOptions) {
    this.options = options;
    this.core = new PowerbaseCore({
      ...options,
      source: "browser_sdk",
      transport: makeTransport(options.endpoint, options.projectKey, options.appId, options.fetchImpl),
    });

    if (options.captureUnhandledErrors ?? true) {
      this.watchGlobalErrors(options.target ?? globalThis.window);
    }
    if (options.capturePerformance) {
      this.captureNavigation();
    }
  }

  capture(event: LogEventInput): boolean {
    return this.core.capture({
      route_template: event.route_template ?? this.currentRoute(),
      ...event,
    });
  }

  captureError(error: unknown, context: Record<string, unknown> = {}): boolean {
    return this.core.captureError(error, { route: this.currentRoute(), ...context });
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

  async shutdown() {
    for (const off of this.teardown) off();
    this.teardown = [];
    return this.core.shutdown();
  }

  private currentRoute(): string | undefined {
    const target = this.options.target ?? globalThis.window;
    const href = target?.location?.href;
    return href ? safeLocation(href) : undefined;
  }

  private watchGlobalErrors(target: Window | undefined): void {
    if (!target?.addEventListener) return;

    const onError = (event: ErrorEvent) => {
      const error = event.error ?? new Error(event.message);
      this.core.captureError(error, {
        route: this.currentRoute(),
        // The filename is a URL, so it goes through the same treatment: origin
        // and path only, never the query string.
        source_file: event.filename ? safeLocation(event.filename) : undefined,
        line: event.lineno,
        column: event.colno,
      });
    };

    const onRejection = (event: PromiseRejectionEvent) => {
      this.core.captureError(event.reason, {
        route: this.currentRoute(),
        kind: "unhandledrejection",
      });
    };

    target.addEventListener("error", onError as EventListener);
    target.addEventListener("unhandledrejection", onRejection as EventListener);
    this.teardown.push(() => {
      target.removeEventListener("error", onError as EventListener);
      target.removeEventListener("unhandledrejection", onRejection as EventListener);
    });
  }

  private captureNavigation(): void {
    const target = this.options.target ?? globalThis.window;
    const performance = target?.performance;
    if (!performance?.getEntriesByType) return;

    const report = () => {
      const entries = performance.getEntriesByType("navigation") as PerformanceNavigationTiming[];
      const entry = entries[0];
      if (!entry) return;
      const duration = Math.round(entry.duration || entry.loadEventEnd - entry.startTime);
      if (!Number.isFinite(duration) || duration <= 0) return;
      this.core.capture({
        event_type: "navigation",
        severity: "INFO",
        duration_ms: duration,
        route_template: this.currentRoute(),
        attributes: {
          dom_content_loaded_ms: Math.round(entry.domContentLoadedEventEnd),
          transfer_size: entry.transferSize,
          navigation_type: entry.type,
        },
      });
    };

    if (target.document?.readyState === "complete") {
      report();
    } else {
      const listener = () => report();
      target.addEventListener("load", listener, { once: true });
      this.teardown.push(() => target.removeEventListener("load", listener));
    }
  }
}

export { PowerbaseCore, fingerprint } from "./core.js";
export type { CoreOptions, LogEvent, LogEventInput, Severity, Transport } from "./core.js";
export { isSensitiveName, redactText, sanitizeAttributes, safeLocation } from "./redaction.js";
