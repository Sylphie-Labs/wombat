# wombat agent-memory

A custom, local, model-light memory for the agent building wombat: capture what the agent derives, and
surface it again *before it re-derives it*.

## Pipeline

```
Stop hook ──► raw            one row per transcript MESSAGE (append-style), has_processed flag
  skill   ──► raw_chunks     sentence/short-window splits (~15-40 words), has_processed + is_vectored
  CLI     ──► (in place)     embed chunks WHERE NOT is_vectored via Ollama nomic-embed-text (768-dim)
PreToolUse ─► recall         split latest "what I'm doing & why" → batched embed → cosine search
                             raw_chunks → inject a message of already-derived findings
```

- **Store:** one Postgres + `pgvector`. The "RAG store" is `raw_chunks` + the HNSW index — no separate
  vector store; `is_vectored` just means the embedding column is populated.
- **Embeddings:** Ollama `nomic-embed-text` (local, free, 768-dim, cosine). Keep it resident
  (`keep_alive`) so the PreToolUse hot path stays in the ~50-100 ms band instead of ~1.5 s cold.

## Run

```bash
docker compose -f memory/docker-compose.yml up -d     # Postgres on localhost:5544
# schema.sql auto-applies on first init; to (re)apply by hand:
docker exec -i wombat-memory-db psql -U wombat -d wombat_memory < memory/schema.sql
```

Connection: `postgresql://wombat:wombat-memory-local@localhost:5544/wombat_memory`
