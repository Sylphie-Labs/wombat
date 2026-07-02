// Chunk: split unprocessed `raw` rows into `raw_chunks` (deterministic, no model).
// Runs chained after capture in the Stop hook, and standalone: `node memory/chunk.js`.

const { pool } = require("./lib/db");
const { chunkText } = require("./lib/splitter");

async function chunk(opts = {}) {
  const limit = opts.limit || 2000;
  const client = await pool.connect();
  let rawProcessed = 0, chunksInserted = 0;
  try {
    const { rows } = await client.query(
      `SELECT id, session_id, ts, content FROM raw WHERE NOT has_processed ORDER BY id LIMIT $1`,
      [limit]
    );
    for (const r of rows) {
      const pieces = chunkText(r.content);
      await client.query("BEGIN");
      try {
        for (let i = 0; i < pieces.length; i++) {
          const res = await client.query(
            `INSERT INTO raw_chunks(raw_id, session_id, ts, chunk_index, chunk_text)
             VALUES ($1,$2,$3,$4,$5)
             ON CONFLICT (raw_id, chunk_index) DO NOTHING`,
            [r.id, r.session_id, r.ts, i, pieces[i]]
          );
          chunksInserted += res.rowCount;
        }
        await client.query(`UPDATE raw SET has_processed = true WHERE id = $1`, [r.id]);
        await client.query("COMMIT");
        rawProcessed++;
      } catch (e) {
        await client.query("ROLLBACK");
        throw e;
      }
    }
  } finally {
    client.release();
  }
  return { rawProcessed, chunksInserted };
}

module.exports = { chunk };

if (require.main === module) {
  chunk()
    .then((r) => { console.log("[chunk] " + JSON.stringify(r)); return pool.end(); })
    .catch((e) => { console.error("[chunk] " + ((e && e.stack) || e)); process.exit(1); });
}
