/**
 * TK-223 (Q-111(a) ruled shape): `sendChat` rides the TK-222 runtime chat
 * handshake, reached exclusively via `window.wombatChat.getInfo()` (the
 * preload contextBridge - never a URL parameter). Unlike `api.ts`'s cached
 * settings bridge, the bridge is queried FRESH on every send: the runtime's
 * lifecycle is independent of the app's, so a stale cached `null` would
 * wrongly pin the degraded state past a runtime that has since come up (and
 * a cached live value would outlive a runtime that has since died).
 *
 * `surface.py`'s wire (`POST /chat`, `X-Wombat-Chat-Token` header, body
 * `{"text": <str>}`) answers `{"status": "replied", "text": <str>}` or, after
 * its honest 30s timeout, `{"status": "held"}`; anything else (no handshake,
 * a network failure, a non-200, an unrecognized body shape) collapses to the
 * SAME closed `unavailable` result - the pane never has to distinguish why.
 */

export interface ChatBridgeInfo {
  readonly port: number;
  readonly token: string;
}

declare global {
  interface Window {
    wombatChat: {
      getInfo(): Promise<ChatBridgeInfo | null>;
    };
  }
}

export type ChatResult =
  | { readonly kind: "replied"; readonly text: string }
  | { readonly kind: "held" }
  | { readonly kind: "unavailable" };

/**
 * TK-263 (ISS-16): the header's liveness signal. Handshake-file presence
 * alone (`getInfo() !== null`) only proves the file survived - not that the
 * runtime process behind it is still alive, so this re-reads `getInfo()`
 * FRESH (same freshness discipline as `sendChat`) and then round-trips a
 * lightweight, side-effect-free request to the bridge-supplied loopback
 * port. ANY HTTP response - including a 404 or 405 - proves the process is
 * answering and counts as alive; a thrown fetch (connection refused, etc.)
 * means the runtime is dead. This deliberately never hits `POST /chat` (that
 * would enqueue a chat turn as a side effect of a status probe) and the
 * token never rides the URL.
 */
export async function probeChat(): Promise<boolean> {
  const info = await window.wombatChat.getInfo();
  if (info === null) {
    return false;
  }

  try {
    await fetch(`http://127.0.0.1:${info.port}/`);
    return true;
  } catch {
    return false;
  }
}

export async function sendChat(text: string): Promise<ChatResult> {
  const info = await window.wombatChat.getInfo();
  if (info === null) {
    return { kind: "unavailable" };
  }

  let response: Response;
  try {
    // The loopback origin is derived entirely from the bridge-supplied port;
    // the token rides ONLY the X-Wombat-Chat-Token header, never the URL.
    response = await fetch(`http://127.0.0.1:${info.port}/chat`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Wombat-Chat-Token": info.token,
      },
      body: JSON.stringify({ text }),
    });
  } catch {
    return { kind: "unavailable" };
  }

  if (!response.ok) {
    return { kind: "unavailable" };
  }

  const body = (await response.json()) as { status?: unknown; text?: unknown };
  if (body.status === "replied" && typeof body.text === "string") {
    return { kind: "replied", text: body.text };
  }
  if (body.status === "held") {
    return { kind: "held" };
  }
  return { kind: "unavailable" };
}
