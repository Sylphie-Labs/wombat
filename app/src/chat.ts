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
 * its honest 30s timeout, `{"status": "held", "id": <str>}`; anything else
 * (no handshake, a network failure, a non-200, an unrecognized body shape)
 * collapses to the SAME closed `unavailable` result - the pane never has to
 * distinguish why.
 *
 * TK-266 (ISS-19): if the runtime dies holding the connection, the fetch
 * above never settles on its own - so `sendChat` races it against an
 * `AbortController` timing out at `SEND_TIMEOUT_MS`. An abort is its OWN
 * closed result, `timed_out`, distinct from `unavailable`: the pane needs to
 * tell "the app is honestly down" apart from "we gave up waiting and the
 * message may be lost".
 *
 * TK-270 (DEC-56(b)): a `held` result now carries the surface-minted `id` -
 * the pane polls `pollChatReply(id)` (below) for it to arrive late. `id`
 * never enters any model-visible payload; it is purely a client<->surface
 * correlation handle.
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
  | { readonly kind: "held"; readonly id: string }
  | { readonly kind: "unavailable" }
  | { readonly kind: "timed_out" };

/**
 * TK-266: the send-side abort timeout. MUST stay STRICTLY GREATER than
 * `surface.py`'s `CHAT_REPLY_TIMEOUT_SECONDS` (30s) so a legitimate `held`
 * answer - which surface.py itself only returns after its own 30s wait - is
 * NEVER falsely reported as timed out here.
 */
export const SEND_TIMEOUT_MS = 45_000;

/**
 * TK-266: `probeChat`'s own, much shorter abort timeout - a liveness probe
 * that hangs is itself a dead answer, so it must not wait anywhere near as
 * long as a real chat send.
 */
export const PROBE_TIMEOUT_MS = 5_000;

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

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), PROBE_TIMEOUT_MS);
  try {
    await fetch(`http://127.0.0.1:${info.port}/`, { signal: controller.signal });
    return true;
  } catch {
    // Covers both a thrown network error and an abort (a hung probe is a
    // dead answer) - either way the runtime does not count as alive.
    return false;
  } finally {
    clearTimeout(timer);
  }
}

export async function sendChat(text: string): Promise<ChatResult> {
  const info = await window.wombatChat.getInfo();
  if (info === null) {
    return { kind: "unavailable" };
  }

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), SEND_TIMEOUT_MS);
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
      signal: controller.signal,
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") {
      return { kind: "timed_out" };
    }
    return { kind: "unavailable" };
  } finally {
    clearTimeout(timer);
  }

  if (!response.ok) {
    return { kind: "unavailable" };
  }

  const body = (await response.json()) as { status?: unknown; text?: unknown; id?: unknown };
  if (body.status === "replied" && typeof body.text === "string") {
    return { kind: "replied", text: body.text };
  }
  if (body.status === "held" && typeof body.id === "string") {
    return { kind: "held", id: body.id };
  }
  return { kind: "unavailable" };
}

/**
 * TK-270 (DEC-56(b)): `pollChatReply`'s own abort timeout - a hung poll must
 * abort like any other fetch here (TK-266 discipline), not hang the polling
 * loop.
 */
export const POLL_TIMEOUT_MS = 5_000;

/** TK-270: the interval between successive polls. */
export const POLL_INTERVAL_MS = 5_000;

/**
 * TK-270: the pinned, honest bound on how long the pane keeps polling for a
 * late reply before giving up. A constant, not configurable - past this the
 * pane renders an honest gave-up line rather than polling forever.
 */
export const POLL_GIVE_UP_MS = 12 * 60_000;

export type PollReplyResult =
  | { readonly kind: "replied"; readonly text: string }
  | { readonly kind: "pending" }
  | { readonly kind: "unavailable" };

/**
 * TK-270: `GET /chat/reply/<id>` - answers `{"status": "pending"}` or
 * `{"status": "replied", "text": <str>}` (popped server-side on read).
 * Mirrors `sendChat`'s freshness discipline (`getInfo()` re-read per call,
 * never cached) and its collapse-to-`unavailable` posture for anything else.
 */
export async function pollChatReply(id: string): Promise<PollReplyResult> {
  const info = await window.wombatChat.getInfo();
  if (info === null) {
    return { kind: "unavailable" };
  }

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), POLL_TIMEOUT_MS);
  let response: Response;
  try {
    response = await fetch(`http://127.0.0.1:${info.port}/chat/reply/${id}`, {
      method: "GET",
      headers: {
        "X-Wombat-Chat-Token": info.token,
      },
      signal: controller.signal,
    });
  } catch {
    return { kind: "unavailable" };
  } finally {
    clearTimeout(timer);
  }

  if (!response.ok) {
    return { kind: "unavailable" };
  }

  const body = (await response.json()) as { status?: unknown; text?: unknown };
  if (body.status === "replied" && typeof body.text === "string") {
    return { kind: "replied", text: body.text };
  }
  return { kind: "pending" };
}
