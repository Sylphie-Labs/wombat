-- 009_wombat_scratchpad.sql — the scoped working-memory substrate (TK-247, DEC-46).
--
-- Idempotent DDL only (CREATE ... IF NOT EXISTS): safe to execute on every process start via
-- ensure_schema(conn), with no migration framework (NG-3). This is the next free number after
-- 001/003/004/005/006/007/008.
--
-- Columns:
--   scope_key       the scratch scope's caller-chosen identifier (e.g. a run/session id).
--   entry_key       the entry's own key within its scope.
--   value           the entry's value, stored as JSONB.
--   created_at      write-once: set on first insert, never touched by a later upsert.
--   updated_at      bumped on every upsert of the same (scope_key, entry_key).
--
-- PRIMARY KEY (scope_key, entry_key) makes a re-put of the same entry an upsert.
--
-- NOT a second queue, NOT the memory graph, NOT a chat log, NOT gate input (DEC-46(e)) — this
-- table is data inside the CON-1 boundary, never trusted instruction (CON-3).

CREATE TABLE IF NOT EXISTS wombat_scratchpad (
    scope_key TEXT NOT NULL,
    entry_key TEXT NOT NULL,
    value JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (scope_key, entry_key)
);
