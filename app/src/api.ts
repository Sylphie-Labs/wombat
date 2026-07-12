/**
 * TK-200 (Q-110(e) ruled shape): the tokened fetch wrapper the settings form
 * rides. `window.wombatSettings.getInfo()` (the TK-199 preload bridge,
 * pinned - never a URL parameter) resolves the loopback port + per-launch
 * token EXACTLY ONCE per renderer session; every request after that reuses
 * the cached value and sets it as `X-Wombat-Token` (the Q-109(d) API shape -
 * every route requires this header).
 *
 * The two backend routes this wraps (`src/wombat/settings_app/api.py`):
 *   - `GET /settings` -> `{settings: {<APP_EDITABLE_FIELDS>: value|null}, keys: {<KEY_PROVIDERS>: bool}}`
 *   - `PUT /settings` body = only the touched fields (unknown keys 422)
 *   - `PUT /keys/{provider}` body = `{key}`, write-only - the response never
 *     echoes a secret back, so this module never has one to expose either.
 */

export interface BridgeInfo {
  readonly port: number;
  readonly token: string;
}

declare global {
  interface Window {
    wombatSettings: {
      getInfo(): Promise<BridgeInfo>;
    };
  }
}

// wombat.settings_app.api.KEY_PROVIDERS, verbatim - the closed cloud
// voice-provider key vocabulary ("local" has no key to store).
export const KEY_PROVIDERS = ["elevenlabs", "deepgram", "fish"] as const;
export type KeyProvider = (typeof KEY_PROVIDERS)[number];

// wombat.settings_app.api.SettingsUpdate's Literal vocabularies, verbatim.
export type Provider = "local" | "deepgram" | "elevenlabs" | "fish";
export type Brevity = "terse" | "balanced" | "expansive";
export type Warmth = "reserved" | "neutral" | "warm";
export type Directness = "gentle" | "plain" | "blunt";
export type Humor = "none" | "dry";
export type Proactivity = "minimal" | "balanced" | "forward";

export interface SettingsFields {
  wombat_stt_provider: Provider | null;
  wombat_tts_provider: Provider | null;
  wombat_tts_voice_id: string | null;
  wombat_assistant_name: string | null;
  wombat_persona_brevity: Brevity | null;
  wombat_persona_warmth: Warmth | null;
  wombat_persona_directness: Directness | null;
  wombat_persona_humor: Humor | null;
  wombat_persona_proactivity: Proactivity | null;
  // TK-224 (Q-111(b)): the newly-admitted app-editable field - a bootstrap-read bool gating
  // voice delivery (wombat.bootstrap), so a save PUTting it carries the DEC-32 restart notice.
  wombat_voice_enabled: boolean | null;
}

export interface SettingsResponse {
  // Widened with `Record<string, unknown>` because the backend view mirrors
  // the FULL `APP_EDITABLE_FIELDS` (e.g. it also carries `wombat_stt_model`,
  // which this form does not edit) - `SettingsFields` types only the fields
  // this ticket's form reads/writes.
  settings: SettingsFields & Record<string, unknown>;
  keys: Record<KeyProvider, boolean>;
}

export type SettingsPatch = Partial<SettingsFields>;

let cachedBridgeInfo: Promise<BridgeInfo> | null = null;

function bridgeInfo(): Promise<BridgeInfo> {
  cachedBridgeInfo ??= window.wombatSettings.getInfo();
  return cachedBridgeInfo;
}

/** Test-only escape hatch - a fresh render should re-resolve the bridge. */
export function resetBridgeCacheForTests(): void {
  cachedBridgeInfo = null;
}

async function tokenedFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const { port, token } = await bridgeInfo();
  const headers = new Headers(init.headers);
  headers.set("X-Wombat-Token", token);
  if (init.body !== undefined) {
    headers.set("Content-Type", "application/json");
  }
  // The loopback origin is derived entirely from the bridge-supplied port -
  // never a literal external host (DEC-29: no non-loopback fetch target
  // anywhere in this module).
  return fetch(`http://127.0.0.1:${port}${path}`, { ...init, headers });
}

export async function getSettings(): Promise<SettingsResponse> {
  const response = await tokenedFetch("/settings");
  if (!response.ok) {
    throw new Error(`GET /settings failed: ${response.status}`);
  }
  return (await response.json()) as SettingsResponse;
}

export async function putSettings(patch: SettingsPatch): Promise<void> {
  const response = await tokenedFetch("/settings", {
    method: "PUT",
    body: JSON.stringify(patch),
  });
  if (!response.ok) {
    throw new Error(`PUT /settings failed: ${response.status}`);
  }
}

export async function putKey(provider: KeyProvider, key: string): Promise<void> {
  const response = await tokenedFetch(`/keys/${provider}`, {
    method: "PUT",
    body: JSON.stringify({ key }),
  });
  if (!response.ok) {
    throw new Error(`PUT /keys/${provider} failed: ${response.status}`);
  }
}

/**
 * TK-250 (RULING r2, contract v2.75 - binding payload shape): a stored gcal
 * item, verbatim. No location/attendee/source field exists in the store -
 * the UI must never invent one.
 */
export interface CalendarEventItem {
  event_id: string;
  title: string;
  start: string;
  end: string;
  all_day: boolean;
}

export interface CalendarResponse {
  items: CalendarEventItem[];
  storage_unavailable: boolean;
}

/**
 * `GET /external/calendar` (TK-246, read-only). Load-on-view only - no
 * polling/refresh machinery, no `window_hours` override in this client
 * (the backend's 168-hour default stands).
 */
export async function getCalendarEvents(): Promise<CalendarResponse> {
  const response = await tokenedFetch("/external/calendar");
  if (!response.ok) {
    throw new Error(`GET /external/calendar failed: ${response.status}`);
  }
  return (await response.json()) as CalendarResponse;
}
