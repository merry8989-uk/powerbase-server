-- 0008_traces_metrics_drains.sql
--
-- Step 6: the parts of observability that are not logs.
--
--   * `spans`         — OTLP traces. A trace id ties a request's spans to its
--                       log events, which is what turns a list of errors into
--                       an explanation.
--   * `metric_points` — OTLP metrics, kept raw. `metrics_daily` is a rollup for
--                       charts; these are the points as the SDK sent them.
--   * `log_drains`    — forward what was ingested to somewhere else, with a
--                       cursor so nothing is forwarded twice.

CREATE TABLE spans (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    span_id        TEXT NOT NULL,
    trace_id       TEXT NOT NULL,
    parent_span_id TEXT,
    app_id         TEXT NOT NULL REFERENCES apps(id) ON DELETE CASCADE,
    service_name   TEXT,
    operation      TEXT NOT NULL,
    span_kind      TEXT NOT NULL DEFAULT 'internal'
                   CHECK (span_kind IN ('internal', 'server', 'client', 'producer', 'consumer')),
    status         TEXT NOT NULL DEFAULT 'unset'
                   CHECK (status IN ('ok', 'error', 'unset')),
    status_message TEXT,
    start_time     TEXT NOT NULL,
    end_time       TEXT NOT NULL,
    duration_ms    INTEGER NOT NULL,
    -- http.route and friends, promoted out of attributes the same way log
    -- events promote them, so RED metrics can group on a route template.
    route_template TEXT,
    attributes     TEXT NOT NULL DEFAULT '{}',
    received_at    TEXT NOT NULL,
    UNIQUE (app_id, span_id)
);

CREATE INDEX idx_spans_app_time   ON spans(app_id, start_time DESC);
CREATE INDEX idx_spans_trace      ON spans(app_id, trace_id);
CREATE INDEX idx_spans_service    ON spans(app_id, service_name, start_time DESC);
CREATE INDEX idx_spans_operation  ON spans(app_id, operation, start_time DESC);
CREATE INDEX idx_spans_errors     ON spans(app_id, status, start_time DESC);

CREATE TABLE metric_points (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id         TEXT NOT NULL REFERENCES apps(id) ON DELETE CASCADE,
    metric_name    TEXT NOT NULL,
    metric_kind    TEXT NOT NULL CHECK (metric_kind IN ('gauge', 'sum', 'histogram')),
    unit           TEXT,
    service_name   TEXT,
    point_time     TEXT NOT NULL,
    -- gauge and sum carry the number here; a histogram carries its sum, with
    -- `count` alongside. Buckets stay in `attributes` rather than being
    -- modelled, because how they are read depends on the aggregation.
    value          REAL NOT NULL,
    count          INTEGER,
    attributes     TEXT NOT NULL DEFAULT '{}',
    -- A hash of the attribute set, so the unique index below can deduplicate a
    -- retried export without needing a generated column.
    dimension_hash TEXT NOT NULL,
    received_at    TEXT NOT NULL,
    UNIQUE (app_id, metric_name, point_time, dimension_hash)
);

CREATE INDEX idx_metric_points_app_time ON metric_points(app_id, point_time DESC);
CREATE INDEX idx_metric_points_name     ON metric_points(app_id, metric_name, point_time DESC);
CREATE INDEX idx_metric_points_service  ON metric_points(app_id, service_name, point_time DESC);

CREATE TABLE log_drains (
    id           TEXT PRIMARY KEY,
    -- Null means every project.
    app_id       TEXT REFERENCES apps(id) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    kind         TEXT NOT NULL CHECK (kind IN ('http')),
    target       TEXT NOT NULL,
    enabled      INTEGER NOT NULL DEFAULT 1,
    batch_size   INTEGER NOT NULL DEFAULT 500,
    min_severity TEXT NOT NULL DEFAULT 'DEBUG'
                 CHECK (min_severity IN ('DEBUG', 'INFO', 'WARN', 'ERROR', 'FATAL')),
    -- Where the last successful run got to. `(cursor_time, cursor_id)` is a
    -- keyset position, so a run that overlaps another cannot skip or repeat.
    cursor_time  TEXT,
    cursor_id    INTEGER,
    created_at   TEXT NOT NULL,
    last_run_at  TEXT,
    UNIQUE (app_id, name)
);

CREATE INDEX idx_log_drains_enabled ON log_drains(enabled, app_id);

CREATE TABLE drain_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    drain_id    TEXT NOT NULL REFERENCES log_drains(id) ON DELETE CASCADE,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    sent        INTEGER NOT NULL DEFAULT 0,
    ok          INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT,
    cursor_time TEXT,
    cursor_id   INTEGER
);

CREATE INDEX idx_drain_runs_drain ON drain_runs(drain_id, started_at DESC);
