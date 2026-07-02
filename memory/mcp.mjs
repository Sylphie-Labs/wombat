#!/usr/bin/env node
/**
 * wombat-memory MCP server (stdio).
 * Exposes `search_memory` so the agent can check what it already derived
 * BEFORE re-deriving it — a reliable, on-demand complement to the PreToolUse hook.
 */
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { ListToolsRequestSchema, CallToolRequestSchema } from "@modelcontextprotocol/sdk/types.js";
import { createRequire } from "module";
import { fileURLToPath } from "url";
import { dirname, join } from "path";

const require = createRequire(import.meta.url);
const __dirname = dirname(fileURLToPath(import.meta.url));
const { searchText } = require(join(__dirname, "lib", "search.js"));

const TOOL = {
  name: "search_memory",
  description:
    "Search wombat's agent-memory — findings and derivations from earlier work — by semantic " +
    "similarity. Call this BEFORE investigating or deriving something, to check whether you " +
    "already figured it out. Returns ranked memory chunks with cosine-similarity scores; treat " +
    "them as past notes to verify against current code, not as ground truth.",
  inputSchema: {
    type: "object",
    properties: {
      query: { type: "string", description: "What you're about to investigate or derive, as a question or topic." },
      limit: { type: "number", description: "Max results (default 5)." },
      min_similarity: { type: "number", description: "Minimum cosine similarity 0-1 (default 0; raise to filter weak matches)." },
    },
    required: ["query"],
  },
};

const server = new Server({ name: "wombat-memory", version: "0.1.0" }, { capabilities: { tools: {} } });

server.setRequestHandler(ListToolsRequestSchema, async () => ({ tools: [TOOL] }));

server.setRequestHandler(CallToolRequestSchema, async (req) => {
  if (req.params.name !== "search_memory") {
    return { content: [{ type: "text", text: "unknown tool: " + req.params.name }], isError: true };
  }
  const a = req.params.arguments || {};
  try {
    const results = await searchText(a.query, {
      limit: a.limit || 5,
      minSim: a.min_similarity != null ? a.min_similarity : 0,
    });
    if (!results.length) {
      return { content: [{ type: "text", text: "No related memories found." }] };
    }
    const lines = results.map((r) => {
      const day = new Date(r.ts).toISOString().slice(0, 10);
      return `- (${r.sim.toFixed(2)}) [${day}] ${r.text}`;
    });
    return {
      content: [{ type: "text", text: "Related memories (past notes — verify before relying):\n" + lines.join("\n") }],
    };
  } catch (e) {
    return { content: [{ type: "text", text: "search_memory error: " + ((e && e.message) || e) }], isError: true };
  }
});

await server.connect(new StdioServerTransport());
