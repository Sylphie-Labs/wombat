-- 003_daily_ledger.sql — the shared DailyLedger table (TK-152, EP-2, Q-46/DEC-21).
--
-- Idempotent DDL only (CREATE ... IF NOT EXISTS): safe to execute on every process start via
-- ensure_schema(conn), with no migration framework (NG-3). One row per (ledger_name,
-- wombat_date) — the canonical wombat-day boundary shared by every daily counter (mouth spend
-- TK-9, surfacing ceiling TK-27/28, brief once-per-day TK-97). This table owns the row
-- lifecycle only; counter MEANING belongs to each consumer ticket (non_goal).
--
-- Columns:
--   ledger_name  which daily counter this row belongs to (e.g. 'spend', 'ceiling', 'brief').
--   wombat_date  the civil date (in the configured IANA zone) this row is for — computed by
--                wombat_today() at access time, never by a fired timer (AC3).
--   value        the counter itself; row lifecycle only, semantics owned by the caller.
--   created_at   row creation timestamp, for observability only.

CREATE TABLE IF NOT EXISTS daily_ledger (
    ledger_name TEXT NOT NULL,
    wombat_date DATE NOT NULL,
    value BIGINT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (ledger_name, wombat_date)
);
