-- 005_pending_journal.sql — the pending-set durable write-ahead log table (TK-29, RISK-5, Q-70).
--
-- Idempotent DDL only (CREATE ... IF NOT EXISTS): safe to execute on every process start via
-- ensure_schema(conn), with no migration framework (NG-3). Slot 002 is a documented historical
-- gap (never issued) — migration numbering is strictly monotonic and a released slot is never
-- reused, so this is 005, not 002.
--
-- A DISTINCT table from wombat_queue (001) — this is post-ack gate custody state, a different
-- lifecycle entirely (Q-44: the pending set's own journal seam, not a cog-worx substrate/journal
-- record and not a view/projection of wombat_queue).
--
-- NO pg enum type for record_type (migration-hostile) — the writer-side vocabulary ('add' |
-- 'remove' | 'clear') is enforced in Python (PgPendingJournal), mirroring action_trail's
-- str-Enum-over-TEXT convention (004).
--
-- Columns:
--   seq         server-assigned, strictly increasing — the ONLY replay order (never created_at).
--   record_type 'add' | 'remove' | 'clear'.
--   item_id     the item's TK-12-derived identity, stored verbatim; NULL for 'clear'.
--   item_kind   ItemKind.value (TEXT); NULL for 'remove'/'clear'.
--   urgency     NULL for 'remove'/'clear'.
--   load        NULL for 'remove'/'clear'.
--   added_at    the Q-55 rider (epoch seconds) on 'add' rows; NULL replays as 0.0 (never raises).
--   created_at  observability only — NOT used for ordering.

CREATE TABLE IF NOT EXISTS pending_journal (
    seq BIGSERIAL PRIMARY KEY,
    record_type TEXT NOT NULL,
    item_id TEXT NULL,
    item_kind TEXT NULL,
    urgency DOUBLE PRECISION NULL,
    load DOUBLE PRECISION NULL,
    added_at DOUBLE PRECISION NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
