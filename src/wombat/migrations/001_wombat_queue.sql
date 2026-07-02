-- 001_wombat_queue.sql — the wombat-owned durable bounded queue table (TK-2, EP-2, Q-46).
--
-- Idempotent DDL only (CREATE ... IF NOT EXISTS): safe to execute on every process start via
-- ensure_schema(conn), with no migration framework (NG-3). This is wombat's OWN Postgres
-- table — it never writes cog-worx substrate/journal records (non_goal).
--
-- Columns:
--   id              server-assigned primary key (FIFO tiebreaker alongside created_at).
--   idempotency_key UNIQUE — a second enqueue with the same key is a no-op (AC1).
--   payload         the queued item's data, stored as JSON text.
--   status          reserved lifecycle marker (default 'ready'); v1 logic keys off leased_by.
--   created_at      FIFO ordering key.
--   leased_by       NULL when unleased; set to the draining WombatQueue instance's epoch
--                   (uuid text) while in flight. A lease held by a DIFFERENT epoch than the
--                   current process is a dead prior process's orphaned lease (single-host,
--                   single-process v1, DEC-6) and is reclaimed on the next drain().

CREATE TABLE IF NOT EXISTS wombat_queue (
    id BIGSERIAL PRIMARY KEY,
    idempotency_key TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ready',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    leased_by TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS wombat_queue_idempotency_key_idx
    ON wombat_queue (idempotency_key);

CREATE INDEX IF NOT EXISTS wombat_queue_fifo_idx
    ON wombat_queue (created_at, id);
