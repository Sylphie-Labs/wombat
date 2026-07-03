-- 004_action_trail.sql — the action-trail projection table (TK-146, EP-27, Q-63).
--
-- Idempotent DDL only (CREATE ... IF NOT EXISTS): safe to execute on every process start via
-- ensure_schema(conn), with no migration framework (NG-3). Records proposed side-effects
-- (draft emails, form submits, ...) so a human can read a plain description of what will
-- happen before it happens (CON-4/DEC-19). Write-only from this ticket's side (TK-146);
-- TK-147 owns the read/render surface.
--
-- NO pg enum types (migration-hostile, Q-63) — action_type and status are closed vocabularies
-- enforced writer-side as str-Enums (ActionType, TrailStatus in schema.py), not by the DB.
--
-- Columns:
--   action_id     PRIMARY KEY — an OPAQUE caller-supplied stable string (NOT derived through
--                 TK-12 item_identity; an action is a proposed side effect, not a source item,
--                 Q-63). The writer keys on it verbatim for idempotent insert/transition.
--   seq           BIGSERIAL UNIQUE — TK-147's ordered/incremental read cursor.
--   action_type   the kind of proposed action (e.g. 'draft_email', 'blocked_by_taint').
--   human_summary a plain-language description of what will happen.
--   target        the action's target (e.g. a recipient or form URL).
--   proposed_at   caller-supplied aware-UTC timestamp of when the action was proposed.
--   status        'pending' | 'dispatched' | 'cancelled' | 'blocked' (TrailStatus).
--   dispatched_at set once, first-write-wins, on pending->dispatched.
--   cancelled_at  set once, first-write-wins, on pending->cancelled.

CREATE TABLE IF NOT EXISTS action_trail_projection (
    action_id TEXT PRIMARY KEY,
    seq BIGSERIAL NOT NULL UNIQUE,
    action_type TEXT NOT NULL,
    human_summary TEXT NOT NULL,
    target TEXT NOT NULL,
    proposed_at TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL,
    dispatched_at TIMESTAMPTZ NULL,
    cancelled_at TIMESTAMPTZ NULL
);
