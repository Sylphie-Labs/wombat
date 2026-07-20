// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { resetBridgeCacheForTests } from "../api";
import { AudioPanel } from "./AudioPanel";

/**
 * TK-224 AC2 (mute + device select) / AC3 (honest controls), against a fake
 * getUserMedia/AudioContext/wombatAudio bridge + fake settings API - no real
 * mic, no Electron. The live round trip is TK-201's smoke.
 */

const PORT = 44120;
const TOKEN = "audio-test-token";

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

function fireSamples(context: FakeAudioContext, values: number[]): void {
  context.processor.onaudioprocess?.({
    inputBuffer: { getChannelData: () => new Float32Array(values) },
  });
}

let getUserMedia: ReturnType<typeof vi.fn>;
let lastAudioContext: FakeAudioContext;
let saveCapture: ReturnType<typeof vi.fn>;

interface InstallOptions {
  devices?: { deviceId: string; kind: string; label: string }[];
  saveCaptureResult?: { ok: boolean; path?: string; reason?: string };
}

interface FetchCall {
  url: string;
  method: string;
  body: unknown;
}

function installFakeAudioEnv(options: InstallOptions = {}): { calls: FetchCall[] } {
  getUserMedia = vi.fn().mockResolvedValue(new FakeMediaStream());
  (navigator as unknown as { mediaDevices: unknown }).mediaDevices = {
    getUserMedia,
    enumerateDevices: vi.fn().mockResolvedValue(
      options.devices ?? [
        { kind: "audioinput", deviceId: "dev-1", label: "Mic One" },
        { kind: "audioinput", deviceId: "dev-2", label: "Mic Two" },
        { kind: "audiooutput", deviceId: "dev-3", label: "Speakers" },
      ],
    ),
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

  const calls: FetchCall[] = [];
  const settings: Record<string, unknown> = { wombat_voice_enabled: false, wombat_ptt_binding: "" };
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = init?.method ?? "GET";
    const body = init?.body ? (JSON.parse(init.body as string) as unknown) : undefined;
    calls.push({ url, method, body });
    const headers = new Headers(init?.headers);
    if (headers.get("X-Wombat-Token") !== TOKEN) {
      return new Response(null, { status: 401 });
    }
    if (method === "GET" && url.endsWith("/settings")) {
      return Response.json({ settings: { ...settings }, keys: {} });
    }
    if (method === "PUT" && url.endsWith("/settings")) {
      Object.assign(settings, body as Record<string, unknown>);
      return Response.json({ settings: { ...settings } });
    }
    throw new Error(`unhandled fetch: ${method} ${url}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  return { calls };
}

beforeEach(() => {
  resetBridgeCacheForTests();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("AudioPanel (TK-224 AC2: mute + device select)", () => {
  it("mute suppresses the hand-off entirely - saveCapture is never called", async () => {
    installFakeAudioEnv();
    render(<AudioPanel />);
    await screen.findByLabelText("Input device");

    fireEvent.click(screen.getByRole("button", { name: /^mute$/i }));
    fireEvent.click(screen.getByRole("button", { name: /^record$/i }));
    await waitFor(() => expect(getUserMedia).toHaveBeenCalledTimes(1));

    fireSamples(lastAudioContext, [0.1, 0.2, 0.3]);

    fireEvent.click(screen.getByRole("button", { name: /^stop$/i }));
    await waitFor(() => expect(screen.getByRole("button", { name: /^record$/i })).toBeTruthy());

    expect(saveCapture).not.toHaveBeenCalled();
  });

  it("delivers the capture to saveCapture when not muted", async () => {
    installFakeAudioEnv();
    render(<AudioPanel />);
    await screen.findByLabelText("Input device");

    fireEvent.click(screen.getByRole("button", { name: /^record$/i }));
    await waitFor(() => expect(getUserMedia).toHaveBeenCalledTimes(1));

    fireSamples(lastAudioContext, [0.1, 0.2, 0.3]);

    fireEvent.click(screen.getByRole("button", { name: /^stop$/i }));
    await waitFor(() => expect(saveCapture).toHaveBeenCalledTimes(1));
  });

  it("renders the enumerated input devices (output devices excluded)", async () => {
    installFakeAudioEnv();
    render(<AudioPanel />);

    const select = (await screen.findByLabelText("Input device")) as HTMLSelectElement;
    expect(Array.from(select.options).map((o) => o.value)).toEqual(["dev-1", "dev-2"]);
  });

  it("applies a device selection change to the NEXT capture's getUserMedia constraints", async () => {
    installFakeAudioEnv();
    render(<AudioPanel />);

    const select = (await screen.findByLabelText("Input device")) as HTMLSelectElement;
    fireEvent.change(select, { target: { value: "dev-2" } });
    fireEvent.click(screen.getByRole("button", { name: /^record$/i }));

    await waitFor(() => expect(getUserMedia).toHaveBeenCalledTimes(1));
    expect(getUserMedia).toHaveBeenCalledWith({ audio: { deviceId: { exact: "dev-2" } } });
  });
});

describe("AudioPanel (TK-224 AC3: honest controls)", () => {
  it("PUTs wombat_voice_enabled through the settings API and shows the restart notice", async () => {
    const { calls } = installFakeAudioEnv();
    render(<AudioPanel />);
    await screen.findByLabelText("Input device");

    fireEvent.click(screen.getByRole("button", { name: /^voice off$/i }));

    expect(await screen.findByRole("button", { name: /^voice on$/i })).toBeTruthy();
    expect(await screen.findByText("Restart Wombat to apply this change.")).toBeTruthy();

    const settingsPuts = calls.filter(
      (call) => call.method === "PUT" && call.url.endsWith("/settings"),
    );
    expect(settingsPuts.length).toBe(1);
    expect(settingsPuts[0].body).toEqual({ wombat_voice_enabled: true });
  });

  it("renders no output-volume control", async () => {
    installFakeAudioEnv();
    render(<AudioPanel />);
    await screen.findByLabelText("Input device");

    expect(screen.queryByLabelText(/volume/i)).toBeNull();
    expect(screen.queryByText(/volume/i)).toBeNull();
    expect(screen.queryByRole("slider")).toBeNull();
  });

  it("disables the capture controls with an explanation once the drop-dir is reported unconfigured", async () => {
    installFakeAudioEnv({ saveCaptureResult: { ok: false, reason: "drop-dir-not-configured" } });
    render(<AudioPanel />);
    await screen.findByLabelText("Input device");

    fireEvent.click(screen.getByRole("button", { name: /^record$/i }));
    await waitFor(() => expect(getUserMedia).toHaveBeenCalledTimes(1));
    fireSamples(lastAudioContext, [0.1, 0.2, 0.3]);
    fireEvent.click(screen.getByRole("button", { name: /^stop$/i }));

    expect(
      await screen.findByText("voice drop-dir not configured - set WOMBAT_ASR_DROP_DIR"),
    ).toBeTruthy();
    expect(
      (screen.getByRole("button", { name: /^record$/i }) as HTMLButtonElement).disabled,
    ).toBe(true);
    expect((screen.getByRole("button", { name: /^mute$/i }) as HTMLButtonElement).disabled).toBe(
      true,
    );
    expect((screen.getByLabelText("Input device") as HTMLSelectElement).disabled).toBe(true);
  });
});

describe("AudioPanel (TK-275 DEC-58 c/d: push-to-talk binding capture)", () => {
  it("AC1: arming then pressing a key becomes the binding and PUTs the key: encoding once", async () => {
    const { calls } = installFakeAudioEnv();
    render(<AudioPanel />);
    await screen.findByLabelText("Input device");

    fireEvent.click(screen.getByRole("button", { name: /^set push-to-talk$/i }));
    fireEvent.keyDown(document, { code: "KeyK", key: "k" });

    expect(await screen.findByText("Key: KeyK")).toBeTruthy();

    const settingsPuts = calls.filter(
      (call) => call.method === "PUT" && call.url.endsWith("/settings"),
    );
    expect(settingsPuts.length).toBe(1);
    expect(settingsPuts[0].body).toEqual({ wombat_ptt_binding: "key:KeyK" });

    // one-shot: a second press after capture does nothing further.
    fireEvent.keyDown(document, { code: "KeyJ", key: "j" });
    expect(screen.queryByText("Key: KeyJ")).toBeNull();
    expect(
      calls.filter((call) => call.method === "PUT" && call.url.endsWith("/settings")).length,
    ).toBe(1);

    expect(screen.queryByText("Restart Wombat to apply this change.")).toBeNull();
  });

  it("AC1: arming then pressing a non-left/right mouse button becomes the binding", async () => {
    const { calls } = installFakeAudioEnv();
    render(<AudioPanel />);
    await screen.findByLabelText("Input device");

    fireEvent.click(screen.getByRole("button", { name: /^set push-to-talk$/i }));
    fireEvent.mouseDown(document, { button: 1 });

    expect(await screen.findByText("Mouse button 1")).toBeTruthy();
    const settingsPuts = calls.filter(
      (call) => call.method === "PUT" && call.url.endsWith("/settings"),
    );
    expect(settingsPuts.length).toBe(1);
    expect(settingsPuts[0].body).toEqual({ wombat_ptt_binding: "mouse:1" });
  });

  it("AC1: Escape during arming cancels with no putSettings call", async () => {
    const { calls } = installFakeAudioEnv();
    render(<AudioPanel />);
    await screen.findByLabelText("Input device");

    fireEvent.click(screen.getByRole("button", { name: /^set push-to-talk$/i }));
    fireEvent.keyDown(document, { code: "Escape", key: "Escape" });

    expect(await screen.findByRole("button", { name: /^set push-to-talk$/i })).toBeTruthy();
    expect(screen.getByText("Not set")).toBeTruthy();
    expect(
      calls.filter((call) => call.method === "PUT" && call.url.endsWith("/settings")).length,
    ).toBe(0);
  });

  it("AC2: left and right click are rejected with a visible explanation and arming continues", async () => {
    installFakeAudioEnv();
    render(<AudioPanel />);
    await screen.findByLabelText("Input device");

    fireEvent.click(screen.getByRole("button", { name: /^set push-to-talk$/i }));
    fireEvent.mouseDown(document, { button: 0 });
    expect(
      await screen.findByText("Left and right click can't be used as the push-to-talk binding."),
    ).toBeTruthy();
    expect(screen.getByRole("button", { name: /press a key or mouse button/i })).toBeTruthy();

    fireEvent.mouseDown(document, { button: 2 });
    expect(
      screen.getByText("Left and right click can't be used as the push-to-talk binding."),
    ).toBeTruthy();
    expect(screen.getByRole("button", { name: /press a key or mouse button/i })).toBeTruthy();

    // arming survived both rejections - a subsequent valid mouse button still captures.
    fireEvent.mouseDown(document, { button: 1 });
    expect(await screen.findByText("Mouse button 1")).toBeTruthy();
  });

  it("AC2: a keypress with modifiers held binds the bare key code (no chords)", async () => {
    const { calls } = installFakeAudioEnv();
    render(<AudioPanel />);
    await screen.findByLabelText("Input device");

    fireEvent.click(screen.getByRole("button", { name: /^set push-to-talk$/i }));
    fireEvent.keyDown(document, { code: "KeyK", key: "k", ctrlKey: true });

    expect(await screen.findByText("Key: KeyK")).toBeTruthy();
    const settingsPuts = calls.filter(
      (call) => call.method === "PUT" && call.url.endsWith("/settings"),
    );
    expect(settingsPuts[0].body).toEqual({ wombat_ptt_binding: "key:KeyK" });
  });
});
