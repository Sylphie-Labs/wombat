#!/usr/bin/env node
/**
 * PreToolUse hook -> surface already-derived findings before the agent re-derives them.
 * Emits hookSpecificOutput.additionalContext on a hit; silent on a miss.
 * Fail-open: any error / Ollama-down / timeout returns no output and never blocks the tool.
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
    const { recall } = require(path.join(memDir, "recall.js"));
    const ctx = await recall(payload);
    if (ctx) {
      process.stdout.write(
        JSON.stringify({
          hookSpecificOutput: { hookEventName: "PreToolUse", additionalContext: ctx },
        })
      );
    }
  } catch (e) {
    if (process.env.WOMBAT_MEMORY_DEBUG) {
      process.stderr.write("[memory-recall] " + ((e && e.stack) || e) + "\n");
    }
  }
  process.exit(0);
});
