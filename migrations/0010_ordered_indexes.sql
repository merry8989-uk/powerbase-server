-- 0010_ordered_indexes.sql
--
-- Every explorer query was paying for a sort it did not need.
--
-- The query orders by `event_time DESC, id DESC` — `id` is the tiebreaker that
-- makes the ordering total, which is what makes the keyset cursor exact. The
-- indexes were declared `(app_id, event_time DESC)` and so on: the leading
-- columns match, but nothing tells SQLite that the rowid inside the index is
-- ordered the way the query wants. The rowid is in *ascending* order within each
-- `event_time` value, the query asks for descending, and so the planner does
-- this:
--
--     SEARCH log_events USING INDEX idx_log_events_app_time (app_id=? AND event_time>?)
--     USE TEMP B-TREE FOR RIGHT PART OF ORDER BY
--
-- on **every** query. `scripts/querybench.py` is what found it; it printed that
-- line for all five scenarios, including the ones that looked fast.
--
-- Declaring `id DESC` as the last column makes the index order match the ORDER
-- BY exactly, and the sort step disappears. This is an index-only change: no
-- table is rewritten and no row moves.
--
-- Only the time-ordered indexes need it. `idx_log_events_trace`,
-- `idx_log_events_request` and `idx_log_events_route` are equality lookups with
-- no ORDER BY on `event_time`, and are left alone.

DROP INDEX IF EXISTS idx_log_events_app_time;
CREATE INDEX idx_log_events_app_time
    ON log_events(app_id, event_time DESC, id DESC);

DROP INDEX IF EXISTS idx_log_events_app_sev_time;
CREATE INDEX idx_log_events_app_sev_time
    ON log_events(app_id, severity, event_time DESC, id DESC);

DROP INDEX IF EXISTS idx_log_events_app_type_time;
CREATE INDEX idx_log_events_app_type_time
    ON log_events(app_id, event_type, event_time DESC, id DESC);

DROP INDEX IF EXISTS idx_log_events_app_source_time;
CREATE INDEX idx_log_events_app_source_time
    ON log_events(app_id, source, event_time DESC, id DESC);

DROP INDEX IF EXISTS idx_log_events_fingerprint;
CREATE INDEX idx_log_events_fingerprint
    ON log_events(app_id, error_fingerprint, event_time DESC, id DESC);

DROP INDEX IF EXISTS idx_log_events_category;
CREATE INDEX idx_log_events_category
    ON log_events(app_id, category, event_time DESC, id DESC);

DROP INDEX IF EXISTS idx_log_events_source_type;
CREATE INDEX idx_log_events_source_type
    ON log_events(app_id, source_type, event_time DESC, id DESC);
