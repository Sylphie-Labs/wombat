-- 007_wombat_settings.sql — the Postgres-backed app-editable settings table (TK-240, DEC-43/DEC-44).
--
-- Idempotent DDL only (CREATE ... IF NOT EXISTS): safe to execute on every process start via
-- ensure_schema(conn), with no migration framework (NG-3). This is the next free number after
-- 001/003/004/005/006.
--
-- Columns:
--   key         the setting name (an APP_EDITABLE_FIELDS name, or 'wombat_persona_pins') —
--               PRIMARY KEY, so a write is always an upsert.
--   value       the setting's value, stored as JSONB.
--   updated_at  bumped to now() on every write (SettingsStore.put) — never backfilled otherwise.

CREATE TABLE IF NOT EXISTS wombat_settings (
    key TEXT PRIMARY KEY,
    value JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
