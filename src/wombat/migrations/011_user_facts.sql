-- 011_user_facts.sql — the durable what-wombat-knows-about-the-user table (TK-294, DEC-65d).
--
-- Idempotent DDL only (CREATE ... IF NOT EXISTS): safe to execute on every process start via
-- ensure_schema(conn), with no migration framework (NG-3). This is the next free number after
-- 001/003/004/005/006/007/008/009/010.
--
-- Columns:
--   fact_key        caller-supplied stable identity for one fact (TK-297 derives it
--                   deterministically from normalized fact text) — one row per distinct fact.
--   fact            the fact text itself.
--   source          the DEC-66 provenance spine (dream | derived | behavior | told) — never
--                   collapsed or defaulted by this module.
--   first_seen_at   write-once: set on first insert, never touched by a later upsert.
--   updated_at      bumped on every (re-)upsert.
--
-- PRIMARY KEY (fact_key) — one row per distinct fact, forever (no pruning, DEC-65d: durability
-- is the point). The 200-row cap is enforced in application code (UserFactsStore), not here.

CREATE TABLE IF NOT EXISTS wombat_user_facts (
    fact_key TEXT PRIMARY KEY,
    fact TEXT NOT NULL,
    source TEXT NOT NULL,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
