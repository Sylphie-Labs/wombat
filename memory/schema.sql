-- wombat agent-memory schema (Postgres + pgvector)
--
-- Pipeline:
--   Stop hook ──► raw ──► chunking skill ──► raw_chunks ──► embed CLI (in place) ──► PreToolUse recall
--
-- Idempotent: safe to re-run. Embedding model = Ollama nomic-embed-text (768-dim).

CREATE EXTENSION IF NOT EXISTS vector;

-- 1. raw -- append-style capture: ONE ROW PER TRANSCRIPT MESSAGE (not whole-turn blobs).
--    The Stop hook drains new transcript lines past a byte cursor into here.
CREATE TABLE IF NOT EXISTS raw (
    id              BIGSERIAL   PRIMARY KEY,
    session_id      TEXT        NOT NULL,
    transcript_uuid TEXT        NOT NULL,           -- message uuid from the transcript
    parent_uuid     TEXT,                           -- chain link (transcript parentUuid)
    ts              TIMESTAMPTZ NOT NULL,            -- message timestamp
    role            TEXT        NOT NULL,            -- user | assistant | tool_use | tool_result | thinking
    content         TEXT        NOT NULL,            -- message text / tool payload
    cwd             TEXT,                            -- project context at capture time
    has_processed   BOOLEAN     NOT NULL DEFAULT false,  -- chunking skill consumed this row
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (session_id, transcript_uuid)            -- re-running Stop never duplicates a message
);

-- chunking skill drains: WHERE NOT has_processed
CREATE INDEX IF NOT EXISTS idx_raw_unprocessed ON raw (id) WHERE NOT has_processed;

-- 2. raw_chunks -- sentence / short-window splits of raw, with the vector IN THE SAME ROW.
--    "RAG store" == this table + the hnsw index. No separate vector store.
CREATE TABLE IF NOT EXISTS raw_chunks (
    id            BIGSERIAL   PRIMARY KEY,
    raw_id        BIGINT      NOT NULL REFERENCES raw(id) ON DELETE CASCADE,  -- provenance
    session_id    TEXT        NOT NULL,
    ts            TIMESTAMPTZ NOT NULL,             -- inherited from raw, for recency ranking
    chunk_index   INT         NOT NULL,             -- order within the raw row
    chunk_text    TEXT        NOT NULL,             -- ~15-40 words (one clause/sentence)
    embedding     vector(768),                      -- nomic-embed-text; NULL until vectored
    has_processed BOOLEAN     NOT NULL DEFAULT false,
    is_vectored   BOOLEAN     NOT NULL DEFAULT false,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (raw_id, chunk_index)
);

-- embed CLI drains: WHERE NOT is_vectored
CREATE INDEX IF NOT EXISTS idx_chunks_unvectored ON raw_chunks (id) WHERE NOT is_vectored;

-- recency / per-session filters
CREATE INDEX IF NOT EXISTS idx_chunks_session_ts ON raw_chunks (session_id, ts DESC);

-- ANN recall (cosine distance). Built now; NULL embeddings are skipped until vectored.
CREATE INDEX IF NOT EXISTS idx_chunks_embedding ON raw_chunks
    USING hnsw (embedding vector_cosine_ops);
