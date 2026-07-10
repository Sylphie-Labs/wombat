// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ChatPane } from "./ChatPane";

/**
 * TK-223 AC1/AC2: ChatPane against a fake window.wombatChat bridge + fake
 * fetch, speaking surface.py's verbatim wire shapes. The live round trip
 * through the real runtime is TK-201's launch-doc bring-up step.
 */

const PORT = 45123;
const TOKEN = "chat-test-token";

function stubBridge(info: { port: number; token: string } | null): void {
  (window as unknown as { wombatChat: { getInfo: () => Promise<unknown> } }).wombatChat = {
    getInfo: vi.fn().mockResolvedValue(info),
  };
}

async function typeAndSend(text: string): Promise<void> {
  fireEvent.change(screen.getByLabelText("Message"), { target: { value: text } });
  fireEvent.click(screen.getByRole("button", { name: /send/i }));
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("ChatPane (TK-223 AC1: send/receive)", () => {
  it("renders the reply in the transcript on a replied response", async () => {
    stubBridge({ port: PORT, token: TOKEN });
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(Response.json({ status: "replied", text: "hi there" })),
    );

    render(<ChatPane />);
    await typeAndSend("hello wombat");

    expect(await screen.findByText("You: hello wombat")).toBeTruthy();
    expect(await screen.findByText("Wombat: hi there")).toBeTruthy();
  });

  it("renders the honest held state on a held response, without a spinner", async () => {
    stubBridge({ port: PORT, token: TOKEN });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json({ status: "held" })));

    render(<ChatPane />);
    await typeAndSend("still working on this one");

    expect(
      await screen.findByText(/no reply within 30s - wombat is holding this or still working/i),
    ).toBeTruthy();
  });

  it("clears the input after sending", async () => {
    stubBridge({ port: PORT, token: TOKEN });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json({ status: "held" })));

    render(<ChatPane />);
    await typeAndSend("hello");

    await waitFor(() => {
      expect((screen.getByLabelText("Message") as HTMLInputElement).value).toBe("");
    });
  });
});

describe("ChatPane (TK-223 AC2: degraded state)", () => {
  it("shows 'wombat is not running' on load when the bridge resolves null", async () => {
    stubBridge(null);

    render(<ChatPane />);

    expect(await screen.findByText(/wombat is not running/i)).toBeTruthy();
  });

  it("shows 'wombat is not running' after a send fails against a rejecting fetch, without crashing", async () => {
    stubBridge({ port: PORT, token: TOKEN });
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("ECONNREFUSED")));

    render(<ChatPane />);
    await typeAndSend("hello?");

    expect(await screen.findByText(/wombat is not running/i)).toBeTruthy();
    // The user's own message still renders - a failed send doesn't wipe the transcript.
    expect(screen.getByText("You: hello?")).toBeTruthy();
  });

  it("recovers the not-running banner once a later send succeeds", async () => {
    stubBridge(null);
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(Response.json({ status: "replied", text: "back online" })),
    );

    render(<ChatPane />);
    expect(await screen.findByText(/wombat is not running/i)).toBeTruthy();

    // A subsequent send re-queries the bridge fresh and this time succeeds
    // (fetch itself is never reached for the first, bridge-null send).
    (window as unknown as { wombatChat: { getInfo: () => Promise<unknown> } }).wombatChat = {
      getInfo: vi.fn().mockResolvedValue({ port: PORT, token: TOKEN }),
    };
    await typeAndSend("hello again");

    await waitFor(() => {
      expect(screen.queryByText(/wombat is not running/i)).toBeNull();
    });
    expect(await screen.findByText("Wombat: back online")).toBeTruthy();
  });
});
