// Embed: vectorize `raw_chunks` rows in place (WHERE NOT is_vectored) via Ollama.
// Chained after chunk in the Stop hook, and standalone: `node memory/embed.js`.

const { pool } = require("./lib/db");
const { embed, toVectorLiteral } = require("./lib/ollama");

const BATCH = parseInt(process.env.WOMBAT_MEMORY_EMBED_BATCH || "64", 10);

async function embedPending(opts = {}) {
  const max = opts.limit || 100000;
  const client = await pool.connect();
  let vectored = 0;
  try {
    while (vectored < max) {
      const { rows } = await client.query(
        `SELECT id, chunk_text FROM raw_chunks WHERE NOT is_vectored ORDER BY id LIMIT $1`,
        [Math.min(BATCH, max - vectored)]
      );
      if (!rows.length) break;
      const vecs = await embed(rows.map((r) => r.chunk_text));
      for (let i = 0; i < rows.length; i++) {
        await client.query(
          `UPDATE raw_chunks SET embedding = $1::vector, is_vectored = true WHERE id = $2`,
          [toVectorLiteral(vecs[i]), rows[i].id]
        );
        vectored++;
      }
    }
  } finally {
    client.release();
  }
  return { vectored };
}

module.exports = { embedPending };

if (require.main === module) {
  embedPending()
    .then((r) => { console.log("[embed] " + JSON.stringify(r)); return pool.end(); })
    .catch((e) => { console.error("[embed] " + ((e && e.stack) || e)); process.exit(1); });
}
