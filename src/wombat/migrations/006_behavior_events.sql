-- 006_behavior_events.sql — the append-only behavioral event log (TK-111, EP-21, Q-98).
--
-- Idempotent DDL only (CREATE ... IF NOT EXISTS): safe to execute on every process start via
-- ensure_schema(conn), with no migration framework (NG-3). Slots 002 and beyond that were never
-- issued are documented historical gaps (see 005_pending_journal.sql) — migration numbering is
-- strictly monotonic and a released slot is never reused, so this is 006, the next free number
-- after 001/003/004/005.
--
-- Written ONLY by the nightly dream pass (DreamBehaviorLogStage, EP-13) — no hot-path writer, no
-- dashboard/analytics reader (NG-3, structurally enforced by an import-surface test).
--
-- MOTIVE-FREE BY CONSTRUCTION (CON-6/NG-1, Q-98 ruling f): there is no motive/why column, and
-- there never can be one added casually — TK-43's ClaimPredicate closed enum is the ONE
-- type-level wall enforcing this upstream, at claim-construction time; this table has no
-- competing runtime schema-violation mechanism of its own.
--
-- Columns:
--   idempotency_key   PRIMARY KEY — the canonical TK-12 idempotency_key (== the terminal OUTCOME_*
--                     claim's payload item_ref), NOT an ad-hoc id. Re-running the nightly pass
--                     over the SAME terminal claim upserts this SAME row (AC1 idempotency).
--   event_type        the EventClass value the claim was scored under (e.g. 'draft_reply').
--   source_id         parsed from idempotency_key via domain.item_identity.split_idempotency_key
--                      — the source's own registration id ('calendar', 'gmail', ...).
--   timestamp_utc     the claim payload's resolved_at (aware UTC) — ordering/readability (AC3).
--   outcome_label      the closed OUTCOME_* predicate value the claim carried (load_bearing |
--                      regretted | ignored) — never a free-form string, never a motive.
--   duration_seconds   NULLABLE; NULL in v1 — no duration signal exists yet (recorded honestly,
--                      not synthesized).
--   created_at         row creation timestamp, for observability only — NOT used for ordering.

CREATE TABLE IF NOT EXISTS wombat_behavior_events (
    idempotency_key TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    timestamp_utc TIMESTAMPTZ NOT NULL,
    outcome_label TEXT NOT NULL,
    duration_seconds DOUBLE PRECISION NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
