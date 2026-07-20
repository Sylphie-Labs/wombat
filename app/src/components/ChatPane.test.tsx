// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  POLL_GIVE_UP_MS,
  POLL_INTERVAL_MS,
  SEND_TIMEOUT_MS,
  VOICE_TURNS_POLL_INTERVAL_MS,
} from "../chat";
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

// TK-281: the pane's own always-on voice-turns poll shares the same global
// `fetch` mock in these tests - filter a mock's calls down to just the
// late-reply path so assertions about THAT poll's call count aren't
// perturbed by the unrelated voice-turns poll ticking in the background.
function lateReplyCalls(fetchMock: ReturnType<typeof vi.fn>): unknown[] {
  return fetchMock.mock.calls.filter(([url]) => (url as string).includes("/chat/reply/"));
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  vi.useRealTimers();
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
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(Response.json({ status: "held", id: "item-held-1" })),
    );

    render(<ChatPane />);
    await typeAndSend("still working on this one");

    expect(
      await screen.findByText(/no reply within 30s - wombat is holding this or still working/i),
    ).toBeTruthy();
  });

  it("renders the honest timed-out line and re-enables send controls (TK-266 / ISS-19)", async () => {
    vi.useFakeTimers();
    stubBridge({ port: PORT, token: TOKEN });
    // A fetch that never settles - only `sendChat`'s own AbortController
    // resolves it, once its timeout fires.
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((_url: string, init?: RequestInit) => {
        return new Promise((_resolve, reject) => {
          init?.signal?.addEventListener("abort", () => {
            reject(new DOMException("The operation was aborted.", "AbortError"));
          });
        });
      }),
    );

    render(<ChatPane />);
    fireEvent.change(screen.getByLabelText("Message"), {
      target: { value: "will this ever come back" },
    });
    fireEvent.click(screen.getByRole("button", { name: /send/i }));

    await act(async () => {
      await vi.advanceTimersByTimeAsync(SEND_TIMEOUT_MS);
    });

    expect(
      screen.getByText("Wombat stopped responding - the message may be lost."),
    ).toBeTruthy();

    // "Send" (not stuck on "Sending...") and the input itself re-enabled;
    // the button stays disabled only because the (already-cleared) draft is
    // empty - the same as after any other completed send, e.g. `held`.
    expect(screen.getByRole("button", { name: "Send" })).toBeTruthy();
    expect((screen.getByLabelText("Message") as HTMLInputElement).disabled).toBe(false);

    vi.useRealTimers();
  });

  it("clears the input after sending", async () => {
    stubBridge({ port: PORT, token: TOKEN });
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(Response.json({ status: "held", id: "item-held-2" })),
    );

    render(<ChatPane />);
    await typeAndSend("hello");

    await waitFor(() => {
      expect((screen.getByLabelText("Message") as HTMLInputElement).value).toBe("");
    });
  });
});

describe("ChatPane (TK-270 / DEC-56(b): late-reply polling)", () => {
  it("polls for a late reply after held, appends it with a 'replied late' marker, and stops polling", async () => {
    vi.useFakeTimers();
    stubBridge({ port: PORT, token: TOKEN });
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(Response.json({ status: "held", id: "item-abc" }))
      .mockResolvedValueOnce(Response.json({ status: "pending" }))
      .mockResolvedValueOnce(Response.json({ status: "replied", text: "sorry for the wait" }))
      // Fallback for the background voice-turns poll ticking during/after this test's window.
      .mockResolvedValue(Response.json({ status: "pending" }));
    vi.stubGlobal("fetch", fetchMock);

    render(<ChatPane />);
    fireEvent.change(screen.getByLabelText("Message"), { target: { value: "slow one" } });
    fireEvent.click(screen.getByRole("button", { name: /send/i }));

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(
      screen.getByText(/no reply within 30s - wombat is holding this or still working/i),
    ).toBeTruthy();

    // Two poll ticks: first sees "pending", second sees "replied".
    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS);
    });

    expect(screen.getByText("Wombat (replied late): sorry for the wait")).toBeTruthy();

    // Late-reply polling has stopped for this id - further time passing
    // makes no more /chat/reply/ calls (the unrelated voice-turns poll may
    // still tick in the background; see `lateReplyCalls`).
    const lateReplyCallsAfterReply = lateReplyCalls(fetchMock).length;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS * 3);
    });
    expect(lateReplyCalls(fetchMock).length).toBe(lateReplyCallsAfterReply);

    vi.useRealTimers();
  });

  it("gives up after the poll bound elapses, renders an honest line, and stops polling", async () => {
    vi.useFakeTimers();
    stubBridge({ port: PORT, token: TOKEN });
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(Response.json({ status: "held", id: "item-xyz" }))
      .mockImplementation(() => Promise.resolve(Response.json({ status: "pending" })));
    vi.stubGlobal("fetch", fetchMock);

    render(<ChatPane />);
    fireEvent.change(screen.getByLabelText("Message"), {
      target: { value: "never comes back" },
    });
    fireEvent.click(screen.getByRole("button", { name: /send/i }));

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });

    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_GIVE_UP_MS + POLL_INTERVAL_MS * 2);
    });

    expect(
      screen.getByText(/still no reply after several minutes - giving up waiting/i),
    ).toBeTruthy();

    // Late-reply polling has stopped (the give-up line rendered) - further
    // time passing makes no more /chat/reply/ calls (the unrelated
    // voice-turns poll may still tick in the background).
    const lateReplyCallsAfterGiveUp = lateReplyCalls(fetchMock).length;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS * 3);
    });
    expect(lateReplyCalls(fetchMock).length).toBe(lateReplyCallsAfterGiveUp);

    vi.useRealTimers();
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

describe("ChatPane (TK-281 / DEC-60c app half: voice turns)", () => {
  it("AC1: an unseen voice transcript appends once with a visible marker; its reply appends once on land; re-polls of the same snapshot append nothing", async () => {
    vi.useFakeTimers();
    stubBridge({ port: PORT, token: TOKEN });
    const voiceTurn = (reply: string | null) =>
      Response.json([{ id: "v1", transcript: "hey wombat", captured_at: "t1", reply }]);
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(voiceTurn(null))
      .mockResolvedValueOnce(voiceTurn("hi there"))
      // Fresh Response instance per call (a Response body can only be read once).
      .mockImplementation(() => Promise.resolve(voiceTurn("hi there")));
    vi.stubGlobal("fetch", fetchMock);

    render(<ChatPane />);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(VOICE_TURNS_POLL_INTERVAL_MS);
    });
    expect(screen.getAllByText(/hey wombat/i)).toHaveLength(1);
    expect(screen.getByLabelText("voice message")).toBeTruthy();
    expect(screen.queryByText("Wombat: hi there")).toBeNull();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(VOICE_TURNS_POLL_INTERVAL_MS);
    });
    expect(screen.getByText("Wombat: hi there")).toBeTruthy();

    // A re-poll of the same (already-seen) snapshot appends nothing further.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(VOICE_TURNS_POLL_INTERVAL_MS * 2);
    });
    expect(screen.getAllByText(/hey wombat/i)).toHaveLength(1);
    expect(screen.getAllByText("Wombat: hi there")).toHaveLength(1);

    vi.useRealTimers();
  });

  it("AC2: a repeatedly failing voice-turns poll shows nothing, never disturbs typed chat, and silently resumes", async () => {
    vi.useFakeTimers();
    stubBridge({ port: PORT, token: TOKEN });
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      if (url.includes("/chat/voice-turns")) {
        return Promise.reject(new Error("ECONNREFUSED"));
      }
      return Promise.resolve(Response.json({ status: "replied", text: "hi there" }));
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<ChatPane />);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(VOICE_TURNS_POLL_INTERVAL_MS * 4);
    });
    // Repeated poll failures never surface a "not running" banner or any
    // transcript line - the typed-chat path is entirely untouched by them.
    expect(screen.queryByText(/wombat is not running/i)).toBeNull();
    expect(screen.getByRole("log").children).toHaveLength(0);

    // A subsequent tick succeeding proves the poll silently resumed.
    fetchMock.mockImplementation((url: string) => {
      if (url.includes("/chat/voice-turns")) {
        return Promise.resolve(
          Response.json([{ id: "v9", transcript: "still there?", captured_at: "t9", reply: null }]),
        );
      }
      return Promise.resolve(Response.json({ status: "replied", text: "hi there" }));
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(VOICE_TURNS_POLL_INTERVAL_MS);
    });
    expect(screen.getByText(/still there\?/i)).toBeTruthy();

    // Typed chat itself is unaffected throughout.
    fireEvent.change(screen.getByLabelText("Message"), { target: { value: "hello" } });
    fireEvent.click(screen.getByRole("button", { name: /send/i }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(screen.getByText("Wombat: hi there")).toBeTruthy();

    vi.useRealTimers();
  });

  it("AC3: unmounting mid-poll clears the interval and aborts the in-flight fetch, with no post-unmount state updates", async () => {
    vi.useFakeTimers();
    stubBridge({ port: PORT, token: TOKEN });
    let capturedSignal: AbortSignal | undefined;
    const fetchMock = vi.fn().mockImplementation((_url: string, init?: RequestInit) => {
      capturedSignal = init?.signal ?? undefined;
      return new Promise((_resolve, reject) => {
        init?.signal?.addEventListener("abort", () => {
          reject(new DOMException("The operation was aborted.", "AbortError"));
        });
      });
    });
    vi.stubGlobal("fetch", fetchMock);

    const { unmount } = render(<ChatPane />);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(VOICE_TURNS_POLL_INTERVAL_MS);
    });
    expect(capturedSignal?.aborted).toBe(false);
    const callsBeforeUnmount = fetchMock.mock.calls.length;

    unmount();

    expect(capturedSignal?.aborted).toBe(true);

    // No further poll calls after unmount - the interval was cleared, and
    // there is no state update to warn about since the pane is gone.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(VOICE_TURNS_POLL_INTERVAL_MS * 5);
    });
    expect(fetchMock.mock.calls.length).toBe(callsBeforeUnmount);

    vi.useRealTimers();
  });
});
