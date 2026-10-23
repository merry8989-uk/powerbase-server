-- 0003_log_events.sql
--
-- Observability-shaped log events, alongside the existing mobile-analytics
-- tables. The two models coexist on purpose:
--
--   * `events`     answers "what are users doing"      (screen, name, properties)
--   * `log_events` answers "what is the software doing" (severity, route, status,
--                  duration, trace id, error fingerprint)
--
-- A browser or backend log has nowhere to live in the first model, which is
-- exactly why websites could not be integrated before this migration.

CREATE TABLE log_events (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id          TEXT NOT NULL,
    app_id            TEXT NOT NULL REFERENCES apps(id) ON DELETE CASCADE,
    event_time        TEXT NOT NULL,
    received_at       TEXT NOT NULL,
    source            TEXT NOT NULL CHECK (source IN
                          ('browser_sdk', 'server_sdk', 'android_sdk', 'otel', 'system')),
    severity          TEXT NOT NULL CHECK (severity IN
                          ('DEBUG', 'INFO', 'WARN', 'ERROR', 'FATAL')),
    event_type        TEXT NOT NULL,
    service_name      TEXT,
    environment       TEXT,
    app_version       TEXT,
    release_id        TEXT,
    request_id        TEXT,
    trace_id          TEXT,
    span_id           TEXT,
    route_template    TEXT,
    http_method       TEXT,
    status_code       INTEGER CHECK (status_code IS NULL
                          OR (status_code BETWEEN 100 AND 599)),
    duration_ms       INTEGER CHECK (duration_ms IS NULL OR duration_ms >= 0),
    error_code        TEXT,
    error_fingerprint TEXT,
    message           TEXT,
    attributes        TEXT NOT NULL DEFAULT '{}',
    -- 0 means "stored unredacted on purpose" (redaction disabled for this
    -- install); 1 is the current rule set. Bumping the rules bumps this, so a
    -- row can always be traced to the redaction it went through.
    redaction_version INTEGER NOT NULL DEFAULT 1,
    sdk_version       TEXT,
    -- Per-event idempotency on top of the per-batch ledger: a client that
    -- retries a batch with one new event must not duplicate the old ones.
    UNIQUE(app_id, event_id)
);

-- Indexes follow the query shapes the console will run: always scoped to one
-- project, always ordered by time, usually filtered by one more column.
CREATE INDEX idx_log_events_app_time       ON log_events(app_id, event_time DESC);
CREATE INDEX idx_log_events_app_sev_time   ON log_events(app_id, severity, event_time DESC);
CREATE INDEX idx_log_events_app_type_time  ON log_events(app_id, event_type, event_time DESC);
CREATE INDEX idx_log_events_app_source_time ON log_events(app_id, source, event_time DESC);
CREATE INDEX idx_log_events_fingerprint    ON log_events(app_id, error_fingerprint, event_time DESC);
CREATE INDEX idx_log_events_trace          ON log_events(app_id, trace_id);
CREATE INDEX idx_log_events_request        ON log_events(app_id, request_id);
CREATE INDEX idx_log_events_route          ON log_events(app_id, route_template, event_time DESC);

-- Keys gain an expiry and a rotation lineage. `expires_at` lets an operator
-- retire a key on a schedule; `rotated_from` keeps the trail of which key a
-- replacement came from, so rotation is auditable without deleting history.
ALTER TABLE api_keys ADD COLUMN expires_at TEXT;
ALTER TABLE api_keys ADD COLUMN rotated_from TEXT;
