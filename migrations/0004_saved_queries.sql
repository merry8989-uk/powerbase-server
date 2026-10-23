-- 0004_saved_queries.sql
--
-- Saved log-explorer queries. The definition is stored as validated JSON, not
-- as SQL: it goes through the same allowlist as a live query on the way in and
-- on the way out, so a saved search can never hold something the query layer
-- would refuse to run.

CREATE TABLE saved_queries (
    id               TEXT PRIMARY KEY,
    app_id           TEXT NOT NULL REFERENCES apps(id) ON DELETE CASCADE,
    name             TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 120),
    -- The validated query definition: {"filters": [...], "sort": {...}, "q": "..."}
    query_definition TEXT NOT NULL DEFAULT '{}',
    -- Nullable because there is no user table yet; project roles arrive in a
    -- later step and will start filling this in.
    created_by       TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    UNIQUE(app_id, name)
);

CREATE INDEX idx_saved_queries_app ON saved_queries(app_id, name);
