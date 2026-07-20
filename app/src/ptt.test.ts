// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { createElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { resetBridgeCacheForTests } from "./api";
import {
  acquireCaptureLatch,
  announcePttBinding,
  isEditableTarget,
  parseBinding,
  releaseCaptureLatch,
  resetCaptureLatchForTests,
  usePushToTalk,
} from "./ptt";

/**
 * TK-276 (DEC-58 a/b/e): the pure binding parser, the shared capture latch,
 * and `usePushToTalk` itself, against a fake getUserMedia/AudioContext/
 * wombatAudio bridge (no real mic, no Electron) - mirrors
 * `AudioPanel.test.tsx`'s fake env precedent.
 */

const PORT = 44121;
const TOKEN = "ptt-test-token";

class FakeAudioTrack {
  enabled = true;
  stop = vi.fn();
}

class FakeMediaStream {
  private tracks: FakeAudioTrack[] = [new FakeAudioTrack()];
  getAudioTracks(): FakeAudioTrack[] {
    return this.tracks;
  }
  getTracks(): FakeAudioTrack[] {
    return this.tracks;
  }
}

class FakeScriptProcessor {
  onaudioprocess: ((event: unknown) => void) | null = null;
  connect = vi.fn();
  disconnect = vi.fn();
}

class FakeAudioContext {
  sampleRate = 16000;
  destination = {};
  processor = new FakeScriptProcessor();
  createMediaStreamSource(): { connect: () => void; disconnect: () => void } {
    return { connect: vi.fn(), disconnect: vi.fn() };
  }
  createScriptProcessor(): FakeScriptProcessor {
    return this.processor;
  }
  close(): Promise<void> {
    return Promise.resolve();
  }
}

/** Retries `dispatch` for a bounded window and asserts `getUserMedia` is NEVER called - a
 * non-vacuous negative check (it gives listener installation, which trails an async settings
 * fetch, ample time to happen) rather than a single premature assertion. */
async function neverStartsCapture(dispatch: () => void): Promise<void> {
  await expect(
    waitFor(
      () => {
        dispatch();
        expect(getUserMedia).toHaveBeenCalled();
      },
      { timeout: 300, interval: 20 },
    ),
  ).rejects.toThrow();
}

function fireSamples(context: FakeAudioContext, values: number[]): void {
  context.processor.onaudioprocess?.({
    inputBuffer: { getChannelData: () => new Float32Array(values) },
  });
}

let getUserMedia: ReturnType<typeof vi.fn>;
let lastAudioContext: FakeAudioContext;
let saveCapture: ReturnType<typeof vi.fn>;

function installFakeEnv(options: {
  pttBinding?: string;
  saveCaptureResult?: { ok: boolean; path?: string; reason?: string };
} = {}): void {
  getUserMedia = vi.fn().mockResolvedValue(new FakeMediaStream());
  (navigator as unknown as { mediaDevices: unknown }).mediaDevices = {
    getUserMedia,
    enumerateDevices: vi.fn().mockResolvedValue([]),
  };

  (window as unknown as { AudioContext: unknown }).AudioContext = vi
    .fn()
    .mockImplementation(function AudioContextMock() {
      lastAudioContext = new FakeAudioContext();
      return lastAudioContext;
    });

  saveCapture = vi
    .fn()
    .mockResolvedValue(options.saveCaptureResult ?? { ok: true, path: "C:/drop/capture-1.wav" });
  (window as unknown as { wombatAudio: unknown }).wombatAudio = { saveCapture };

  (window as unknown as { wombatSettings: { getInfo: () => Promise<unknown> } }).wombatSettings = {
    getInfo: vi.fn().mockResolvedValue({ port: PORT, token: TOKEN }),
  };

  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = init?.method ?? "GET";
    const headers = new Headers(init?.headers);
    if (headers.get("X-Wombat-Token") !== TOKEN) {
      return new Response(null, { status: 401 });
    }
    if (method === "GET" && url.endsWith("/settings")) {
      return Response.json({
        settings: { wombat_ptt_binding: options.pttBinding ?? "" },
        keys: {},
      });
    }
    throw new Error(`unhandled fetch: ${method} ${url}`);
  });
  vi.stubGlobal("fetch", fetchMock);
}

/** Minimal host: renders the hook's state as text plus an editable input, so keydown/mousedown
 * events can be dispatched with a chosen target. */
function Harness() {
  const state = usePushToTalk();
  return createElement(
    "div",
    null,
    createElement("span", { "data-testid": "active" }, state.active ? "active" : "idle"),
    createElement("span", { "data-testid": "degraded" }, state.degraded ? "degraded" : "ok"),
    createElement("input", { "data-testid": "editable" }),
  );
}

beforeEach(() => {
  resetBridgeCacheForTests();
  resetCaptureLatchForTests();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("parseBinding", () => {
  it("parses a key binding", () => {
    expect(parseBinding("key:KeyK")).toEqual({ kind: "key", code: "KeyK" });
  });

  it("parses a mouse binding", () => {
    expect(parseBinding("mouse:1")).toEqual({ kind: "mouse", button: 1 });
  });

  it("returns null for unbound and malformed values", () => {
    expect(parseBinding("")).toBeNull();
    expect(parseBinding("bogus")).toBeNull();
    expect(parseBinding("mouse:not-a-number")).toBeNull();
  });
});

describe("isEditableTarget", () => {
  it("treats input/textarea/contenteditable as editable, everything else as not", () => {
    const input = document.createElement("input");
    const textarea = document.createElement("textarea");
    const div = document.createElement("div");
    const editableDiv = document.createElement("div");
    editableDiv.setAttribute("contenteditable", "true");

    expect(isEditableTarget(input)).toBe(true);
    expect(isEditableTarget(textarea)).toBe(true);
    expect(isEditableTarget(editableDiv)).toBe(true);
    expect(isEditableTarget(div)).toBe(false);
    expect(isEditableTarget(null)).toBe(false);
  });
});

describe("shared capture latch", () => {
  it("only one owner can hold it at a time", () => {
    expect(acquireCaptureLatch("manual")).toBe(true);
    expect(acquireCaptureLatch("ptt")).toBe(false);
    releaseCaptureLatch("manual");
    expect(acquireCaptureLatch("ptt")).toBe(true);
  });
});

describe("usePushToTalk (AC1: key/mouse hold drives the existing capture path)", () => {
  it("a key press starts capture exactly once with the indicator visible; repeat keydowns don't restart it; release stops it and hands off to saveCapture", async () => {
    installFakeEnv({ pttBinding: "key:KeyK" });
    render(createElement(Harness));
    await waitFor(() => expect(screen.getByTestId("active").textContent).toBe("idle"));

    // Listener installation trails the async settings fetch - retry the press until it lands
    // (this is the only way to observe "listeners are now installed" from outside the hook).
    await waitFor(() => {
      fireEvent.keyDown(document, { code: "KeyK" });
      expect(getUserMedia).toHaveBeenCalledTimes(1);
    });
    expect(screen.getByTestId("active").textContent).toBe("active");

    // synthetic OS auto-repeat - never restarts the capture.
    fireEvent.keyDown(document, { code: "KeyK", repeat: true });
    expect(getUserMedia).toHaveBeenCalledTimes(1);

    fireSamples(lastAudioContext, [0.1, 0.2, 0.3]);
    fireEvent.keyUp(document, { code: "KeyK" });

    await waitFor(() => expect(saveCapture).toHaveBeenCalledTimes(1));
    expect(screen.getByTestId("active").textContent).toBe("idle");
  });

  it("a mouse:<button> binding drives the same hold via mousedown/mouseup", async () => {
    installFakeEnv({ pttBinding: "mouse:3" });
    render(createElement(Harness));
    await waitFor(() => expect(screen.getByTestId("active").textContent).toBe("idle"));

    await waitFor(() => {
      fireEvent.mouseDown(document, { button: 3 });
      expect(getUserMedia).toHaveBeenCalledTimes(1);
    });
    expect(screen.getByTestId("active").textContent).toBe("active");

    fireSamples(lastAudioContext, [0.1, 0.2, 0.3]);
    fireEvent.mouseUp(document, { button: 3 });

    await waitFor(() => expect(saveCapture).toHaveBeenCalledTimes(1));
    expect(screen.getByTestId("active").textContent).toBe("idle");
  });
});

describe("usePushToTalk (AC2: never a silent no-op, but also never a wrong-op)", () => {
  it("a press targeting an editable element does not open the mic", async () => {
    installFakeEnv({ pttBinding: "key:KeyK" });
    render(createElement(Harness));
    await waitFor(() => expect(screen.getByTestId("active").textContent).toBe("idle"));

    await neverStartsCapture(() =>
      fireEvent.keyDown(screen.getByTestId("editable"), { code: "KeyK", bubbles: true }),
    );
    expect(screen.getByTestId("active").textContent).toBe("idle");
  });

  it("a press defers while the manual Record session holds the shared latch", async () => {
    installFakeEnv({ pttBinding: "key:KeyK" });
    expect(acquireCaptureLatch("manual")).toBe(true);

    render(createElement(Harness));

    await neverStartsCapture(() => fireEvent.keyDown(document, { code: "KeyK" }));
    expect(screen.getByTestId("active").textContent).toBe("idle");

    releaseCaptureLatch("manual");
  });

  it("an unbound ('') binding installs no listeners - a keypress never opens the mic", async () => {
    installFakeEnv({ pttBinding: "" });
    render(createElement(Harness));

    await neverStartsCapture(() => fireEvent.keyDown(document, { code: "KeyK" }));
  });
});

describe("usePushToTalk (AC3: drop-dir degrade truth)", () => {
  it("surfaces the existing drop-dir-not-configured degrade truth", async () => {
    installFakeEnv({
      pttBinding: "key:KeyK",
      saveCaptureResult: { ok: false, reason: "drop-dir-not-configured" },
    });
    render(createElement(Harness));

    await waitFor(() => {
      fireEvent.keyDown(document, { code: "KeyK" });
      expect(getUserMedia).toHaveBeenCalledTimes(1);
    });
    fireSamples(lastAudioContext, [0.1, 0.2, 0.3]);
    fireEvent.keyUp(document, { code: "KeyK" });

    await waitFor(() => expect(screen.getByTestId("degraded").textContent).toBe("degraded"));
  });
});

describe("announcePttBinding", () => {
  it("updates the live binding immediately, without a settings refetch", async () => {
    installFakeEnv({ pttBinding: "" });
    render(createElement(Harness));
    await waitFor(() => expect(screen.getByTestId("degraded").textContent).toBe("ok"));

    act(() => announcePttBinding("key:KeyZ"));

    fireEvent.keyDown(document, { code: "KeyZ" });
    await waitFor(() => expect(getUserMedia).toHaveBeenCalledTimes(1));
  });
});
