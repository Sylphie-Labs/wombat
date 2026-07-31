-- 013_observations.sql — the append-only ambient-observability ledger (TK-310, DEC-68(a)/(c)).
--
-- Idempotent DDL only (CREATE ... IF NOT EXISTS): safe to execute on every process start via
-- ensure_schema(conn), with no migration framework (NG-3). This is the next free number after
-- 001/003/004/005/006/007/008/009/010/011/012.
--
-- Columns:
--   id            surrogate identity, insertion order.
--   channel       the observation channel a row belongs to, e.g. 'screen' (webcam/mic land in
--                 later tickets — DEC-68(a)).
--   kind          the observation kind within that channel, e.g. 'app_segment'.
--   started_at    when this closed segment began.
--   ended_at      when this closed segment ended.
--   payload       JSONB, bounded — the channel/kind-specific projected fields only (screen's
--                 {app, title}). Never raw pixels/audio/screenshots (DEC-68(a) structural — no
--                 raw capture exists beyond the closed segment).
--   day_key       the tz-local civil date (DEC-21 wombat_today) ``started_at`` falls on.
--
-- No PRIMARY KEY beyond the surrogate id — append-only, a row per closed segment, purged by age
-- in application code (ObservationStore.prune_older_than), never here. Indexed on (channel,
-- started_at) for windowed per-channel reads and on started_at alone for the age-based prune.

CREATE TABLE IF NOT EXISTS wombat_observations (
    id BIGSERIAL PRIMARY KEY,
    channel TEXT NOT NULL,
    kind TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL,
    ended_at TIMESTAMPTZ NOT NULL,
    payload JSONB NOT NULL,
    day_key DATE NOT NULL
);

CREATE INDEX IF NOT EXISTS wombat_observations_channel_started_at_idx
    ON wombat_observations (channel, started_at);

CREATE INDEX IF NOT EXISTS wombat_observations_started_at_idx
    ON wombat_observations (started_at);
