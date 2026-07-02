// Capture: append NEW transcript messages (past a byte cursor) into `raw`.
// Idempotent (UNIQUE session_id+transcript_uuid) and cursor-driven so each Stop
// only processes the lines appended that turn — no re-reading the whole file.

const fs = require("fs");
const path = require("path");
const { pool } = require("./lib/db");
const { parseLines } = require("./lib/transcript");

const CURSOR_DIR =
  process.env.WOMBAT_MEMORY_CURSOR_DIR ||
  path.join(process.env.CLAUDE_PROJECT_DIR || process.cwd(), ".claude", "memory", "cursors");

function readCursor(session) {
  try {
    return JSON.parse(fs.readFileSync(path.join(CURSOR_DIR, session + ".json"), "utf8"));
  } catch {
    return null;
  }
}

function writeCursor(session, data) {
  fs.mkdirSync(CURSOR_DIR, { recursive: true });
  fs.writeFileSync(path.join(CURSOR_DIR, session + ".json"), JSON.stringify(data));
}

async function capture(payload) {
  const transcriptPath = payload.transcript_path;
  const session = payload.session_id || "unknown";
  if (!transcriptPath || !fs.existsSync(transcriptPath)) {
    return { inserted: 0, scanned: 0, reason: "no transcript" };
  }

  const stat = fs.statSync(transcriptPath);
  const cur = readCursor(session);
  // Resume from cursor only if it points at this same file and isn't past EOF (rotation guard).
  let offset = 0;
  if (cur && cur.path === transcriptPath && cur.offset <= stat.size) offset = cur.offset;

  let text = "";
  const len = stat.size - offset;
  if (len > 0) {
    const fd = fs.openSync(transcriptPath, "r");
    try {
      const buf = Buffer.alloc(len);
      fs.readSync(fd, buf, 0, len, offset);
      text = buf.toString("utf8");
    } finally {
      fs.closeSync(fd);
    }
  }

  const records = parseLines(text, { session_id: session, cwd: payload.cwd });
  let inserted = 0;
  if (records.length) {
    const client = await pool.connect();
    try {
      for (const r of records) {
        const res = await client.query(
          `INSERT INTO raw(session_id, transcript_uuid, parent_uuid, ts, role, content, cwd)
           VALUES ($1,$2,$3,$4,$5,$6,$7)
           ON CONFLICT (session_id, transcript_uuid) DO NOTHING`,
          [r.session_id, r.transcript_uuid, r.parent_uuid, r.ts, r.role, r.content, r.cwd]
        );
        inserted += res.rowCount;
      }
    } finally {
      client.release();
    }
  }

  writeCursor(session, { path: transcriptPath, offset: stat.size });
  return { inserted, scanned: records.length };
}

module.exports = { capture };
