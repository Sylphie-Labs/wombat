// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  POLL_TIMEOUT_MS,
  PROBE_TIMEOUT_MS,
  SEND_TIMEOUT_MS,
  VOICE_TURNS_TIMEOUT_MS,
  getVoiceTurns,
  pollChatReply,
  probeChat,
  sendChat,
} from "./chat";

// surface.py's CHAT_REPLY_TIMEOUT_SECONDS (src/wombat/chat/surface.py:64),
// mirrored as a literal here (out of scope to import across the app/runtime
// boundary) - SEND_TIMEOUT_MS must stay strictly greater than this in ms.
const SURFACE_HELD_TIMEOUT_MS = 30_000;

/**
 * TK-223 AC1/AC2/AC3: sendChat against a fake window.wombatChat bridge +
 * fake fetch, speaking surface.py's verbatim wire shapes. The live round
 * trip through the real runtime is TK-201's launch-doc bring-up step.
 */

const PORT = 45123;
const TOKEN = "chat-test-token";

function stubBridge(info: { port: number; token: string } | null): void {
  (window as unknown as { wombatChat: { getInfo: () => Promise<unknown> } }).wombatChat = {
    getInfo: vi.fn().mockResolvedValue(info),
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

/**
 * TK-266: a fetch stand-in for the ISS-19 crash class - it never settles on
 * its own, only rejecting with the real `AbortError` `fetch` itself would
 * produce once its passed `AbortSignal` fires.
 */
function neverSettlingFetch(): ReturnType<typeof vi.fn> {
  return vi.fn().mockImplementation((_url: string, init?: RequestInit) => {
    return new Promise((_resolve, reject) => {
      const onAbort = (): void => reject(new DOMException("The operation was aborted.", "AbortError"));
      if (init?.signal?.aborted) {
        onAbort();
        return;
      }
      init?.signal?.addEventListener("abort", onAbort);
    });
  });
}

describe("sendChat", () => {
  it("returns unavailable when the bridge resolves null (no handshake / chat disabled)", async () => {
    stubBridge(null);
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    const result = await sendChat("hello");

    expect(result).toEqual({ kind: "unavailable" });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("returns replied on surface.py's verbatim replied shape", async () => {
    stubBridge({ port: PORT, token: TOKEN });
    const fetchMock = vi.fn().mockResolvedValue(
      Response.json({ status: "replied", text: "hi there" }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const result = await sendChat("hello");

    expect(result).toEqual({ kind: "replied", text: "hi there" });
  });

  it("returns held on surface.py's verbatim held shape", async () => {
    stubBridge({ port: PORT, token: TOKEN });
    const fetchMock = vi.fn().mockResolvedValue(
      Response.json({ status: "held", id: "item-held-1" }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const result = await sendChat("hello");

    expect(result).toEqual({ kind: "held", id: "item-held-1" });
  });

  it("returns unavailable on a non-200 response (e.g. a 401)", async () => {
    stubBridge({ port: PORT, token: TOKEN });
    const fetchMock = vi.fn().mockResolvedValue(new Response(null, { status: 401 }));
    vi.stubGlobal("fetch", fetchMock);

    const result = await sendChat("hello");

    expect(result).toEqual({ kind: "unavailable" });
  });

  it("returns unavailable when fetch rejects (dead port / network error)", async () => {
    stubBridge({ port: PORT, token: TOKEN });
    const fetchMock = vi.fn().mockRejectedValue(new Error("ECONNREFUSED"));
    vi.stubGlobal("fetch", fetchMock);

    const result = await sendChat("hello");

    expect(result).toEqual({ kind: "unavailable" });
  });

  it("derives the URL solely from the bridge port and sends the token only in the header", async () => {
    stubBridge({ port: PORT, token: TOKEN });
    const fetchMock = vi.fn().mockResolvedValue(
      Response.json({ status: "held", id: "item-held-2" }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await sendChat("hello");

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(`http://127.0.0.1:${PORT}/chat`);
    expect(url).not.toContain(TOKEN);
    const headers = new Headers(init.headers);
    expect(headers.get("X-Wombat-Chat-Token")).toBe(TOKEN);
    expect(JSON.parse(init.body as string)).toEqual({ text: "hello" });
  });

  it("queries the bridge fresh on every call (no caching, unlike api.ts)", async () => {
    const getInfo = vi.fn().mockResolvedValue({ port: PORT, token: TOKEN });
    (window as unknown as { wombatChat: { getInfo: () => Promise<unknown> } }).wombatChat = {
      getInfo,
    };
    const fetchMock = vi.fn().mockImplementation(() =>
      Promise.resolve(Response.json({ status: "held", id: "item-held-3" })),
    );
    vi.stubGlobal("fetch", fetchMock);

    await sendChat("first");
    await sendChat("second");

    expect(getInfo).toHaveBeenCalledTimes(2);
  });
});

describe("sendChat timeout (TK-266 / ISS-19)", () => {
  it("SEND_TIMEOUT_MS is strictly greater than surface.py's 30s held-window", () => {
    expect(SEND_TIMEOUT_MS).toBeGreaterThan(SURFACE_HELD_TIMEOUT_MS);
  });

  it("aborts a never-settling fetch at SEND_TIMEOUT_MS and returns timed_out", async () => {
    vi.useFakeTimers();
    stubBridge({ port: PORT, token: TOKEN });
    vi.stubGlobal("fetch", neverSettlingFetch());

    const pending = sendChat("hello");
    await vi.advanceTimersByTimeAsync(SEND_TIMEOUT_MS);
    const result = await pending;

    expect(result).toEqual({ kind: "timed_out" });
  });
});

describe("pollChatReply (TK-270 / DEC-56(b))", () => {
  it("returns pending on surface.py's verbatim pending shape", async () => {
    stubBridge({ port: PORT, token: TOKEN });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json({ status: "pending" })));

    expect(await pollChatReply("item-abc")).toEqual({ kind: "pending" });
  });

  it("returns replied+text on surface.py's verbatim replied shape", async () => {
    stubBridge({ port: PORT, token: TOKEN });
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(Response.json({ status: "replied", text: "sorry, late" })),
    );

    expect(await pollChatReply("item-abc")).toEqual({ kind: "replied", text: "sorry, late" });
  });

  it("returns unavailable when the bridge resolves null", async () => {
    stubBridge(null);
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    expect(await pollChatReply("item-abc")).toEqual({ kind: "unavailable" });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("hits GET /chat/reply/<id> with the token only in the header, never the URL", async () => {
    stubBridge({ port: PORT, token: TOKEN });
    const fetchMock = vi.fn().mockResolvedValue(Response.json({ status: "pending" }));
    vi.stubGlobal("fetch", fetchMock);

    await pollChatReply("item-abc");

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(`http://127.0.0.1:${PORT}/chat/reply/item-abc`);
    expect(url).not.toContain(TOKEN);
    const headers = new Headers(init.headers);
    expect(headers.get("X-Wombat-Chat-Token")).toBe(TOKEN);
  });

  it("aborts a never-settling fetch at POLL_TIMEOUT_MS and returns unavailable", async () => {
    vi.useFakeTimers();
    stubBridge({ port: PORT, token: TOKEN });
    vi.stubGlobal("fetch", neverSettlingFetch());

    const pending = pollChatReply("item-abc");
    await vi.advanceTimersByTimeAsync(POLL_TIMEOUT_MS);
    const result = await pending;

    expect(result).toEqual({ kind: "unavailable" });
  });
});

describe("getVoiceTurns (TK-281 / DEC-60c app half)", () => {
  it("returns unavailable when the bridge resolves null", async () => {
    stubBridge(null);
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    expect(await getVoiceTurns()).toEqual({ kind: "unavailable" });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("parses surface.py's verbatim voice-turns array shape", async () => {
    stubBridge({ port: PORT, token: TOKEN });
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        Response.json([
          { id: "v1", transcript: "hey wombat", captured_at: "2026-07-20T00:00:00Z", reply: null },
          { id: "v2", transcript: "what's the weather", captured_at: "2026-07-20T00:01:00Z", reply: "sunny" },
        ]),
      ),
    );

    expect(await getVoiceTurns()).toEqual({
      kind: "ok",
      turns: [
        { id: "v1", transcript: "hey wombat", capturedAt: "2026-07-20T00:00:00Z", reply: null },
        { id: "v2", transcript: "what's the weather", capturedAt: "2026-07-20T00:01:00Z", reply: "sunny" },
      ],
    });
  });

  it("returns unavailable on a malformed (non-array) body", async () => {
    stubBridge({ port: PORT, token: TOKEN });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json({ oops: true })));

    expect(await getVoiceTurns()).toEqual({ kind: "unavailable" });
  });

  it("returns unavailable on a non-200 response", async () => {
    stubBridge({ port: PORT, token: TOKEN });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(null, { status: 401 })));

    expect(await getVoiceTurns()).toEqual({ kind: "unavailable" });
  });

  it("returns unavailable when fetch rejects", async () => {
    stubBridge({ port: PORT, token: TOKEN });
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("ECONNREFUSED")));

    expect(await getVoiceTurns()).toEqual({ kind: "unavailable" });
  });

  it("hits GET /chat/voice-turns with the token only in the header, never the URL", async () => {
    stubBridge({ port: PORT, token: TOKEN });
    const fetchMock = vi.fn().mockResolvedValue(Response.json([]));
    vi.stubGlobal("fetch", fetchMock);

    await getVoiceTurns();

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(`http://127.0.0.1:${PORT}/chat/voice-turns`);
    expect(url).not.toContain(TOKEN);
    const headers = new Headers(init.headers);
    expect(headers.get("X-Wombat-Chat-Token")).toBe(TOKEN);
  });

  it("aborts a never-settling fetch at VOICE_TURNS_TIMEOUT_MS and returns unavailable", async () => {
    vi.useFakeTimers();
    stubBridge({ port: PORT, token: TOKEN });
    vi.stubGlobal("fetch", neverSettlingFetch());

    const pending = getVoiceTurns();
    await vi.advanceTimersByTimeAsync(VOICE_TURNS_TIMEOUT_MS);

    expect(await pending).toEqual({ kind: "unavailable" });
  });

  it("aborts the in-flight fetch immediately when the caller's externalSignal aborts", async () => {
    stubBridge({ port: PORT, token: TOKEN });
    vi.stubGlobal("fetch", neverSettlingFetch());
    const externalController = new AbortController();

    const pending = getVoiceTurns(externalController.signal);
    externalController.abort();

    expect(await pending).toEqual({ kind: "unavailable" });
  });
});

describe("probeChat timeout (TK-266 / ISS-19)", () => {
  it("aborts a never-settling probe fetch at PROBE_TIMEOUT_MS and reports not-alive", async () => {
    vi.useFakeTimers();
    stubBridge({ port: PORT, token: TOKEN });
    vi.stubGlobal("fetch", neverSettlingFetch());

    const pending = probeChat();
    await vi.advanceTimersByTimeAsync(PROBE_TIMEOUT_MS);
    const alive = await pending;

    expect(alive).toBe(false);
  });
});
