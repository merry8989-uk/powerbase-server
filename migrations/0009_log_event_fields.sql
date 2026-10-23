-- 0009_log_event_fields.sql
--
-- Four fields the Logs Explorer specification asks for and the table did not
-- have, plus TRACE in the severity vocabulary.
--
--   * `category`         — a validated event category (api, ui, job, auth, …)
--   * `source_type`      — web / android / backend. The specification's
--                          vocabulary is platform-shaped; `source` is
--                          SDK-shaped. Both are kept: `source` says which SDK
--                          sent it, `source_type` says what kind of thing it
--                          came from, and the explorer filters on the latter.
--   * `source_id`        — a registered source identifier, where one exists
--   * `platform_version` — browser or OS version, distinct from `app_version`
--
-- SQLite cannot alter a CHECK constraint, so the table is rebuilt to admit
-- TRACE. The pattern is the same one migration 0006 used for the batch ledger:
-- create, copy, drop, rename, recreate every index. Getting the index list
-- wrong here would silently make the explorer's queries slow, which is why the
-- schema contract test asserts all of them.

CREATE TABLE log_events_new (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id          TEXT NOT NULL,
    app_id            TEXT NOT NULL REFERENCES apps(id) ON DELETE CASCADE,
    event_time        TEXT NOT NULL,
    received_at       TEXT NOT NULL,
    source            TEXT NOT NULL CHECK (source IN
                          ('browser_sdk', 'server_sdk', 'android_sdk', 'otel', 'system')),
    severity          TEXT NOT NULL CHECK (severity IN
                          ('TRACE', 'DEBUG', 'INFO', 'WARN', 'ERROR', 'FATAL')),
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
    status_code       INTEGER,
    duration_ms       INTEGER,
    error_code        TEXT,
    error_fingerprint TEXT,
    message           TEXT,
    attributes        TEXT NOT NULL DEFAULT '{}',
    redaction_version INTEGER NOT NULL DEFAULT 1,
    sdk_version       TEXT,
    category          TEXT,
    source_type       TEXT CHECK (source_type IS NULL OR
                                  source_type IN ('web', 'android', 'backend')),
    source_id         TEXT,
    platform_version  TEXT,
    UNIQUE (app_id, event_id)
);

INSERT INTO log_events_new (
    id, event_id, app_id, event_time, received_at, source, severity, event_type,
    service_name, environment, app_version, release_id, request_id, trace_id,
    span_id, route_template, http_method, status_code, duration_ms, error_code,
    error_fingerprint, message, attributes, redaction_version, sdk_version
)
SELECT
    id, event_id, app_id, event_time, received_at, source, severity, event_type,
    service_name, environment, app_version, release_id, request_id, trace_id,
    span_id, route_template, http_method, status_code, duration_ms, error_code,
    error_fingerprint, message, attributes, redaction_version, sdk_version
FROM log_events;

DROP TABLE log_events;

ALTER TABLE log_events_new RENAME TO log_events;

-- Every index from 0003, plus two for the new filter columns.
CREATE INDEX idx_log_events_app_time ON log_events(app_id, event_time DESC);
CREATE INDEX idx_log_events_app_sev_time ON log_events(app_id, severity, event_time DESC);
CREATE INDEX idx_log_events_app_type_time ON log_events(app_id, event_type, event_time DESC);
CREATE INDEX idx_log_events_app_source_time ON log_events(app_id, source, event_time DESC);
CREATE INDEX idx_log_events_fingerprint ON log_events(app_id, error_fingerprint, event_time DESC);
CREATE INDEX idx_log_events_trace ON log_events(app_id, trace_id);
CREATE INDEX idx_log_events_request ON log_events(app_id, request_id);
CREATE INDEX idx_log_events_route ON log_events(app_id, route_template);
CREATE INDEX idx_log_events_category ON log_events(app_id, category, event_time DESC);
CREATE INDEX idx_log_events_source_type ON log_events(app_id, source_type, event_time DESC);
