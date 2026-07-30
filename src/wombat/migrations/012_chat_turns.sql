-- 012_chat_turns.sql — the 7-day rolling ledger of the user's chat/voice utterances (TK-295,
-- DEC-65e).
--
-- Idempotent DDL only (CREATE ... IF NOT EXISTS): safe to execute on every process start via
-- ensure_schema(conn), with no migration framework (NG-3). This is the next free number after
-- 001/003/004/005/006/007/008/009/010/011.
--
-- Columns:
--   id            surrogate identity, insertion order.
--   text          the utterance's text — the user's own words only (CON-1/CON-6): never an
--                 assistant reply, never a correlation id.
--   voice         True iff this turn arrived as a spoken (ASR) utterance, False for typed chat.
--   captured_at   when the utterance was captured (the source's own received_at/captured_at
--                 timestamp, not insertion time).
--
-- No PRIMARY KEY beyond the surrogate id — a row per turn, purged by age in application code
-- (ChatTurnStore.purge_older_than), never here. Indexed on captured_at: both the ascending
-- turns_since(cutoff) read and the age-based purge filter on this column.

CREATE TABLE IF NOT EXISTS wombat_chat_turns (
    id BIGSERIAL PRIMARY KEY,
    text TEXT NOT NULL,
    voice BOOLEAN NOT NULL,
    captured_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS wombat_chat_turns_captured_at_idx
    ON wombat_chat_turns (captured_at);
