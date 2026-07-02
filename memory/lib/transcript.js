// Parse Claude Code transcript JSONL into flat `raw` records (one per message line).

const BLOCK_CAP = parseInt(process.env.WOMBAT_MEMORY_BLOCK_CAP || "6000", 10);

function truncate(s, n) {
  s = String(s == null ? "" : s);
  return s.length > n ? s.slice(0, n) + " …[truncated]" : s;
}

// Flatten a message's content (string OR array of blocks) into readable text.
function flattenContent(content) {
  if (content == null) return "";
  if (typeof content === "string") return content.trim();
  if (!Array.isArray(content)) return "";
  const parts = [];
  for (const b of content) {
    if (!b || typeof b !== "object") continue;
    switch (b.type) {
      case "text":        if (b.text) parts.push(b.text); break;
      case "thinking":    if (b.thinking) parts.push(b.thinking); break;   // may be empty (known redaction bug)
      case "tool_use":    parts.push(`[tool_use ${b.name}] ` + truncate(JSON.stringify(b.input || {}), BLOCK_CAP)); break;
      case "tool_result": parts.push(`[tool_result] ` + truncate(flattenContent(b.content), BLOCK_CAP)); break;
      default: break;
    }
  }
  return parts.join("\n").trim();
}

// Parse a slab of transcript text into raw records. `fallback` supplies session_id/cwd
// when a line omits them. Only user/assistant message lines with content are kept.
function parseLines(text, fallback) {
  const records = [];
  for (const line of String(text || "").split("\n")) {
    const t = line.trim();
    if (!t) continue;
    let o;
    try { o = JSON.parse(t); } catch { continue; }
    if (!o || !o.uuid) continue;                                   // need a stable id for idempotency
    if (o.type !== "user" && o.type !== "assistant") continue;     // skip summary/system/meta lines
    const msg = o.message || {};
    const content = flattenContent(msg.content);
    if (!content) continue;
    records.push({
      session_id: o.sessionId || fallback.session_id,
      transcript_uuid: o.uuid,
      parent_uuid: o.parentUuid || null,
      ts: o.timestamp || new Date().toISOString(),
      role: msg.role || o.type,
      content,
      cwd: o.cwd || fallback.cwd || null,
    });
  }
  return records;
}

module.exports = { flattenContent, parseLines, truncate };
