-- Maintenance bookkeeping.
--
-- Every scheduled job records a row here, which buys three things:
--   * a visible audit trail of what ran, when, and whether it worked;
--   * "has today's rollup already run?" without a separate state file;
--   * a precise answer to "is this day rolled up?", which the read path needs
--     before it is allowed to serve a chart from metrics_daily instead of
--     aggregating the raw tables.

CREATE TABLE job_runs (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    job     TEXT NOT NULL,
    day     TEXT,
    ran_at  TEXT NOT NULL,
    ok      INTEGER NOT NULL,
    detail  TEXT
);
CREATE INDEX idx_job_runs_job_day ON job_runs (job, day);
CREATE INDEX idx_job_runs_ran_at ON job_runs (ran_at);
