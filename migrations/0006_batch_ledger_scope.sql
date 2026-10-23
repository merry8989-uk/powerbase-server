-- 0006_batch_ledger_scope.sql
--
-- The idempotency ledger was keyed on `batch_id` alone, which is global. A batch
-- id is a *client-side* identifier, so two consequences followed, and the first
-- one is a tenant-isolation hole rather than an inconvenience:
--
--   1. A key for one project could send another project's batch id first and
--      have that project's real batch silently ignored as a duplicate.
--   2. Two projects sending identical payloads collided. OTLP derives the batch
--      id from the payload, so this was not hypothetical.
--
-- The key becomes (app_id, batch_id): idempotency stays exactly as strong within
-- a project, and stops crossing the tenant boundary.
--
-- SQLite cannot alter a primary key in place, so the table is rebuilt.

CREATE TABLE ingest_batches_new (
    batch_id    TEXT NOT NULL,
    app_id      TEXT NOT NULL REFERENCES apps(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,
    received_at TEXT NOT NULL,
    event_count INTEGER NOT NULL,
    PRIMARY KEY (app_id, batch_id)
);

INSERT INTO ingest_batches_new (batch_id, app_id, kind, received_at, event_count)
    SELECT batch_id, app_id, kind, received_at, event_count FROM ingest_batches;

DROP TABLE ingest_batches;

ALTER TABLE ingest_batches_new RENAME TO ingest_batches;

CREATE INDEX idx_ingest_batches_app_time ON ingest_batches(app_id, received_at);
