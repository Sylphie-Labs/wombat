// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";
import { resetBridgeCacheForTests } from "./api";
import { Header } from "./components/Header";

/**
 * TK-200 acceptance tests, against a fake bridge + fake fetch (no live API -
 * the live round trip is TK-201's smoke). Covers AC1 (load), AC2 (save +
 * restart notice), and AC3 (the DEC-37 persona hot-apply/restart notice
 * split).
 *
 * TK-249 AC1/AC2: the "App (TK-249 shell)" block below covers the re-housed
 * iteration-4 shell itself - header/rail/Today-as-landing/the collapsible
 * chat dock's down-state - on top of the TK-200 behavior above, which stays
 * byte-unchanged aside from now living behind nav-rail navigation.
 */

const PORT = 41417;
const TOKEN = "test-token";

type SettingsShape = Record<string, string | number | null>;

function baseSettings(): SettingsShape {
  return {
    wombat_stt_provider: "deepgram",
    wombat_tts_provider: "elevenlabs",
    wombat_tts_voice_id: "voice-1",
    wombat_stt_model: null,
    wombat_assistant_name: "Wombat",
    wombat_user_name: null,
    wombat_asr_model: null,
    wombat_reply_window_seconds: null,
    wombat_spoken_reply_max_chars: null,
    wombat_persona_brevity: "balanced",
    wombat_persona_warmth: "warm",
    wombat_persona_directness: "blunt",
    wombat_persona_humor: "dry",
    wombat_persona_proactivity: "forward",
    wombat_quiet_start: null,
    wombat_quiet_end: null,
    wombat_param_morning_brief_time: null,
    wombat_param_nightly_dream_time: null,
    wombat_param_urgency_threshold: null,
    wombat_param_per_class_daily_ceiling: null,
    wombat_param_decay_ttl_seconds: null,
    wombat_param_mouth_model_timeout_seconds: null,
    wombat_param_mouth_daily_token_ceiling: null,
    wombat_param_mouth_max_usd_per_drive: null,
  };
}

// TK-306: the fake GET /settings timezone object - a stand-in for
// `wombat.settings_app.api._timezone_view`'s shape, unrelated to the settings-store degrade
// path (it never depends on `store`/`currentSettings`).
function baseTimezone(): { name: string | null; source: string } {
  return { name: "America/Chicago", source: "system" };
}

interface FetchCall {
  url: string;
  method: string;
  body: unknown;
}

function installFakeApi(
  settings: SettingsShape = baseSettings(),
  keys: Record<string, boolean> = { elevenlabs: true, deepgram: false, fish: false },
  timezone: { name: string | null; source: string } = baseTimezone(),
): { calls: FetchCall[] } {
  (window as unknown as { wombatSettings: { getInfo: () => Promise<unknown> } }).wombatSettings =
    {
      getInfo: vi.fn().mockResolvedValue({ port: PORT, token: TOKEN }),
    };

  // TK-223: App now always mounts ChatPane, which reads window.wombatChat on
  // mount - stub it here (chat-absent baseline) so these TK-200-era tests,
  // which don't exercise chat, don't crash on an undefined bridge. TK-223's
  // own chat.test.ts/ChatPane.test.tsx cover the chat behavior itself.
  (window as unknown as { wombatChat: { getInfo: () => Promise<unknown> } }).wombatChat = {
    getInfo: vi.fn().mockResolvedValue(null),
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
      return Response.json({
        settings: { ...currentSettings },
        keys: { ...currentKeys },
        timezone,
      });
    }
    if (method === "PUT" && url.endsWith("/settings")) {
      // Mirrors wombat.settings_app.api.SettingsUpdate's Field bounds (TK-303/305) - an
      // out-of-range numeric 422s here exactly as the real API would, WITHOUT touching
      // currentSettings (so a subsequent GET still reflects the last-good value).
      const patch = body as SettingsShape;
      const window = patch.wombat_reply_window_seconds;
      const cap = patch.wombat_spoken_reply_max_chars;
      const outOfBounds =
        (typeof window === "number" && (window < 30 || window > 600)) ||
        (typeof cap === "number" && (cap < 200 || cap > 1200));
      if (outOfBounds) {
        return new Response(null, { status: 422 });
      }
      Object.assign(currentSettings, patch);
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

// TK-249: settings fields now live across four nav-rail categories instead
// of one page - these helpers navigate there before touching a field.
function gotoPersona(): void {
  fireEvent.click(screen.getByRole("button", { name: "Persona" }));
}
function gotoVoice(): void {
  fireEvent.click(screen.getByRole("button", { name: "Voice & Audio" }));
}
function gotoKeys(): void {
  fireEvent.click(screen.getByRole("button", { name: "API Keys" }));
}
function gotoSystem(): void {
  fireEvent.click(screen.getByRole("button", { name: "System" }));
}

describe("App (TK-200 AC1: load)", () => {
  it("renders current settings and never displays a stored key value", async () => {
    installFakeApi();
    render(<App />);

    gotoPersona();
    expect(await screen.findByDisplayValue("Wombat")).toBeTruthy();
    expect((screen.getByLabelText("Brevity") as HTMLSelectElement).value).toBe("balanced");
    expect((screen.getByLabelText("Warmth") as HTMLSelectElement).value).toBe("warm");
    expect((screen.getByLabelText("Directness") as HTMLSelectElement).value).toBe("blunt");
    expect((screen.getByLabelText("Humor") as HTMLSelectElement).value).toBe("dry");
    expect((screen.getByLabelText("Proactivity") as HTMLSelectElement).value).toBe("forward");

    gotoVoice();
    expect(screen.getByDisplayValue("voice-1")).toBeTruthy();
    expect((screen.getByLabelText("STT provider") as HTMLSelectElement).value).toBe("deepgram");
    expect((screen.getByLabelText("TTS provider") as HTMLSelectElement).value).toBe("elevenlabs");

    gotoKeys();
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

    gotoPersona();
    await screen.findByDisplayValue("Wombat");
    fireEvent.change(screen.getByLabelText("Assistant name"), {
      target: { value: "New Name" },
    });

    gotoVoice();
    fireEvent.change(screen.getByLabelText("STT provider"), {
      target: { value: "fish" },
    });
    fireEvent.change(screen.getByLabelText("TTS voice ID"), {
      target: { value: "voice-42" },
    });

    gotoKeys();
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
    gotoPersona();
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
    gotoVoice();
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

describe("App (TK-300 AC6: widened Brevity/Warmth/Humor selects)", () => {
  it("offers exactly the closed DEC-67b/c level sets on each widened select", async () => {
    installFakeApi();
    render(<App />);
    gotoPersona();
    await screen.findByDisplayValue("Wombat");

    const optionTexts = (select: HTMLSelectElement): string[] =>
      Array.from(select.options).map((option) => option.value);

    expect(optionTexts(screen.getByLabelText("Brevity") as HTMLSelectElement)).toEqual([
      "terse",
      "balanced",
      "expansive",
      "exhaustive",
    ]);
    expect(optionTexts(screen.getByLabelText("Warmth") as HTMLSelectElement)).toEqual([
      "reserved",
      "neutral",
      "warm",
      "affectionate",
    ]);
    expect(optionTexts(screen.getByLabelText("Humor") as HTMLSelectElement)).toEqual([
      "none",
      "dry",
      "playful",
      "comedian",
    ]);
  });

  it("saves a new humor level via the existing patch path, showing the hot-apply hint not the restart notice", async () => {
    const { calls } = installFakeApi();
    render(<App />);
    gotoPersona();
    await screen.findByDisplayValue("Wombat");

    fireEvent.change(screen.getByLabelText("Brevity"), { target: { value: "exhaustive" } });
    fireEvent.change(screen.getByLabelText("Warmth"), { target: { value: "affectionate" } });
    fireEvent.change(screen.getByLabelText("Humor"), { target: { value: "comedian" } });
    fireEvent.click(screen.getByRole("button", { name: /save/i }));

    await waitFor(() => {
      expect(calls.some((call) => call.method === "PUT" && call.url.endsWith("/settings"))).toBe(
        true,
      );
    });

    const settingsPut = calls.find(
      (call) => call.method === "PUT" && call.url.endsWith("/settings"),
    );
    expect(settingsPut?.body).toEqual({
      wombat_persona_brevity: "exhaustive",
      wombat_persona_warmth: "affectionate",
      wombat_persona_humor: "comedian",
    });

    expect(await screen.findByText("Persona changes apply on the next turn.")).toBeTruthy();
    expect(screen.queryByText("Restart Wombat to apply these changes.")).toBeNull();
  });
});

describe("App (TK-301 AC3: eager proactivity option)", () => {
  it("offers eager as a fourth Proactivity option alongside minimal/balanced/forward", async () => {
    installFakeApi();
    render(<App />);
    gotoPersona();
    await screen.findByDisplayValue("Wombat");

    const optionTexts = (select: HTMLSelectElement): string[] =>
      Array.from(select.options).map((option) => option.value);

    expect(optionTexts(screen.getByLabelText("Proactivity") as HTMLSelectElement)).toEqual([
      "minimal",
      "balanced",
      "forward",
      "eager",
    ]);
  });

  it("saves eager via the existing patch path as a hot-apply persona change, not a restart", async () => {
    const { calls } = installFakeApi();
    render(<App />);
    gotoPersona();
    await screen.findByDisplayValue("Wombat");

    fireEvent.change(screen.getByLabelText("Proactivity"), { target: { value: "eager" } });
    fireEvent.click(screen.getByRole("button", { name: /save/i }));

    await waitFor(() => {
      expect(calls.some((call) => call.method === "PUT" && call.url.endsWith("/settings"))).toBe(
        true,
      );
    });

    const settingsPut = calls.find(
      (call) => call.method === "PUT" && call.url.endsWith("/settings"),
    );
    expect(settingsPut?.body).toEqual({ wombat_persona_proactivity: "eager" });

    expect(await screen.findByText("Persona changes apply on the next turn.")).toBeTruthy();
    expect(screen.queryByText("Restart Wombat to apply these changes.")).toBeNull();
  });
});

describe("App (TK-305 AC1: persona view gains Your name)", () => {
  it("renders '' when wombat_user_name is unset, and saving it shows the restart notice", async () => {
    const { calls } = installFakeApi();
    render(<App />);
    gotoPersona();
    await screen.findByDisplayValue("Wombat");

    expect((screen.getByLabelText("Your name") as HTMLInputElement).value).toBe("");

    fireEvent.change(screen.getByLabelText("Your name"), { target: { value: "Jim" } });
    fireEvent.click(screen.getByRole("button", { name: /save/i }));

    await waitFor(() => {
      const settingsPuts = calls.filter(
        (call) => call.method === "PUT" && call.url.endsWith("/settings"),
      );
      expect(settingsPuts.length).toBe(1);
      expect(settingsPuts[0].body).toEqual({ wombat_user_name: "Jim" });
    });

    expect(await screen.findByText("Restart Wombat to apply these changes.")).toBeTruthy();
    expect(screen.queryByText("Persona changes apply on the next turn.")).toBeNull();
  });

  it("renders a stored Your name value", async () => {
    installFakeApi({ ...baseSettings(), wombat_user_name: "Jim" });
    render(<App />);
    gotoPersona();

    expect(await screen.findByDisplayValue("Jim")).toBeTruthy();
  });
});

describe("App (TK-305 AC2: voice view gains cloud STT model / local ASR model / reply window / spoken cap)", () => {
  it("PUTs exactly the touched voice fields with correct types, and shows the restart notice", async () => {
    const { calls } = installFakeApi();
    render(<App />);
    gotoVoice();
    await screen.findByLabelText("STT provider");

    expect((screen.getByLabelText("Local ASR model") as HTMLSelectElement).value).toBe("base");
    expect((screen.getByLabelText("Reply window (s)") as HTMLInputElement).value).toBe("120");
    expect((screen.getByLabelText("Spoken reply cap (chars)") as HTMLInputElement).value).toBe(
      "400",
    );

    fireEvent.change(screen.getByLabelText("Cloud STT model"), {
      target: { value: "nova-2" },
    });
    fireEvent.change(screen.getByLabelText("Local ASR model"), {
      target: { value: "small" },
    });
    fireEvent.change(screen.getByLabelText("Reply window (s)"), {
      target: { value: "180" },
    });
    fireEvent.change(screen.getByLabelText("Spoken reply cap (chars)"), {
      target: { value: "500" },
    });
    fireEvent.click(screen.getByRole("button", { name: /save/i }));

    await waitFor(() => {
      const settingsPuts = calls.filter(
        (call) => call.method === "PUT" && call.url.endsWith("/settings"),
      );
      expect(settingsPuts.length).toBe(1);
      expect(settingsPuts[0].body).toEqual({
        wombat_stt_model: "nova-2",
        wombat_asr_model: "small",
        wombat_reply_window_seconds: 180,
        wombat_spoken_reply_max_chars: 500,
      });
    });

    expect(await screen.findByText("Restart Wombat to apply these changes.")).toBeTruthy();
  });
});

describe("App (TK-305 AC3: out-of-bounds numeric input 422s and reverts)", () => {
  it("surfaces the 422 via the save-error line and reverts to the stored value on reload", async () => {
    const { calls } = installFakeApi();
    const { unmount } = render(<App />);
    gotoVoice();
    await screen.findByLabelText("STT provider");

    fireEvent.change(screen.getByLabelText("Reply window (s)"), { target: { value: "10" } });
    fireEvent.change(screen.getByLabelText("Spoken reply cap (chars)"), {
      target: { value: "50" },
    });
    fireEvent.click(screen.getByRole("button", { name: /save/i }));

    await waitFor(() => {
      expect(calls.some((call) => call.method === "PUT" && call.url.endsWith("/settings"))).toBe(
        true,
      );
    });
    expect(await screen.findByText("Save failed: PUT /settings failed: 422")).toBeTruthy();

    // The failed PUT never persisted server-side - a fresh load (simulated by
    // unmount+remount, since this app has no reload button) shows the
    // still-stored defaults, not the rejected 10/50.
    unmount();
    render(<App />);
    gotoVoice();

    expect(await screen.findByLabelText("Reply window (s)")).toHaveProperty("value", "120");
    expect(screen.getByLabelText("Spoken reply cap (chars)")).toHaveProperty("value", "400");
  });
});

describe("App (TK-306 AC1: system view gains Briefs & interruptions / Limits panels)", () => {
  it("keeps RuntimeControls rendering above the two new panels", async () => {
    installFakeApi();
    render(<App />);
    gotoSystem();
    await screen.findByLabelText("Brief time");

    expect(screen.getByRole("heading", { name: "Runtime" })).toBeTruthy();
    expect(screen.getByRole("heading", { name: "Briefs & interruptions" })).toBeTruthy();
    expect(screen.getByRole("heading", { name: "Limits" })).toBeTruthy();

    const text = document.body.textContent ?? "";
    expect(text.indexOf("Runtime")).toBeLessThan(text.indexOf("Briefs & interruptions"));
    expect(text.indexOf("Briefs & interruptions")).toBeLessThan(text.indexOf("Limits"));
  });

  it("renders unset param overrides blank with a pinned-default placeholder", async () => {
    installFakeApi();
    render(<App />);
    gotoSystem();
    await screen.findByLabelText("Brief time");

    const briefTime = screen.getByLabelText("Brief time") as HTMLInputElement;
    expect(briefTime.value).toBe("");
    expect(briefTime.placeholder).toBe("default 07:00");

    const reflectionTime = screen.getByLabelText("Reflection time") as HTMLInputElement;
    expect(reflectionTime.value).toBe("");
    expect(reflectionTime.placeholder).toBe("default 02:00");

    expect(
      (screen.getByLabelText("Urgency threshold") as HTMLInputElement).placeholder,
    ).toBe("default 0.75");
    expect(
      (screen.getByLabelText("Max voice interruptions per sender class per day") as HTMLInputElement)
        .placeholder,
    ).toBe("default 3");
    expect((screen.getByLabelText("Item decay (hours)") as HTMLInputElement).placeholder).toBe(
      "default 24",
    );
    expect(
      (screen.getByLabelText("Model response wait (s)") as HTMLInputElement).placeholder,
    ).toBe("default 10");
    expect(
      (screen.getByLabelText("Daily token ceiling") as HTMLInputElement).placeholder,
    ).toBe("default 100000");
    expect(
      (screen.getByLabelText("Per-conversation spend cap (USD)") as HTMLInputElement).placeholder,
    ).toBe("default 0.50");

    // Quiet hours carry no pinned default - blank means "off", not "unset".
    expect((screen.getByLabelText("Quiet hours start") as HTMLInputElement).value).toBe("");
    expect((screen.getByLabelText("Quiet hours end") as HTMLInputElement).value).toBe("");
  });
});

describe("App (TK-306 AC2: brief time + decay save)", () => {
  it("PUTs the converted HH:MM:00 time and the x3600 decay seconds, and shows the restart notice", async () => {
    const { calls } = installFakeApi();
    render(<App />);
    gotoSystem();
    await screen.findByLabelText("Brief time");

    fireEvent.change(screen.getByLabelText("Brief time"), { target: { value: "06:30" } });
    fireEvent.change(screen.getByLabelText("Item decay (hours)"), { target: { value: "48" } });
    fireEvent.click(screen.getByRole("button", { name: /save/i }));

    await waitFor(() => {
      const settingsPuts = calls.filter(
        (call) => call.method === "PUT" && call.url.endsWith("/settings"),
      );
      expect(settingsPuts.length).toBe(1);
      expect(settingsPuts[0].body).toEqual({
        wombat_param_morning_brief_time: "06:30:00",
        wombat_param_decay_ttl_seconds: 172800,
      });
    });

    expect(await screen.findByText("Restart Wombat to apply these changes.")).toBeTruthy();
  });
});

describe("App (repair: clearing the ceiling override PUTs null, not 0)", () => {
  it("PUTs null when a previously-set ceiling field is cleared back to blank", async () => {
    const { calls } = installFakeApi({
      ...baseSettings(),
      wombat_param_per_class_daily_ceiling: 5,
    });
    render(<App />);
    gotoSystem();
    const ceilingField = await screen.findByLabelText(
      "Max voice interruptions per sender class per day",
    );
    expect((ceilingField as HTMLInputElement).value).toBe("5");

    fireEvent.change(ceilingField, { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: /save/i }));

    await waitFor(() => {
      const settingsPuts = calls.filter(
        (call) => call.method === "PUT" && call.url.endsWith("/settings"),
      );
      expect(settingsPuts.length).toBe(1);
      expect(settingsPuts[0].body).toEqual({
        wombat_param_per_class_daily_ceiling: null,
      });
    });
  });
});

describe("App (TK-306 AC3: read-only timezone line)", () => {
  it("renders the timezone name/source with no control", async () => {
    installFakeApi(undefined, undefined, { name: "America/Chicago", source: "env" });
    render(<App />);
    gotoSystem();

    expect(await screen.findByText("Timezone: America/Chicago (env)")).toBeTruthy();
    expect(screen.queryByLabelText(/timezone/i)).toBeNull();
  });
});

describe("App (TK-306 AC4: storage-unavailable GET degrade)", () => {
  it("still renders the two new panels, blank with placeholders, when every field is null", async () => {
    const nullSettings: SettingsShape = {
      wombat_stt_provider: null,
      wombat_tts_provider: null,
      wombat_tts_voice_id: null,
      wombat_stt_model: null,
      wombat_assistant_name: null,
      wombat_user_name: null,
      wombat_asr_model: null,
      wombat_reply_window_seconds: null,
      wombat_spoken_reply_max_chars: null,
      wombat_persona_brevity: null,
      wombat_persona_warmth: null,
      wombat_persona_directness: null,
      wombat_persona_humor: null,
      wombat_persona_proactivity: null,
      wombat_quiet_start: null,
      wombat_quiet_end: null,
      wombat_param_morning_brief_time: null,
      wombat_param_nightly_dream_time: null,
      wombat_param_urgency_threshold: null,
      wombat_param_per_class_daily_ceiling: null,
      wombat_param_decay_ttl_seconds: null,
      wombat_param_mouth_model_timeout_seconds: null,
      wombat_param_mouth_daily_token_ceiling: null,
      wombat_param_mouth_max_usd_per_drive: null,
    };
    installFakeApi(nullSettings, { elevenlabs: false, deepgram: false, fish: false });
    render(<App />);
    gotoSystem();

    const briefTime = await screen.findByLabelText("Brief time");
    expect((briefTime as HTMLInputElement).value).toBe("");
    expect((briefTime as HTMLInputElement).placeholder).toBe("default 07:00");
    expect(screen.getByRole("heading", { name: "Briefs & interruptions" })).toBeTruthy();
    expect(screen.getByRole("heading", { name: "Limits" })).toBeTruthy();
  });
});

describe("App (TK-249 shell AC1: header/rail/chat dock/Today landing)", () => {
  it("renders the header mark+wordmark, Today as the default landing view, and every nav category", async () => {
    installFakeApi();
    render(<App />);

    expect(screen.getByText("wombat")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Today" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Persona" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Voice & Audio" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "API Keys" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "System" })).toBeTruthy();

    // Today is the default landing view - its honest placeholder sections
    // render without navigating anywhere, and none of the settings fields do.
    expect(screen.getByText("Morning brief")).toBeTruthy();
    expect(screen.getByText("Upcoming")).toBeTruthy();
    expect(screen.getByText("Inbox highlights")).toBeTruthy();
    expect(screen.getByText("Steward's notepad")).toBeTruthy();
    expect(screen.queryByLabelText("Assistant name")).toBeNull();
  });

  it("keeps the chat pane mounted on every view, honestly rendering its down-state", async () => {
    installFakeApi(); // wombatChat.getInfo() resolves null - the chat-absent baseline.
    render(<App />);

    expect(await screen.findByText(/wombat is not running/i)).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Persona" }));
    expect(screen.getByText(/wombat is not running/i)).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "System" }));
    expect(screen.getByText(/wombat is not running/i)).toBeTruthy();
  });

  it("collapses and re-expands the chat dock without unmounting the shell", async () => {
    installFakeApi();
    render(<App />);
    await screen.findByText(/wombat is not running/i);

    fireEvent.click(screen.getByRole("button", { name: "Hide chat" }));
    expect(screen.queryByText(/wombat is not running/i)).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Show chat" }));
    expect(await screen.findByText(/wombat is not running/i)).toBeTruthy();
  });

  it("switches settings categories on nav click, rendering only the active category's fields", async () => {
    installFakeApi();
    render(<App />);

    gotoPersona();
    await screen.findByDisplayValue("Wombat");
    expect(screen.getByLabelText("Brevity")).toBeTruthy();
    expect(screen.queryByLabelText("STT provider")).toBeNull();

    gotoVoice();
    expect(screen.getByLabelText("STT provider")).toBeTruthy();
    expect(screen.queryByLabelText("Brevity")).toBeNull();

    gotoKeys();
    expect(screen.getByLabelText("ElevenLabs API key")).toBeTruthy();
    expect(screen.queryByLabelText("STT provider")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "System" }));
    expect(screen.getByRole("button", { name: /restart wombat/i })).toBeTruthy();
    expect(screen.queryByLabelText("ElevenLabs API key")).toBeNull();
  });
});

/**
 * TK-263 (ISS-16): the header's status chip must reflect a real chat-port
 * round trip, not just handshake-file presence. Exercised against `Header`
 * directly (no App/settings bridge needed) with a stubbed
 * `window.wombatChat` and a stubbed global `fetch`.
 */
describe("Header (TK-263: liveness truthfulness)", () => {
  function stubWombatChat(getInfo: () => Promise<{ port: number; token: string } | null>): void {
    (window as unknown as { wombatChat: { getInfo: () => Promise<unknown> } }).wombatChat = {
      getInfo: vi.fn(getInfo),
    };
  }

  it("shows Offline when getInfo resolves handshake info but the port probe fetch rejects (ISS-15 live state)", async () => {
    stubWombatChat(async () => ({ port: PORT, token: TOKEN }));
    const fetchMock = vi.fn().mockRejectedValue(new Error("ECONNREFUSED"));
    vi.stubGlobal("fetch", fetchMock);

    render(<Header />);

    expect(await screen.findByText("Offline")).toBeTruthy();
    expect(screen.queryByText("Running")).toBeNull();
  });

  it("shows Running when the port probe fetch resolves with any HTTP status, and never POSTs /chat", async () => {
    stubWombatChat(async () => ({ port: PORT, token: TOKEN }));
    const fetchMock = vi.fn().mockResolvedValue(new Response(null, { status: 404 }));
    vi.stubGlobal("fetch", fetchMock);

    render(<Header />);

    expect(await screen.findByText("Running")).toBeTruthy();
    for (const call of fetchMock.mock.calls) {
      const url = String(call[0]);
      const method = (call[1] as RequestInit | undefined)?.method ?? "GET";
      expect(url.endsWith("/chat") && method === "POST").toBe(false);
    }
  });

  it("flips Running -> Offline on the next ~15s poll without a reload, and clears the interval on unmount", async () => {
    vi.useFakeTimers();
    stubWombatChat(async () => ({ port: PORT, token: TOKEN }));
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(new Response(null, { status: 200 }))
      .mockRejectedValueOnce(new Error("ECONNREFUSED"));
    vi.stubGlobal("fetch", fetchMock);

    const { unmount } = render(<Header />);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(screen.getByText("Running")).toBeTruthy();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(15_000);
    });
    expect(screen.getByText("Offline")).toBeTruthy();

    const callsAtUnmount = fetchMock.mock.calls.length;
    unmount();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });
    expect(fetchMock.mock.calls.length).toBe(callsAtUnmount);

    vi.useRealTimers();
  });
});
