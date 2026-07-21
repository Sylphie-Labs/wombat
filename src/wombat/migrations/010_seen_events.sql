-- 010_seen_events.sql — the persisted dedup ledger every source's enqueue passes through
-- (TK-286, DEC-63a).
--
-- Idempotent DDL only (CREATE ... IF NOT EXISTS): safe to execute on every process start via
-- ensure_schema(conn), with no migration framework (NG-3). This is the next free number after
-- 001/003/004/005/006/007/008/009.
--
-- Columns:
--   idempotency_key      the SAME canonical key wombat_queue.idempotency_key uses — one row per
--                        distinct source item, surviving across the item's queue-row lifecycle
--                        (wombat_queue's row is DELETEd on ack; this row is NOT).
--   payload_fingerprint  a sha256 hash of the item's payload (json.dumps(..., sort_keys=True)) —
--                        lets a KNOWN key with a CHANGED payload (e.g. an updated calendar event)
--                        legitimately re-enter the queue.
--   first_seen_at        write-once: set on first insert, never touched by a later upsert.
--   last_seen_at         bumped on every (re-)seen event, whether or not the fingerprint changed.
--
-- PRIMARY KEY (idempotency_key) — one row per distinct source item, forever (no pruning in v1).

CREATE TABLE IF NOT EXISTS wombat_seen_events (
    idempotency_key TEXT PRIMARY KEY,
    payload_fingerprint TEXT NOT NULL,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
