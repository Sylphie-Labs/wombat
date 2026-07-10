// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";

import { sendChat } from "./chat";

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
});

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
    const fetchMock = vi.fn().mockResolvedValue(Response.json({ status: "held" }));
    vi.stubGlobal("fetch", fetchMock);

    const result = await sendChat("hello");

    expect(result).toEqual({ kind: "held" });
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
    const fetchMock = vi.fn().mockResolvedValue(Response.json({ status: "held" }));
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
      Promise.resolve(Response.json({ status: "held" })),
    );
    vi.stubGlobal("fetch", fetchMock);

    await sendChat("first");
    await sendChat("second");

    expect(getInfo).toHaveBeenCalledTimes(2);
  });
});
