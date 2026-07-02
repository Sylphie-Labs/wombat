// Ollama embedding helper. SHARED by the embed CLI (store side) and the recall hook (query side),
// so both vectorize identically. Batches multiple inputs in one /api/embed call (nearly free).

const OLLAMA_URL  = process.env.WOMBAT_MEMORY_OLLAMA_URL  || "http://localhost:11434";
const EMBED_MODEL = process.env.WOMBAT_MEMORY_EMBED_MODEL || "nomic-embed-text";
// Keep the model resident so the hot path stays ~50-100ms instead of ~1.5s cold. -1 = forever.
const KEEP_ALIVE  = process.env.WOMBAT_MEMORY_KEEP_ALIVE  || "30m";

async function embed(inputs, { timeoutMs = 20000 } = {}) {
  const arr = Array.isArray(inputs) ? inputs : [inputs];
  if (!arr.length) return [];
  const ctrl = new AbortController();
  const to = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const r = await fetch(OLLAMA_URL + "/api/embed", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ model: EMBED_MODEL, input: arr, keep_alive: KEEP_ALIVE }),
      signal: ctrl.signal,
    });
    if (!r.ok) throw new Error("ollama embed HTTP " + r.status);
    const j = await r.json();
    if (!j.embeddings || j.embeddings.length !== arr.length) {
      throw new Error("ollama embed returned " + (j.embeddings ? j.embeddings.length : 0) + " of " + arr.length);
    }
    return j.embeddings;
  } finally {
    clearTimeout(to);
  }
}

function toVectorLiteral(v) {
  return "[" + v.join(",") + "]";
}

module.exports = { embed, toVectorLiteral, EMBED_MODEL, OLLAMA_URL };
