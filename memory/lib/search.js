// Shared semantic search over raw_chunks. Used by the MCP server (live tool) and
// available to the recall hook. Splits the query with the SHARED splitter, batch-embeds,
// multi-probes cosine, merges by best score per chunk.

const { pool } = require("./db");
const { embed, toVectorLiteral } = require("./ollama");
const { chunkText } = require("./splitter");

async function searchText(query, opts = {}) {
  const limit = opts.limit || 5;
  const minSim = opts.minSim != null ? opts.minSim : 0;
  const topk = opts.topkPerProbe || Math.max(limit, 5);

  const q = String(query || "").trim();
  if (!q) return [];
  const probes = chunkText(q).slice(0, 12);
  const inputs = probes.length ? probes : [q];

  const vecs = await embed(inputs);
  const found = new Map(); // chunk id -> best { sim, text, ts, session }
  const client = await pool.connect();
  try {
    for (const v of vecs) {
      const lit = toVectorLiteral(v);
      const { rows } = await client.query(
        `SELECT id, chunk_text, ts, session_id, 1 - (embedding <=> $1::vector) AS sim
         FROM raw_chunks
         WHERE is_vectored
         ORDER BY embedding <=> $1::vector
         LIMIT $2`,
        [lit, topk]
      );
      for (const r of rows) {
        const sim = Number(r.sim);
        if (sim < minSim) continue;
        const cur = found.get(r.id);
        if (!cur || sim > cur.sim) {
          found.set(r.id, { sim, text: r.chunk_text, ts: r.ts, session: r.session_id });
        }
      }
    }
  } finally {
    client.release();
  }

  return [...found.values()].sort((a, b) => b.sim - a.sim).slice(0, limit);
}

module.exports = { searchText };
