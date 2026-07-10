// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";
import { resetBridgeCacheForTests } from "./api";

/**
 * TK-200 acceptance tests, against a fake bridge + fake fetch (no live API -
 * the live round trip is TK-201's smoke). Covers AC1 (load), AC2 (save +
 * restart notice), and AC3 (the DEC-37 persona hot-apply/restart notice
 * split).
 */

const PORT = 41417;
const TOKEN = "test-token";

type SettingsShape = Record<string, string | null>;

function baseSettings(): SettingsShape {
  return {
    wombat_stt_provider: "deepgram",
    wombat_tts_provider: "elevenlabs",
    wombat_tts_voice_id: "voice-1",
    wombat_stt_model: null,
    wombat_assistant_name: "Wombat",
    wombat_persona_brevity: "balanced",
    wombat_persona_warmth: "warm",
    wombat_persona_directness: "blunt",
    wombat_persona_humor: "dry",
    wombat_persona_proactivity: "forward",
  };
}

interface FetchCall {
  url: string;
  method: string;
  body: unknown;
}

function installFakeApi(
  settings: SettingsShape = baseSettings(),
  keys: Record<string, boolean> = { elevenlabs: true, deepgram: false, fish: false },
): { calls: FetchCall[] } {
  (window as unknown as { wombatSettings: { getInfo: () => Promise<unknown> } }).wombatSettings =
    {
      getInfo: vi.fn().mockResolvedValue({ port: PORT, token: TOKEN }),
    };

  const calls: FetchCall[] = [];
  const currentSettings = { ...settings };
  const currentKeys = { ...keys };

  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = init?.method ?? "GET";
    const body = init?.body ? JSON.parse(init.body as string) : undefined;
    calls.push({ url, method, body });

    const headers = new Headers(init?.headers);
    if (headers.get("X-Wombat-Token") !== TOKEN) {
      return new Response(null, { status: 401 });
    }

    if (method === "GET" && url.endsWith("/settings")) {
      return Response.json({ settings: { ...currentSettings }, keys: { ...currentKeys } });
    }
    if (method === "PUT" && url.endsWith("/settings")) {
      Object.assign(currentSettings, body as SettingsShape);
      return Response.json({ settings: { ...currentSettings } });
    }
    const keyMatch = /\/keys\/(elevenlabs|deepgram|fish)$/.exec(url);
    if (method === "PUT" && keyMatch) {
      currentKeys[keyMatch[1]] = true;
      return Response.json({ ok: true });
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

describe("App (TK-200 AC1: load)", () => {
  it("renders current settings and never displays a stored key value", async () => {
    installFakeApi();
    render(<App />);

    expect(await screen.findByDisplayValue("Wombat")).toBeTruthy();
    expect(screen.getByDisplayValue("voice-1")).toBeTruthy();
    expect((screen.getByLabelText("STT provider") as HTMLSelectElement).value).toBe("deepgram");
    expect((screen.getByLabelText("TTS provider") as HTMLSelectElement).value).toBe("elevenlabs");
    expect((screen.getByLabelText("Brevity") as HTMLSelectElement).value).toBe("balanced");
    expect((screen.getByLabelText("Warmth") as HTMLSelectElement).value).toBe("warm");
    expect((screen.getByLabelText("Directness") as HTMLSelectElement).value).toBe("blunt");
    expect((screen.getByLabelText("Humor") as HTMLSelectElement).value).toBe("dry");
    expect((screen.getByLabelText("Proactivity") as HTMLSelectElement).value).toBe("forward");

    // Configured/not-configured indicators are driven by the GET `keys`
    // booleans - elevenlabs is configured, deepgram/fish are not.
    expect(screen.getAllByText("Configured").length).toBe(1);
    expect(screen.getAllByText("Not configured").length).toBe(2);

    // The key inputs are write-only - they never carry a stored value, even
    // though the elevenlabs key is "configured" server-side.
    const elevenLabsKey = screen.getByLabelText("ElevenLabs API key") as HTMLInputElement;
    const deepgramKey = screen.getByLabelText("Deepgram API key") as HTMLInputElement;
    const fishKey = screen.getByLabelText("Fish Audio API key") as HTMLInputElement;
    expect(elevenLabsKey.value).toBe("");
    expect(deepgramKey.value).toBe("");
    expect(fishKey.value).toBe("");
    expect(elevenLabsKey.type).toBe("password");
  });
});

describe("App (TK-200 AC2: save)", () => {
  it("PUTs only touched settings + the touched key, then shows the restart notice", async () => {
    const { calls } = installFakeApi();
    render(<App />);
    await screen.findByDisplayValue("Wombat");

    fireEvent.change(screen.getByLabelText("Assistant name"), {
      target: { value: "New Name" },
    });
    fireEvent.change(screen.getByLabelText("STT provider"), {
      target: { value: "fish" },
    });
    fireEvent.change(screen.getByLabelText("TTS voice ID"), {
      target: { value: "voice-42" },
    });
    fireEvent.change(screen.getByLabelText("ElevenLabs API key"), {
      target: { value: "sk-live-abc" },
    });

    fireEvent.click(screen.getByRole("button", { name: /save/i }));

    await waitFor(() => {
      expect(calls.some((call) => call.method === "PUT" && call.url.endsWith("/settings"))).toBe(
        true,
      );
    });

    const settingsPuts = calls.filter(
      (call) => call.method === "PUT" && call.url.endsWith("/settings"),
    );
    expect(settingsPuts.length).toBe(1);
    expect(settingsPuts[0].body).toEqual({
      wombat_assistant_name: "New Name",
      wombat_stt_provider: "fish",
      wombat_tts_voice_id: "voice-42",
    });

    const keyPuts = calls.filter((call) => call.method === "PUT" && call.url.includes("/keys/"));
    expect(keyPuts.length).toBe(1);
    expect(keyPuts[0].url).toContain("/keys/elevenlabs");
    expect(keyPuts[0].body).toEqual({ key: "sk-live-abc" });

    expect(await screen.findByText("Restart Wombat to apply these changes.")).toBeTruthy();
    expect(screen.queryByText("Persona changes apply on the next turn.")).toBeNull();

    // The key input clears after a successful save - it never re-displays
    // what was typed, let alone a stored secret.
    expect((screen.getByLabelText("ElevenLabs API key") as HTMLInputElement).value).toBe("");
  });
});

describe("App (TK-200 AC3: notice split)", () => {
  it("shows only the hot-apply hint for a persona-only save; a later provider edit restores the restart notice", async () => {
    const { calls } = installFakeApi();
    render(<App />);
    await screen.findByDisplayValue("Wombat");

    fireEvent.change(screen.getByLabelText("Brevity"), { target: { value: "expansive" } });
    fireEvent.change(screen.getByLabelText("Warmth"), { target: { value: "neutral" } });
    fireEvent.click(screen.getByRole("button", { name: /save/i }));

    await waitFor(() => {
      expect(calls.some((call) => call.method === "PUT" && call.url.endsWith("/settings"))).toBe(
        true,
      );
    });

    const firstSettingsPut = calls.find(
      (call) => call.method === "PUT" && call.url.endsWith("/settings"),
    );
    expect(firstSettingsPut?.body).toEqual({
      wombat_persona_brevity: "expansive",
      wombat_persona_warmth: "neutral",
    });
    expect(calls.some((call) => call.method === "PUT" && call.url.includes("/keys/"))).toBe(
      false,
    );

    expect(await screen.findByText("Persona changes apply on the next turn.")).toBeTruthy();
    expect(screen.queryByText("Restart Wombat to apply these changes.")).toBeNull();

    // A provider edit in the same session still triggers the restart notice.
    fireEvent.change(screen.getByLabelText("STT provider"), { target: { value: "deepgram" } });
    fireEvent.click(screen.getByRole("button", { name: /save/i }));

    await waitFor(() => {
      const settingsPuts = calls.filter(
        (call) => call.method === "PUT" && call.url.endsWith("/settings"),
      );
      expect(settingsPuts.length).toBe(2);
    });

    expect(await screen.findByText("Restart Wombat to apply these changes.")).toBeTruthy();
  });
});
