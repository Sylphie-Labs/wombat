import { readFileSync } from "node:fs";
import path from "node:path";

import { resolveBackendRoot, resolveOperatorSetting } from "./env-config";

/**
 * TK-223 (Q-111(a) ruled shape): reads the TK-222 runtime chat handshake -
 * the ONE `{"port": <int>, "token": <str>}` JSON line the running
 * `wombat.runtime` writes to the path named by the operator-tier
 * `WOMBAT_CHAT_HANDSHAKE_FILE` setting (deliberately NOT app-editable - chat
 * is disabled iff that setting is unset, mirroring `bootstrap.assemble_runtime`).
 */

const HANDSHAKE_ENV_VAR = "WOMBAT_CHAT_HANDSHAKE_FILE";

export interface ChatHandshakeInfo {
  readonly port: number;
  readonly token: string;
}

/**
 * Parses the handshake file's contents. `null` on ANY malformed shape -
 * bad JSON, a non-object, a non-integer port, or a missing/empty token -
 * the SAME null-on-anything-malformed discipline as `api-process.ts`'s
 * `parseHandshake`. Never throws.
 */
export function parseChatHandshake(contents: string): ChatHandshakeInfo | null {
  let parsed: unknown;
  try {
    parsed = JSON.parse(contents);
  } catch {
    return null;
  }
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    return null;
  }
  const { port, token } = parsed as Record<string, unknown>;
  if (!Number.isInteger(port)) {
    return null;
  }
  if (typeof token !== "string" || token.length === 0) {
    return null;
  }
  return { port: port as number, token };
}

/**
 * Resolves `WOMBAT_CHAT_HANDSHAKE_FILE` (env, else the backend root's
 * `.env`, else unset) - a relative path resolves against the backend root -
 * then reads and parses it. `null` on an unset var, an unreadable file, or a
 * malformed body; never throws. Callers (`main.ts`'s `wombat:chat-info`
 * handler) re-resolve and re-read on EVERY call - no caching, since a
 * runtime can start/stop independently of the app.
 */
export function readChatInfo(
  env: NodeJS.ProcessEnv,
  appPath: string,
): ChatHandshakeInfo | null {
  const backendRoot = resolveBackendRoot(env, appPath);
  const configuredPath = resolveOperatorSetting(HANDSHAKE_ENV_VAR, env, backendRoot);
  if (configuredPath === null || configuredPath.trim() === "") {
    return null;
  }
  const handshakePath = path.resolve(backendRoot, configuredPath.trim());

  let contents: string;
  try {
    contents = readFileSync(handshakePath, "utf-8");
  } catch {
    return null;
  }
  return parseChatHandshake(contents);
}
