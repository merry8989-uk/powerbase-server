-- 0005_export_audit.sql
--
-- Two tables that answer two different questions:
--
--   * `export_jobs`  — where is that CSV I asked for, and is the link still good?
--   * `audit_events` — who took a copy of the data, and when?
--
-- The second one is the reason the first one is safe to offer.

CREATE TABLE export_jobs (
    id                TEXT PRIMARY KEY,
    app_id            TEXT NOT NULL REFERENCES apps(id) ON DELETE CASCADE,
    -- Null until project roles land; the admin password is a single actor today.
    requested_by      TEXT,
    format            TEXT NOT NULL CHECK (format IN ('csv', 'json', 'jsonl')),
    -- The validated query definition, same shape as a saved search. Never SQL.
    filter_definition TEXT NOT NULL DEFAULT '{}',
    status            TEXT NOT NULL CHECK (status IN
                          ('queued', 'running', 'completed', 'failed', 'expired')),
    row_count         INTEGER,
    result_bytes      INTEGER,
    result_path       TEXT,
    -- True when the row cap stopped the export short. Reported, never silent:
    -- a truncated file that looks complete is the worst possible outcome.
    truncated         INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL,
    started_at        TEXT,
    completed_at      TEXT,
    expires_at        TEXT,
    sanitized_error   TEXT
);

CREATE INDEX idx_export_jobs_app_time ON export_jobs(app_id, created_at DESC);
CREATE INDEX idx_export_jobs_status ON export_jobs(status, expires_at);

CREATE TABLE audit_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_time  TEXT NOT NULL,
    app_id      TEXT REFERENCES apps(id) ON DELETE SET NULL,
    actor       TEXT,
    event_type  TEXT NOT NULL,
    outcome     TEXT NOT NULL CHECK (outcome IN ('success', 'denied', 'failure')),
    target_type TEXT,
    target_id   TEXT,
    request_id  TEXT,
    metadata    TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX idx_audit_app_time  ON audit_events(app_id, event_time DESC);
CREATE INDEX idx_audit_type_time ON audit_events(event_type, event_time DESC);
CREATE INDEX idx_audit_time      ON audit_events(event_time DESC);
