// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { resetBridgeCacheForTests } from "../api";
import { InboxHighlights } from "./InboxHighlights";

/**
 * TK-251 AC1/AC3: `GET /external/gmail` client + iteration-4 inbox cards,
 * against a fake bridge + fake fetch (no live API).
 */

const PORT = 41419;
const TOKEN = "test-token";

function installFakeBridge(): void {
  (window as unknown as { wombatSettings: { getInfo: () => Promise<unknown> } }).wombatSettings = {
    getInfo: vi.fn().mockResolvedValue({ port: PORT, token: TOKEN }),
  };
}

function installFetch(body: unknown, status = 200): void {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const headers = new Headers(init?.headers);
    if (headers.get("X-Wombat-Token") !== TOKEN) {
      return new Response(null, { status: 401 });
    }
    expect(String(input)).toContain("/external/gmail");
    return Response.json(body, { status });
  });
  vi.stubGlobal("fetch", fetchMock);
}

beforeEach(() => {
  resetBridgeCacheForTests();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("InboxHighlights (TK-251 AC3: loading/degraded/empty states)", () => {
  it("renders card-shaped skeleton frames while loading", () => {
    installFakeBridge();
    let resolveFetch: (value: Response) => void = () => {};
    vi.stubGlobal(
      "fetch",
      vi.fn(
        () =>
          new Promise<Response>((resolve) => {
            resolveFetch = resolve;
          }),
      ),
    );
    render(<InboxHighlights />);

    expect(screen.getAllByText("Loading inbox…").length).toBeGreaterThanOrEqual(1);
    resolveFetch(Response.json({ items: [], storage_unavailable: false }));
  });

  it("renders the degraded body inside the card shell when storage_unavailable is true", async () => {
    installFakeBridge();
    installFetch({ items: [], storage_unavailable: true });
    render(<InboxHighlights />);

    expect(await screen.findByText(/Unavailable/)).toBeTruthy();
  });

  it("renders the designed empty state on zero items - never blank", async () => {
    installFakeBridge();
    installFetch({ items: [], storage_unavailable: false });
    render(<InboxHighlights />);

    expect(await screen.findByText("Nothing in the inbox needs you.")).toBeTruthy();
  });
});

describe("InboxHighlights (TK-251 AC1: inbox cards from the five payload fields only)", () => {
  const ITEMS = [
    {
      message_id: "msg-1",
      subject: "Re: July invoice — can you confirm the hours?",
      sender: "Sarah Lin",
      received_at: "2026-07-11T08:12:00.000Z",
      priority_band: "high",
    },
    {
      message_id: "msg-2",
      subject: "Your receipt for July",
      sender: "Anthropic billing",
      received_at: "2026-07-10T12:00:00.000Z",
      priority_band: "normal",
    },
  ];

  it("renders a card per item, honest to the five stored fields only - no snippet/body", async () => {
    installFakeBridge();
    installFetch({ items: ITEMS, storage_unavailable: false });
    render(<InboxHighlights />);

    expect(await screen.findByText("Re: July invoice — can you confirm the hours?")).toBeTruthy();
    expect(screen.getByText("Sarah Lin")).toBeTruthy();
    expect(screen.getByText("Your receipt for July")).toBeTruthy();
    expect(screen.getByText("Anthropic billing")).toBeTruthy();
    // Priority band mapped to a chip - the literal band value, nothing invented
    // (no "needs reply"/"FYI" label - that refinement stays deferred, DEC-47(c)).
    expect(screen.getByText("high")).toBeTruthy();
    expect(screen.getByText("normal")).toBeTruthy();
    // No snippet/body text is ever rendered - only the five stored fields.
    expect(screen.queryByText(/confirm the hours\?.+/)).toBeNull();
  });

  it("clicking a card invokes the open-in-Gmail bridge with the message_id only", async () => {
    installFakeBridge();
    installFetch({ items: ITEMS, storage_unavailable: false });
    const openMessage = vi.fn().mockResolvedValue({ ok: true });
    (window as unknown as { wombatGmail: { openMessage: typeof openMessage } }).wombatGmail = {
      openMessage,
    };
    render(<InboxHighlights />);

    const card = await screen.findByText("Re: July invoice — can you confirm the hours?");
    fireEvent.click(card.closest('[role="button"]') as HTMLElement);

    await waitFor(() => expect(openMessage).toHaveBeenCalledWith("msg-1"));
    expect(openMessage).toHaveBeenCalledTimes(1);
  });
});
