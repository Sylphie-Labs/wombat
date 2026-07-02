// Recall: before a tool runs, surface findings already derived in past work.
// Query = "what I'm about to do" (the tool call) + "why" (latest assistant text in the
// transcript tail). Split with the SHARED splitter, batch-embed, multi-probe cosine search.

const fs = require("fs");
const { pool } = require("./lib/db");
const { embed, toVectorLiteral } = require("./lib/ollama");
const { chunkText } = require("./lib/splitter");

const THRESHOLD  = parseFloat(process.env.WOMBAT_MEMORY_RECALL_THRESHOLD || "0.7");
const TOPK_PROBE = parseInt(process.env.WOMBAT_MEMORY_RECALL_TOPK_PROBE || "3", 10);
const MAX_RESULTS = parseInt(process.env.WOMBAT_MEMORY_RECALL_MAX || "5", 10);

// Pull the most recent assistant text block from the tail of the transcript (the "why").
function latestAssistantText(transcriptPath, maxChars = 1200) {
  try {
    if (!transcriptPath || !fs.existsSync(transcriptPath)) return "";
    const stat = fs.statSync(transcriptPath);
    const tail = Math.min(stat.size, 65536);
    const fd = fs.openSync(transcriptPath, "r");
    const buf = Buffer.alloc(tail);
    fs.readSync(fd, buf, 0, tail, stat.size - tail);
    fs.closeSync(fd);
    let lines = buf.toString("utf8").split("\n");
    if (stat.size > tail) lines = lines.slice(1); // drop the partial first line
    for (let i = lines.length - 1; i >= 0; i--) {
      const t = lines[i].trim();
      if (!t) continue;
      let o;
      try { o = JSON.parse(t); } catch { continue; }
      if (o.type !== "assistant") continue;
      const c = o.message && o.message.content;
      if (Array.isArray(c)) {
        const txt = c.filter((b) => b && b.type === "text" && b.text).map((b) => b.text).join(" ").trim();
        if (txt) return txt.slice(0, maxChars);
      } else if (typeof c === "string" && c.trim()) {
        return c.slice(0, maxChars);
      }
    }
  } catch {
    /* fail open */
  }
  return "";
}

// "what I'm doing & why" from the PreToolUse payload.
function buildQueryText(payload) {
  const parts = [];
  const ti = payload.tool_input || {};
  const salient = [ti.pattern, ti.query, ti.file_path, ti.command, ti.url, ti.description, ti.prompt, ti.glob, ti.path]
    .filter(Boolean)
    .join(" ");
  const what = `${payload.tool_name || "tool"} ${salient}`.trim();
  if (what) parts.push(what);
  const why = latestAssistantText(payload.transcript_path);
  if (why) parts.push(why);
  return parts.join("\n").trim();
}

async function recall(payload) {
  const qtext = buildQueryText(payload);
  if (!qtext || qtext.length < 8) return null;
  const probes = chunkText(qtext);
  if (!probes.length) return null;

  const vecs = await embed(probes);
  const found = new Map(); // chunk id -> best {sim, text, ts, session}
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
        [lit, TOPK_PROBE]
      );
      for (const r of rows) {
        const sim = Number(r.sim);
        if (sim < THRESHOLD) continue;
        const cur = found.get(r.id);
        if (!cur || sim > cur.sim) found.set(r.id, { sim, text: r.chunk_text, ts: r.ts, session: r.session_id });
      }
    }
  } finally {
    client.release();
  }

  if (!found.size) return null;
  const top = [...found.values()].sort((a, b) => b.sim - a.sim).slice(0, MAX_RESULTS);
  const lines = top.map((it) => `- (${it.sim.toFixed(2)}) ${it.text}`);
  return (
    "<wombat-memory>\nYou may have already derived these in earlier work — check before re-deriving:\n" +
    lines.join("\n") +
    "\n</wombat-memory>"
  );
}

module.exports = { recall, buildQueryText, latestAssistantText };
