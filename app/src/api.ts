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
// TK-305 (DEC-67i, mirrors TK-303's SettingsUpdate.wombat_asr_model): the DEC-64
// walkie-talkie local-ASR model Literal.
export type AsrModel = "tiny" | "base" | "small" | "medium";

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
  // TK-318 (DEC-69b): opts a voice turn into speaking the pane's actual composed reply
  // (sanitized, never the shaped-model-summary mouth) - restart-tier, default off.
  wombat_speak_full_replies: boolean | null;
  // TK-275 (DEC-58 c/d): the one-shot-captured push-to-talk binding
  // ("key:<code>"/"mouse:<button>", "" = unbound) - the renderer is the sole consumer, so no
  // restart notice accompanies a PUT of this field.
  wombat_ptt_binding: string | null;
  // TK-305 (DEC-67i, mirrors TK-292/303's admitted fields): the persona
  // display name and the four voice-provider/DEC-64 walkie-talkie knobs.
  wombat_user_name: string | null;
  wombat_stt_model: string | null;
  wombat_asr_model: AsrModel | null;
  wombat_reply_window_seconds: number | null;
  wombat_spoken_reply_max_chars: number | null;
  // TK-306 (DEC-67i second half, mirrors TK-304's quiet-hours fields): "HH:MM" or "" (both-or-
  // neither honored server-side by SettingsUpdate's pairwise validator).
  wombat_quiet_start: string | null;
  wombat_quiet_end: string | null;
  // TK-306 (DEC-67i second half, mirrors wombat.params.PARAMS_APP_EDITABLE's eight
  // wombat_param_* overlay keys, verbatim - TK-302 already admits these at the door). The two
  // time fields carry the bridge's "HH:MM:SS" shape; the renderer normalizes to/from "HH:MM".
  wombat_param_morning_brief_time: string | null;
  wombat_param_nightly_dream_time: string | null;
  wombat_param_urgency_threshold: number | null;
  wombat_param_per_class_daily_ceiling: number | null;
  wombat_param_decay_ttl_seconds: number | null;
  wombat_param_mouth_model_timeout_seconds: number | null;
  wombat_param_mouth_daily_token_ceiling: number | null;
  wombat_param_mouth_max_usd_per_drive: number | null;
  // TK-309 (DEC-68(b)): the ambient-observability per-channel consent gate - each defaults
  // False server-side (wombat.config.WombatConfig), restart-tier (no hot-apply).
  wombat_observe_screen: boolean | null;
  wombat_observe_webcam: boolean | null;
  wombat_observe_mic: boolean | null;
  // TK-319 (DEC-70(c)): the fourth ambient-observability channel - Screenpipe capture consent,
  // same shape as the TK-309 trio above. wombat_screenpipe_url is deliberately NOT exposed here
  // (operator .env-tier, not app-editable).
  wombat_observe_screenpipe: boolean | null;
}

// TK-306 (RULING v2.172 r4, `wombat.settings_app.api._timezone_view`, verbatim): the read-only
// GET /settings timezone object - never PUT-able (SettingsUpdate has no such field, extra=
// "forbid" 422s it at the door).
export type TimezoneSource = "env" | "system" | "unresolved";

export interface TimezoneInfo {
  name: string | null;
  source: TimezoneSource;
}

export interface SettingsResponse {
  // Widened with `Record<string, unknown>` because the backend view mirrors
  // the FULL `APP_EDITABLE_FIELDS` (e.g. it also carries the `wombat_param_*`
  // overlay rows, which this form does not edit) - `SettingsFields` types
  // only the fields this ticket's form reads/writes.
  settings: SettingsFields & Record<string, unknown>;
  keys: Record<KeyProvider, boolean>;
  // TK-306 (RULING v2.172 r4): read-only, no PUT counterpart.
  timezone: TimezoneInfo;
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

/**
 * TK-251 (verified against `src/wombat/integrations/gmail/triage.py`'s
 * `PriorityBand`): a stored gmail item, verbatim - the DEC-45 five-field
 * projection. No snippet/body field exists in the store - the UI must never
 * invent one.
 */
export type PriorityBand = "high" | "normal";

export interface GmailMessageItem {
  message_id: string;
  subject: string;
  sender: string;
  received_at: string;
  priority_band: PriorityBand;
}

export interface GmailResponse {
  items: GmailMessageItem[];
  storage_unavailable: boolean;
}

/**
 * `GET /external/gmail` (TK-246, read-only). Load-on-view only - no
 * polling/refresh machinery, no `limit` override in this client (the
 * backend's 50-item default stands).
 */
export async function getGmailMessages(): Promise<GmailResponse> {
  const response = await tokenedFetch("/external/gmail");
  if (!response.ok) {
    throw new Error(`GET /external/gmail failed: ${response.status}`);
  }
  return (await response.json()) as GmailResponse;
}

/**
 * TK-257 (DEC-50, verified against `src/wombat/settings_app/google_connect.py` +
 * `settings_app/api.py`'s `GET /google/status`/`POST /google/{service}/connect`):
 * the in-app Google OAuth connection shapes, verbatim. `GoogleServiceName` is the
 * closed two-value vocabulary (`GOOGLE_SERVICES`); `status` is the honest
 * non-crashing connection probe, `consent` is the (in-memory, per-service)
 * background consent-trigger state - `error` is present only while `consent`
 * is `"error"`.
 */
export type GoogleServiceName = "gmail" | "gcal";
export type GoogleConnectionStatus = "not_configured" | "not_connected" | "expired" | "connected";
export type GoogleConsentState = "idle" | "in_progress" | "error";

export interface GoogleServiceStatus {
  status: GoogleConnectionStatus;
  consent: GoogleConsentState;
  error?: string;
}

export type GoogleStatusResponse = Record<GoogleServiceName, GoogleServiceStatus>;

/** `GET /google/status`. Load-on-view / poll-while-consent-in-progress only - no other refresh. */
export async function getGoogleStatus(): Promise<GoogleStatusResponse> {
  const response = await tokenedFetch("/google/status");
  if (!response.ok) {
    throw new Error(`GET /google/status failed: ${response.status}`);
  }
  return (await response.json()) as GoogleStatusResponse;
}

/**
 * `POST /google/{service}/connect` - triggers the (possibly interactive) consent
 * flow. Succeeds with 202 (accepted, runs on a background thread); the backend
 * 409s if a consent flow is already running for this service.
 */
export async function connectGoogleService(service: GoogleServiceName): Promise<void> {
  const response = await tokenedFetch(`/google/${service}/connect`, { method: "POST" });
  if (!response.ok) {
    throw new Error(`POST /google/${service}/connect failed: ${response.status}`);
  }
}
