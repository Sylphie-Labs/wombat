-- 008_external_items.sql — the Postgres-backed external-source item cache (TK-244, DEC-45).
--
-- Idempotent DDL only (CREATE ... IF NOT EXISTS): safe to execute on every process start via
-- ensure_schema(conn), with no migration framework (NG-3). This is the next free number after
-- 001/003/004/005/006/007.
--
-- Columns:
--   source          the external source name (e.g. a calendar/email/feed integration id).
--   item_key        the source's own stable identifier for the item.
--   payload         the caller-projected item payload, stored as JSONB. Never contains a
--                   projected body_text (DEC-45(d)) — this table never projects on its own.
--   occurs_at       the item's own timestamp (e.g. an event's start time), NULLable when the
--                   source has no natural occurs_at.
--   fetched_at      when this row was last (re-)fetched — bumped on every re-fetch of the same
--                   (source, item_key).
--   first_seen_at   write-once: set on first insert, never touched by a later upsert.
--
-- PRIMARY KEY (source, item_key) makes a re-fetch of the same item an upsert. The
-- (source, occurs_at) index supports get_window/get_recent's per-source, occurs_at-ordered reads.

CREATE TABLE IF NOT EXISTS wombat_external_items (
    source TEXT NOT NULL,
    item_key TEXT NOT NULL,
    payload JSONB NOT NULL,
    occurs_at TIMESTAMPTZ NULL,
    fetched_at TIMESTAMPTZ NOT NULL,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (source, item_key)
);

CREATE INDEX IF NOT EXISTS wombat_external_items_source_occurs_at_idx
    ON wombat_external_items (source, occurs_at);
