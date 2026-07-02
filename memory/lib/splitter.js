// Deterministic sentence / short-window splitter.
// SHARED by the store side (chunking raw -> raw_chunks) and the query side (recall hook),
// so both probe at the same granularity. Target ~15-40 words/chunk; merge fragments under
// MIN_WORDS into a neighbor; window any sentence over HARD_MAX words with OVERLAP.

const TARGET_MAX = parseInt(process.env.WOMBAT_MEMORY_CHUNK_MAX_WORDS || "40", 10);
const MIN_WORDS  = parseInt(process.env.WOMBAT_MEMORY_CHUNK_MIN_WORDS || "6", 10);
const HARD_MAX   = parseInt(process.env.WOMBAT_MEMORY_CHUNK_HARD_MAX  || "45", 10);
const OVERLAP    = parseInt(process.env.WOMBAT_MEMORY_CHUNK_OVERLAP   || "10", 10);

function wordCount(s) {
  return String(s || "").split(/\s+/).filter(Boolean).length;
}

// Newlines first, then sentence terminators (. ? ! ;), keeping the delimiter.
function splitSentences(text) {
  const out = [];
  for (const line of String(text || "").split(/\r?\n/)) {
    const t = line.trim();
    if (!t) continue;
    for (const p of t.split(/(?<=[.?!;])\s+/)) {
      const s = p.trim();
      if (s) out.push(s);
    }
  }
  return out;
}

// Slide a window over an over-long sentence so a key phrase isn't severed at a boundary.
function windowLong(sentence) {
  const words = sentence.split(/\s+/).filter(Boolean);
  if (words.length <= HARD_MAX) return [sentence];
  const pieces = [];
  for (let i = 0; i < words.length; i += HARD_MAX - OVERLAP) {
    pieces.push(words.slice(i, i + HARD_MAX).join(" "));
    if (i + HARD_MAX >= words.length) break;
  }
  return pieces;
}

// Split text into chunks. Packs consecutive short sentences toward TARGET_MAX words,
// then folds any sub-MIN_WORDS trailing fragment back into the previous chunk.
function chunkText(text) {
  const sentences = splitSentences(text).flatMap(windowLong);
  const chunks = [];
  let buf = "", bufW = 0;
  const flush = () => { if (buf) { chunks.push(buf); buf = ""; bufW = 0; } };

  for (const s of sentences) {
    const w = wordCount(s);
    if (bufW === 0)              { buf = s; bufW = w; }
    else if (bufW + w <= TARGET_MAX) { buf += " " + s; bufW += w; }
    else                        { flush(); buf = s; bufW = w; }
    if (bufW >= TARGET_MAX) flush();
  }
  flush();

  const merged = [];
  for (const c of chunks) {
    if (merged.length && wordCount(c) < MIN_WORDS) merged[merged.length - 1] += " " + c;
    else merged.push(c);
  }
  return merged.map((c) => c.trim()).filter(Boolean);
}

module.exports = { chunkText, splitSentences, wordCount };
