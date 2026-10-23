-- 0007_team_and_alerting.sql
--
-- Two halves of the same idea: knowing something broke, and knowing who is
-- allowed to do something about it.
--
-- Alerting first. Until now a rule was re-evaluated every pass and the webhook
-- fired every time, so a problem that lasted an hour produced a dozen identical
-- notifications and a webhook that failed was simply lost. An **incident** is
-- one rule failing in one project; the notification is a **delivery** that can
-- be retried, and given up on, explicitly.

CREATE TABLE alert_incidents (
    id               TEXT PRIMARY KEY,
    -- app_id + rule. One open incident per key, enforced by the partial index
    -- below rather than by hope.
    dedup_key        TEXT NOT NULL,
    app_id           TEXT NOT NULL REFERENCES apps(id) ON DELETE CASCADE,
    rule             TEXT NOT NULL,
    severity         TEXT NOT NULL CHECK (severity IN ('critical', 'warning', 'info')),
    title            TEXT NOT NULL,
    detail           TEXT,
    context          TEXT NOT NULL DEFAULT '{}',
    status           TEXT NOT NULL CHECK (status IN ('firing', 'acknowledged', 'resolved')),
    occurrences      INTEGER NOT NULL DEFAULT 1,
    first_seen_at    TEXT NOT NULL,
    last_seen_at     TEXT NOT NULL,
    last_notified_at TEXT,
    notify_count     INTEGER NOT NULL DEFAULT 0,
    acked_by         TEXT,
    acked_at         TEXT,
    resolved_at      TEXT,
    resolved_by      TEXT
);

-- At most one *open* incident per key. Resolved ones stay, so the history is
-- intact and a flapping rule does not silently merge with last week's.
CREATE UNIQUE INDEX idx_alert_incidents_open
    ON alert_incidents(dedup_key) WHERE status IN ('firing', 'acknowledged');
CREATE INDEX idx_alert_incidents_app ON alert_incidents(app_id, first_seen_at DESC);
CREATE INDEX idx_alert_incidents_status ON alert_incidents(status, last_seen_at DESC);

CREATE TABLE alert_channels (
    id           TEXT PRIMARY KEY,
    -- Null means "every project".
    app_id       TEXT REFERENCES apps(id) ON DELETE CASCADE,
    kind         TEXT NOT NULL CHECK (kind IN ('webhook', 'email')),
    target       TEXT NOT NULL,
    enabled      INTEGER NOT NULL DEFAULT 1,
    min_severity TEXT NOT NULL DEFAULT 'info'
                 CHECK (min_severity IN ('info', 'warning', 'critical')),
    created_at   TEXT NOT NULL,
    UNIQUE (app_id, kind, target)
);

CREATE INDEX idx_alert_channels_app ON alert_channels(app_id, enabled);

CREATE TABLE alert_deliveries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id     TEXT REFERENCES alert_incidents(id) ON DELETE CASCADE,
    channel_id      TEXT REFERENCES alert_channels(id) ON DELETE SET NULL,
    kind            TEXT NOT NULL CHECK (kind IN ('webhook', 'email')),
    target          TEXT NOT NULL,
    event           TEXT NOT NULL CHECK (event IN ('opened', 'reminder', 'resolved')),
    status          TEXT NOT NULL CHECK (status IN ('pending', 'sent', 'failed', 'dead')),
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    last_error      TEXT,
    created_at      TEXT NOT NULL,
    delivered_at    TEXT
);

CREATE INDEX idx_alert_deliveries_due ON alert_deliveries(status, next_attempt_at);
CREATE INDEX idx_alert_deliveries_incident ON alert_deliveries(incident_id, created_at DESC);

-- ---------------------------------------------------------------------------
-- Team access
-- ---------------------------------------------------------------------------

CREATE TABLE users (
    id              TEXT PRIMARY KEY,
    email           TEXT NOT NULL UNIQUE,
    display_name    TEXT,
    -- Null while invited; set when the invite is accepted.
    password_hash   TEXT,
    role            TEXT NOT NULL CHECK (role IN ('owner', 'admin', 'developer', 'readonly')),
    status          TEXT NOT NULL CHECK (status IN ('invited', 'active', 'disabled')),
    created_at      TEXT NOT NULL,
    last_login_at   TEXT,
    failed_attempts INTEGER NOT NULL DEFAULT 0,
    locked_until    TEXT
);

CREATE INDEX idx_users_role ON users(role, status);

CREATE TABLE user_sessions (
    id         TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    -- The token itself is never stored, for the same reason a password is not.
    token_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT,
    user_agent TEXT
);

CREATE INDEX idx_user_sessions_token ON user_sessions(token_hash);
CREATE INDEX idx_user_sessions_user ON user_sessions(user_id, expires_at);

CREATE TABLE invites (
    id          TEXT PRIMARY KEY,
    email       TEXT NOT NULL,
    role        TEXT NOT NULL CHECK (role IN ('admin', 'developer', 'readonly')),
    -- Null means instance-wide; set means "this person gets this one project".
    app_id      TEXT REFERENCES apps(id) ON DELETE CASCADE,
    token_hash  TEXT NOT NULL,
    invited_by  TEXT,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    accepted_at TEXT,
    accepted_by TEXT
);

CREATE INDEX idx_invites_email ON invites(email, accepted_at);

CREATE TABLE project_members (
    app_id     TEXT NOT NULL REFERENCES apps(id) ON DELETE CASCADE,
    user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role       TEXT NOT NULL CHECK (role IN ('admin', 'developer', 'readonly')),
    created_at TEXT NOT NULL,
    PRIMARY KEY (app_id, user_id)
);

CREATE INDEX idx_project_members_user ON project_members(user_id, app_id);
