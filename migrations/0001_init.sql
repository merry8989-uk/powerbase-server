-- Powerbase schema v1
-- Nine tables: a tenant root (apps), credentials, device/session context,
-- three high-volume fact tables (events, logs, crashes), a rollup table and
-- an idempotency ledger.

CREATE TABLE apps (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    platform        TEXT NOT NULL CHECK (platform IN ('android', 'ios', 'backend', 'web')),
    retention_days  INTEGER NOT NULL DEFAULT 90 CHECK (retention_days > 0),
    sampling_rate   REAL NOT NULL DEFAULT 1.0 CHECK (sampling_rate >= 0 AND sampling_rate <= 1),
    created_at      TEXT NOT NULL
);

CREATE TABLE api_keys (
    id           TEXT PRIMARY KEY,
    app_id       TEXT NOT NULL REFERENCES apps(id) ON DELETE CASCADE,
    key_hash     TEXT NOT NULL,
    scope        TEXT NOT NULL CHECK (scope IN ('ingest', 'read')),
    label        TEXT,
    created_at   TEXT NOT NULL,
    last_used_at TEXT,
    revoked_at   TEXT
);
CREATE INDEX idx_api_keys_app ON api_keys(app_id);

CREATE TABLE devices (
    id           TEXT PRIMARY KEY,
    app_id       TEXT NOT NULL REFERENCES apps(id) ON DELETE CASCADE,
    model        TEXT,
    manufacturer TEXT,
    os_version   TEXT,
    app_version  TEXT,
    sdk_version  TEXT,
    locale       TEXT,
    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL
);
CREATE INDEX idx_devices_app_seen ON devices(app_id, last_seen);

CREATE TABLE sessions (
    id          TEXT PRIMARY KEY,
    app_id      TEXT NOT NULL REFERENCES apps(id) ON DELETE CASCADE,
    device_id   TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    user_ref    TEXT,
    started_at  TEXT NOT NULL,
    ended_at    TEXT,
    duration_ms INTEGER,
    end_reason  TEXT CHECK (end_reason IN ('background', 'crash', 'timeout') OR end_reason IS NULL),
    app_version TEXT,
    os_version  TEXT,
    country     TEXT
);
CREATE INDEX idx_sessions_device ON sessions(device_id, started_at);
CREATE INDEX idx_sessions_app_started ON sessions(app_id, started_at);

CREATE TABLE events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id      TEXT NOT NULL REFERENCES apps(id) ON DELETE CASCADE,
    device_id   TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    session_id  TEXT REFERENCES sessions(id) ON DELETE CASCADE,
    ts          TEXT NOT NULL,
    received_at TEXT NOT NULL,
    name        TEXT NOT NULL,
    category    TEXT,
    screen      TEXT,
    properties  TEXT NOT NULL DEFAULT '{}',
    sdk_version TEXT
);
CREATE INDEX idx_events_app_ts ON events(app_id, ts);
CREATE INDEX idx_events_session ON events(session_id);
CREATE INDEX idx_events_app_name ON events(app_id, name);

CREATE TABLE logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id      TEXT NOT NULL REFERENCES apps(id) ON DELETE CASCADE,
    device_id   TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    session_id  TEXT REFERENCES sessions(id) ON DELETE CASCADE,
    ts          TEXT NOT NULL,
    received_at TEXT NOT NULL,
    level       TEXT NOT NULL CHECK (level IN ('DEBUG', 'INFO', 'WARN', 'ERROR', 'FATAL')),
    tag         TEXT,
    message     TEXT NOT NULL,
    stacktrace  TEXT,
    context     TEXT NOT NULL DEFAULT '{}',
    sdk_version TEXT
);
CREATE INDEX idx_logs_app_ts ON logs(app_id, ts);
CREATE INDEX idx_logs_level_ts ON logs(level, ts);
CREATE INDEX idx_logs_session ON logs(session_id);

-- Full-text search over log messages (external-content FTS5 table kept in
-- sync by triggers, so it costs no extra storage for the message text).
CREATE VIRTUAL TABLE logs_fts USING fts5(
    message,
    content='logs',
    content_rowid='id',
    tokenize='unicode61'
);

CREATE TRIGGER logs_fts_insert AFTER INSERT ON logs BEGIN
    INSERT INTO logs_fts(rowid, message) VALUES (new.id, new.message);
END;

CREATE TRIGGER logs_fts_delete AFTER DELETE ON logs BEGIN
    INSERT INTO logs_fts(logs_fts, rowid, message) VALUES ('delete', old.id, old.message);
END;

CREATE TRIGGER logs_fts_update AFTER UPDATE ON logs BEGIN
    INSERT INTO logs_fts(logs_fts, rowid, message) VALUES ('delete', old.id, old.message);
    INSERT INTO logs_fts(rowid, message) VALUES (new.id, new.message);
END;

CREATE TABLE crashes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id      TEXT NOT NULL REFERENCES apps(id) ON DELETE CASCADE,
    device_id   TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    session_id  TEXT REFERENCES sessions(id) ON DELETE CASCADE,
    ts          TEXT NOT NULL,
    received_at TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('java', 'native', 'anr', 'other')),
    message     TEXT,
    stacktrace  TEXT,
    fingerprint TEXT NOT NULL,
    handled     INTEGER NOT NULL DEFAULT 0,
    app_version TEXT,
    os_version  TEXT
);
CREATE INDEX idx_crashes_app_ts ON crashes(app_id, ts);
CREATE INDEX idx_crashes_fingerprint ON crashes(app_id, fingerprint);

CREATE TABLE metrics_daily (
    day       TEXT NOT NULL,
    app_id    TEXT NOT NULL REFERENCES apps(id) ON DELETE CASCADE,
    metric    TEXT NOT NULL,
    dimension TEXT NOT NULL DEFAULT '',
    value     REAL NOT NULL,
    PRIMARY KEY (day, app_id, metric, dimension)
);

CREATE TABLE ingest_batches (
    batch_id    TEXT PRIMARY KEY,
    app_id      TEXT NOT NULL REFERENCES apps(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,
    received_at TEXT NOT NULL,
    event_count INTEGER NOT NULL
);
CREATE INDEX idx_ingest_batches_app_time ON ingest_batches(app_id, received_at);
