#!/usr/bin/env node
/**
 * Stop hook -> append new transcript messages into the `raw` table.
 * Fail-open: any error is swallowed so capture never blocks the turn.
 * Set WOMBAT_MEMORY_DEBUG=1 to surface errors on stderr.
 */
const path = require("path");

let input = "";
process.stdin.on("data", (c) => (input += c));
process.stdin.on("end", async () => {
  try {
    const payload = JSON.parse(input || "{}");
    const memDir =
      process.env.WOMBAT_MEMORY_DIR ||
      path.join(process.env.CLAUDE_PROJECT_DIR || process.cwd(), "memory");
    const { capture } = require(path.join(memDir, "capture.js"));
    const { chunk } = require(path.join(memDir, "chunk.js"));
    const { embedPending } = require(path.join(memDir, "embed.js"));
    const cap = await capture(payload);
    const ch = await chunk();
    // Embed is best-effort: if Ollama is down, chunks stay is_vectored=false and a later
    // run (Stop or `node memory/embed.js`) backfills them. Never fail the turn over it.
    let em = { vectored: 0 };
    try { em = await embedPending(); } catch (e) {
      if (process.env.WOMBAT_MEMORY_DEBUG) process.stderr.write("[memory-capture] embed skipped: " + (e && e.message) + "\n");
    }
    if (process.env.WOMBAT_MEMORY_DEBUG) {
      process.stderr.write("[memory-capture] capture=" + JSON.stringify(cap) + " chunk=" + JSON.stringify(ch) + " embed=" + JSON.stringify(em) + "\n");
    }
  } catch (e) {
    if (process.env.WOMBAT_MEMORY_DEBUG) {
      process.stderr.write("[memory-capture] " + ((e && e.stack) || e) + "\n");
    }
  }
  process.exit(0);
});
