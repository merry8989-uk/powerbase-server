/**
 * The engine, shared by the browser and Node entry points.
 *
 * It knows nothing about `window` or `http`. Everything environment-specific is
 * behind the `Transport` interface, which is what lets the same queue, backoff
 * and circuit-breaker logic be tested without a browser or a socket.
 */

import { MAX_VALUE_CHARS, redactText, sanitizeAttributes } from "./redaction.js";

export type Severity = "DEBUG" | "INFO" | "WARN" | "ERROR" | "FATAL";
export type Source = "browser_sdk" | "server_sdk" | "android_sdk" | "otel" | "system";

/** What a caller passes in. Everything but `event_type` is optional. */
export interface LogEventInput {
  event_type: string;
  severity?: Severity;
  /** Overrides the SDK's own source, e.g. to mark a forwarded event. */
  source?: Source;
  ts?: string | Date;
  message?: string;
  attributes?: Record<string, unknown>;
  service_name?: string;
  environment?: string;
  app_version?: string;
  release_id?: string;
  request_id?: string;
  trace_id?: string;
  span_id?: string;
  route_template?: string;
  http_method?: string;
  status_code?: number;
  duration_ms?: number;
  error_code?: string;
  error_fingerprint?: string;
}

/** What goes on the wire. Mirrors the server's `LogEventIn` model. */
export interface LogEvent extends Omit<LogEventInput, "ts"> {
  event_id: string;
  ts: string;
  source: Source;
  severity: Severity;
}

export interface TransportResult {
  ok: boolean;
  status: number;
  /** From a `Retry-After` header, in milliseconds, when the server sent one. */
  retryAfterMs?: number;
}

export interface Transport {
  send(events: LogEvent[], signal: AbortSignal): Promise<TransportResult>;
}

export interface CoreOptions {
  /** Base URL of the Powerbase server, e.g. `https://logs.example.com`. */
  endpoint: string;
  /** The project's ingest key. Public by design — it can write and nothing else. */
  projectKey: string;
  /** The project this key belongs to. Must match the key. */
  appId: string;
  transport: Transport;
  source?: Source;
  serviceName?: string;
  environment?: string;
  appVersion?: string;
  releaseId?: string;
  /** Collection on/off. Off means nothing is queued and nothing is sent. */
  enabled?: boolean;
  maxQueueSize?: number;
  maxBatchSize?: number;
  flushIntervalMs?: number;
  maxRetries?: number;
  baseBackoffMs?: number;
  maxBackoffMs?: number;
  circuitBreakerThreshold?: number;
  circuitBreakerCooldownMs?: number;
  /** Timeout for a single request. */
  requestTimeoutMs?: number;
  /** Injected for tests. */
  now?: () => number;
  random?: () => number;
  onError?: (error: unknown, context: string) => void;
}

export interface FlushResult {
  sent: number;
  failed: number;
  batches: number;
  /** True when the circuit breaker is open and nothing was attempted. */
  skipped: boolean;
}

export interface Stats {
  queued: number;
  dropped: number;
  sent: number;
  failed: number;
  consecutiveFailures: number;
  circuitOpen: boolean;
  enabled: boolean;
}

const DEFAULTS = {
  maxQueueSize: 500,
  maxBatchSize: 50,
  flushIntervalMs: 5000,
  maxRetries: 5,
  baseBackoffMs: 500,
  maxBackoffMs: 30_000,
  circuitBreakerThreshold: 5,
  circuitBreakerCooldownMs: 60_000,
  requestTimeoutMs: 15_000,
};

/** The server caps a message at 8000 characters; stay inside it. */
const MAX_MESSAGE_CHARS = 8000;

/**
 * The longest we will honour a `Retry-After`, independent of our own backoff
 * ceiling.
 *
 * `maxBackoffMs` is tuned for *our* retry schedule; applying it to the server's
 * instruction would mean retrying far sooner than we were told to, which is how
 * a client turns a soft rate limit into a hard one. The cap here exists only so
 * a broken or hostile `Retry-After` cannot stall the queue indefinitely.
 */
const MAX_RETRY_AFTER_MS = 5 * 60_000;

const HEX = /0x[0-9a-fA-F]+/g;
const NUM = /\d+/g;
const WS = /\s+/g;

function newId(): string {
  const globalCrypto = (globalThis as { crypto?: Crypto }).crypto;
  if (globalCrypto?.randomUUID) return globalCrypto.randomUUID();
  // Not cryptographic, and it does not need to be: this is a dedup handle, not
  // a secret.
  return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (char) => {
    const random = Math.floor(Math.random() * 16);
    const value = char === "x" ? random : (random & 0x3) | 0x8;
    return value.toString(16);
  });
}

function toIso(value: string | Date | undefined): string {
  if (value instanceof Date) return value.toISOString();
  if (typeof value === "string" && value) {
    const parsed = new Date(value);
    if (!Number.isNaN(parsed.getTime())) return parsed.toISOString();
  }
  return new Date().toISOString();
}

/**
 * A stable name for a class of error, so occurrences group.
 *
 * Addresses and numbers are stripped first: the same bug at a different memory
 * address, or on line 42 rather than line 43, is the same bug.
 */
export function fingerprint(kind: string, message?: string, stack?: string): string {
  const base = `${stack ?? ""}\n${message ?? ""}`
    .replace(HEX, "0xADDR")
    .replace(NUM, "N")
    .replace(WS, " ")
    .trim();

  // FNV-1a, 32-bit, hex. Short, stable and dependency-free; the server does its
  // own hashing and treats this as an opaque grouping key.
  let hash = 0x811c9dc5;
  const input = `${kind}:${base}`;
  for (let i = 0; i < input.length; i += 1) {
    hash ^= input.charCodeAt(i);
    hash = Math.imul(hash, 0x01000193) >>> 0;
  }
  return hash.toString(16).padStart(8, "0");
}

export class PowerbaseCore {
  private readonly options: Required<
    Pick<
      CoreOptions,
      | "endpoint"
      | "projectKey"
      | "appId"
      | "maxQueueSize"
      | "maxBatchSize"
      | "flushIntervalMs"
      | "maxRetries"
      | "baseBackoffMs"
      | "maxBackoffMs"
      | "circuitBreakerThreshold"
      | "circuitBreakerCooldownMs"
      | "requestTimeoutMs"
    >
  > &
    CoreOptions;

  private queue: LogEvent[] = [];
  private timer: ReturnType<typeof setInterval> | undefined;
  private inFlight: Promise<FlushResult> | undefined;
  private flushing = false;
  private consecutiveFailures = 0;
  private circuitOpenUntil = 0;
  private droppedCount = 0;
  private sentCount = 0;
  private failedCount = 0;
  private enabled: boolean;
  private closed = false;

  constructor(options: CoreOptions) {
    if (!options.endpoint) throw new Error("powerbase: endpoint is required");
    if (!options.projectKey) throw new Error("powerbase: projectKey is required");
    if (!options.appId) throw new Error("powerbase: appId is required");

    this.options = { ...DEFAULTS, ...options } as PowerbaseCore["options"];
    this.enabled = options.enabled ?? true;

    if (this.enabled) this.start();
  }

  // ------------------------------------------------------------------ state
  get isEnabled(): boolean {
    return this.enabled;
  }

  stats(): Stats {
    const now = this.now();
    return {
      queued: this.queue.length,
      dropped: this.droppedCount,
      sent: this.sentCount,
      failed: this.failedCount,
      consecutiveFailures: this.consecutiveFailures,
      circuitOpen: this.circuitOpenUntil > now,
      enabled: this.enabled,
    };
  }

  setEnabled(value: boolean): void {
    if (value === this.enabled) return;
    this.enabled = value;
    if (value) {
      this.start();
    } else {
      // Turning it off clears the queue. Anything already captured would
      // otherwise be sent later, which is not what "stop collecting" means.
      this.stop();
      this.queue = [];
    }
  }

  // ---------------------------------------------------------------- capture
  capture(input: LogEventInput): boolean {
    if (!this.enabled || this.closed) return false;

    const sanitized = sanitizeAttributes(input.attributes ?? {});
    const message =
      typeof input.message === "string"
        ? redactText(input.message).slice(0, MAX_MESSAGE_CHARS)
        : undefined;

    const event: LogEvent = {
      event_id: newId(),
      ts: toIso(input.ts),
      source: input.source ?? this.options.source ?? "browser_sdk",
      severity: input.severity ?? "INFO",
      event_type: String(input.event_type ?? "unknown").slice(0, 128),
      attributes: sanitized.attributes,
    };

    if (message !== undefined) event.message = message;
    const passthrough: Array<keyof LogEventInput> = [
      "service_name", "environment", "app_version", "release_id",
      "request_id", "trace_id", "span_id", "route_template", "http_method",
      "status_code", "duration_ms", "error_code", "error_fingerprint",
    ];
    for (const key of passthrough) {
      const value = input[key];
      if (value !== undefined) {
        (event as unknown as Record<string, unknown>)[key] = value;
      }
    }
    event.service_name = event.service_name ?? this.options.serviceName;
    event.environment = event.environment ?? this.options.environment;
    event.app_version = event.app_version ?? this.options.appVersion;
    event.release_id = event.release_id ?? this.options.releaseId;

    this.enqueue(event);
    if (this.queue.length >= this.options.maxBatchSize) {
      void this.flush();
    }
    return true;
  }

  captureError(error: unknown, context: Record<string, unknown> = {}): boolean {
    const isError = error instanceof Error;
    const kind = isError ? error.name : typeof error;
    const message = isError ? error.message : String(error);
    const stack = isError ? error.stack : undefined;

    return this.capture({
      event_type: "error",
      severity: "ERROR",
      message: stack ? `${message}\n${stack}` : message,
      error_code: typeof context.code === "string" ? context.code : kind,
      error_fingerprint: fingerprint(kind, message, stack),
      attributes: {
        ...context,
        error_name: kind,
      },
    });
  }

  private enqueue(event: LogEvent): void {
    this.queue.push(event);
    // Bounded: the oldest goes first. A queue that grows without limit turns a
    // network problem into a memory problem.
    while (this.queue.length > this.options.maxQueueSize) {
      this.queue.shift();
      this.droppedCount += 1;
    }
  }

  // ------------------------------------------------------------------- send
  private start(): void {
    if (this.timer || this.closed) return;
    this.timer = setInterval(() => {
      void this.flush();
    }, this.options.flushIntervalMs);
    // Do not hold a Node process open just to flush telemetry.
    const handle = this.timer as unknown as { unref?: () => void };
    if (typeof handle.unref === "function") handle.unref();
  }

  private stop(): void {
    if (this.timer) {
      clearInterval(this.timer);
      this.timer = undefined;
    }
  }

  flush(): Promise<FlushResult> {
    // One flush at a time. A second caller joins the first rather than racing it.
    if (this.inFlight) return this.inFlight;

    this.inFlight = this.runFlush().finally(() => {
      this.inFlight = undefined;
    });
    return this.inFlight;
  }

  private async runFlush(): Promise<FlushResult> {
    const empty: FlushResult = { sent: 0, failed: 0, batches: 0, skipped: false };
    if (!this.enabled || this.closed || this.queue.length === 0) return empty;

    // Half-open: after the cooldown, one attempt is allowed through to find out
    // whether the far side is back.
    if (this.circuitOpenUntil > this.now()) {
      return { ...empty, skipped: true };
    }

    // Re-entrancy guard. If the transport itself throws while we are sending —
    // a bad proxy, a patched fetch — we must not start another flush from the
    // error path and spiral.
    if (this.flushing) return empty;
    this.flushing = true;

    const total: FlushResult = { sent: 0, failed: 0, batches: 0, skipped: false };
    try {
      while (this.queue.length > 0 && !this.closed) {
        const batch = this.queue.slice(0, this.options.maxBatchSize);
        const result = await this.sendWithRetries(batch);
        total.batches += 1;

        if (result.ok) {
          // Only remove what was actually accepted. New events captured during
          // the send stay queued.
          this.queue.splice(0, batch.length);
          total.sent += batch.length;
          this.sentCount += batch.length;
          this.consecutiveFailures = 0;
          this.circuitOpenUntil = 0;
        } else {
          total.failed += batch.length;
          this.failedCount += batch.length;
          this.noteFailure();
          break; // stop the loop; the breaker or the next interval will retry
        }
      }
    } finally {
      this.flushing = false;
    }
    return total;
  }

  private noteFailure(): void {
    this.consecutiveFailures += 1;
    if (this.consecutiveFailures >= this.options.circuitBreakerThreshold) {
      this.circuitOpenUntil = this.now() + this.options.circuitBreakerCooldownMs;
      this.report(new Error("circuit breaker opened"), "circuit");
    }
  }

  private async sendWithRetries(batch: LogEvent[]): Promise<TransportResult> {
    let attempt = 0;
    let last: TransportResult = { ok: false, status: 0 };

    while (attempt <= this.options.maxRetries) {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), this.options.requestTimeoutMs);
      try {
        last = await this.options.transport.send(batch, controller.signal);
      } catch (error) {
        this.report(error, "transport");
        last = { ok: false, status: 0 };
      } finally {
        clearTimeout(timeout);
      }

      if (last.ok) return last;

      // 4xx other than 429 is our fault, not the network's. Retrying a
      // malformed batch just wastes the queue.
      if (last.status >= 400 && last.status < 500 && last.status !== 429) {
        return last;
      }
      attempt += 1;
      if (attempt > this.options.maxRetries) break;
      await this.sleep(this.backoffMs(attempt, last.retryAfterMs));
    }
    return last;
  }

  /** Exponential with full jitter, or the server's Retry-After if it gave one. */
  private backoffMs(attempt: number, retryAfterMs?: number): number {
    if (typeof retryAfterMs === "number" && retryAfterMs > 0) {
      // Honour it as asked, capped only by a generous absolute ceiling. The
      // server knows its own load better than we do.
      return Math.min(retryAfterMs, MAX_RETRY_AFTER_MS);
    }
    const ceiling = Math.min(
      this.options.baseBackoffMs * 2 ** (attempt - 1),
      this.options.maxBackoffMs,
    );
    return Math.floor(this.random() * ceiling);
  }

  /**
   * A backoff wait that is deliberately **not** unref'd.
   *
   * The flush interval timer is unref'd, so idle telemetry never keeps a Node
   * process alive. A sleep is different: it is always awaited inside a flush
   * that is already in progress, and unref'ing it means the wait may never
   * resolve if nothing else is holding the event loop open — which is exactly
   * what happens in a short-lived process or a test. A retry that silently
   * never completes is worse than one that keeps the process alive for a
   * moment, and `shutdown()` awaits the flush anyway.
   */
  private sleep(ms: number): Promise<void> {
    return new Promise((resolve) => {
      setTimeout(resolve, ms);
    });
  }

  private now(): number {
    return this.options.now ? this.options.now() : Date.now();
  }

  private random(): number {
    return this.options.random ? this.options.random() : Math.random();
  }

  private report(error: unknown, context: string): void {
    if (this.options.onError) {
      try {
        this.options.onError(error, context);
      } catch {
        // A reporter that throws must not take the SDK with it.
      }
    }
  }

  async shutdown(): Promise<FlushResult> {
    this.stop();
    const result = await this.flush();
    this.closed = true;
    return result;
  }
}

export { MAX_VALUE_CHARS };
