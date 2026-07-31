import { useEffect, useState } from "react";

import {
  AudioPanel,
  Button,
  ChatDock,
  Field,
  GoogleConnections,
  Header,
  Indicator,
  NavRail,
  Panel,
  RuntimeControls,
  Select,
  Today,
  type SelectOption,
  type ViewId,
} from "./components";
import { usePushToTalk } from "./ptt";
import { font, ink, interactive, radius, surface } from "./tokens";
import {
  getSettings,
  putKey,
  putSettings,
  KEY_PROVIDERS,
  type AsrModel,
  type Brevity,
  type Directness,
  type Humor,
  type KeyProvider,
  type Proactivity,
  type Provider,
  type SettingsFields,
  type SettingsPatch,
  type TimezoneInfo,
  type Warmth,
} from "./api";

/**
 * TK-200 (re-housed by TK-249 into the approved iteration-4 shell): the
 * renderer's one settings form. Loads via `GET /settings`, saves via
 * `PUT /settings` + `PUT /keys/{provider}` (Q-110(e) ruled shape) - both
 * BYTE-UNCHANGED from TK-200/224/239. Plain `useState` form + `view` state -
 * no state framework, no router (complexity_budget). The four settings
 * categories (Persona, Voice & Audio, API Keys, System) and Today all read
 * from this single shared form/touched/keyInputs state, so a field changed
 * on one category page and saved from another still round-trips correctly.
 */

interface FormState {
  wombat_assistant_name: string;
  wombat_user_name: string;
  wombat_stt_provider: Provider;
  wombat_tts_provider: Provider;
  wombat_tts_voice_id: string;
  wombat_stt_model: string;
  wombat_asr_model: AsrModel;
  wombat_reply_window_seconds: number;
  wombat_spoken_reply_max_chars: number;
  // TK-318 (DEC-69b): the pane's-actual-reply voice opt-in - a real bool, defaulting False.
  wombat_speak_full_replies: boolean;
  wombat_persona_brevity: Brevity;
  wombat_persona_warmth: Warmth;
  wombat_persona_directness: Directness;
  wombat_persona_humor: Humor;
  wombat_persona_proactivity: Proactivity;
  // TK-306 (DEC-67i second half): the System view's "Briefs & interruptions" / "Limits" panels.
  // Every field here is a plain string, "" meaning unset - unlike the rest of this form, an
  // unset wombat_param_* override renders BLANK with a pinned-default placeholder (AC1), never
  // pre-filled with the default itself (contrast the DEFAULTS-fallback fields above). HH:MM
  // fields hold the renderer's "HH:MM" shape; wombat_param_decay_ttl_hours is a UI-only field
  // (the bridge stores wombat_param_decay_ttl_seconds - x3600 to/from at the buildPatch/
  // toFormState edges, per the ticket's recorded time convention).
  wombat_param_morning_brief_time: string;
  wombat_param_nightly_dream_time: string;
  wombat_param_urgency_threshold: string;
  wombat_param_per_class_daily_ceiling: string;
  wombat_param_decay_ttl_hours: string;
  wombat_quiet_start: string;
  wombat_quiet_end: string;
  wombat_param_mouth_model_timeout_seconds: string;
  wombat_param_mouth_daily_token_ceiling: string;
  wombat_param_mouth_max_usd_per_drive: string;
  // TK-309 (DEC-68(b)): the ambient-observability consent gate - real bools (the
  // DEFAULTS-fallback pattern above, not the wombat_param_* placeholder-blank pattern), each
  // defaulting False.
  wombat_observe_screen: boolean;
  wombat_observe_webcam: boolean;
  wombat_observe_mic: boolean;
}

type FormField = keyof FormState;

// TK-306: the wombat_params.yaml v9 shipped defaults (src/wombat/wombat_params.yaml), shown as
// placeholder hints when an override is unset - NOT a FormState fallback (which stays "" for
// these fields; see the FormState docstring above).
const PARAM_PLACEHOLDERS: Record<
  | "wombat_param_morning_brief_time"
  | "wombat_param_nightly_dream_time"
  | "wombat_param_urgency_threshold"
  | "wombat_param_per_class_daily_ceiling"
  | "wombat_param_decay_ttl_hours"
  | "wombat_param_mouth_model_timeout_seconds"
  | "wombat_param_mouth_daily_token_ceiling"
  | "wombat_param_mouth_max_usd_per_drive",
  string
> = {
  wombat_param_morning_brief_time: "07:00",
  wombat_param_nightly_dream_time: "02:00",
  wombat_param_urgency_threshold: "0.75",
  wombat_param_per_class_daily_ceiling: "3",
  wombat_param_decay_ttl_hours: "24",
  wombat_param_mouth_model_timeout_seconds: "10",
  wombat_param_mouth_daily_token_ceiling: "100000",
  wombat_param_mouth_max_usd_per_drive: "0.50",
};

// DEFAULT_MATRIX (src/wombat/persona/matrix.py) - the fallback shown when a
// field is `null` (never customized) in wombat.settings.json.
// TK-305: the new fields' fallbacks mirror WombatConfig's own field defaults
// (src/wombat/config.py) - wombat_asr_model="base", wombat_reply_window_seconds=120.0,
// wombat_spoken_reply_max_chars=400; wombat_stt_model/wombat_user_name follow the
// established "" fallback for optional/plain-str fields (wombat_tts_voice_id, above).
const DEFAULTS: FormState = {
  wombat_assistant_name: "",
  wombat_user_name: "",
  wombat_stt_provider: "local",
  wombat_tts_provider: "local",
  wombat_tts_voice_id: "",
  wombat_stt_model: "",
  wombat_asr_model: "base",
  wombat_reply_window_seconds: 120,
  wombat_spoken_reply_max_chars: 400,
  wombat_speak_full_replies: false,
  wombat_persona_brevity: "terse",
  wombat_persona_warmth: "reserved",
  wombat_persona_directness: "plain",
  wombat_persona_humor: "none",
  wombat_persona_proactivity: "balanced",
  // TK-306: "" (unset/blank) across the board - see the FormState docstring above.
  wombat_param_morning_brief_time: "",
  wombat_param_nightly_dream_time: "",
  wombat_param_urgency_threshold: "",
  wombat_param_per_class_daily_ceiling: "",
  wombat_param_decay_ttl_hours: "",
  wombat_quiet_start: "",
  wombat_quiet_end: "",
  wombat_param_mouth_model_timeout_seconds: "",
  wombat_param_mouth_daily_token_ceiling: "",
  wombat_param_mouth_max_usd_per_drive: "",
  // TK-309 (DEC-68(b)): false, mirroring WombatConfig's own field defaults.
  wombat_observe_screen: false,
  wombat_observe_webcam: false,
  wombat_observe_mic: false,
};

const PERSONA_FIELDS: readonly FormField[] = [
  "wombat_persona_brevity",
  "wombat_persona_warmth",
  "wombat_persona_directness",
  "wombat_persona_humor",
  "wombat_persona_proactivity",
];

const RESTART_FIELDS: readonly FormField[] = [
  "wombat_assistant_name",
  "wombat_user_name",
  "wombat_stt_provider",
  "wombat_tts_provider",
  "wombat_tts_voice_id",
  "wombat_stt_model",
  "wombat_asr_model",
  "wombat_reply_window_seconds",
  "wombat_spoken_reply_max_chars",
  // TK-318 (DEC-69b): restart-tier (SpeechShapeStage reads it once at construction).
  "wombat_speak_full_replies",
  // TK-306 (DEC-67i second half): all ten new System-view fields are restart-tier - the
  // briefing's binding ruling, no hot-apply for any of them.
  "wombat_param_morning_brief_time",
  "wombat_param_nightly_dream_time",
  "wombat_param_urgency_threshold",
  "wombat_param_per_class_daily_ceiling",
  "wombat_param_decay_ttl_hours",
  "wombat_quiet_start",
  "wombat_quiet_end",
  "wombat_param_mouth_model_timeout_seconds",
  "wombat_param_mouth_daily_token_ceiling",
  "wombat_param_mouth_max_usd_per_drive",
  // TK-309 (DEC-68(b)): restart-to-apply, no hot-apply.
  "wombat_observe_screen",
  "wombat_observe_webcam",
  "wombat_observe_mic",
];

// wombat.settings_app.api.SettingsUpdate's provider Literal, verbatim.
const PROVIDER_OPTIONS: SelectOption[] = [
  { value: "local", label: "Local" },
  { value: "deepgram", label: "Deepgram" },
  { value: "elevenlabs", label: "ElevenLabs" },
  { value: "fish", label: "Fish Audio" },
];

// src/wombat/persona/matrix.py's five closed axes, verbatim named levels.
// TK-300 (DEC-67b/c): brevity gains Exhaustive, warmth gains Affectionate, humor gains
// Playful + Comedian.
const BREVITY_OPTIONS: SelectOption[] = [
  { value: "terse", label: "Terse" },
  { value: "balanced", label: "Balanced" },
  { value: "expansive", label: "Expansive" },
  { value: "exhaustive", label: "Exhaustive" },
];
const WARMTH_OPTIONS: SelectOption[] = [
  { value: "reserved", label: "Reserved" },
  { value: "neutral", label: "Neutral" },
  { value: "warm", label: "Warm" },
  { value: "affectionate", label: "Affectionate" },
];
const DIRECTNESS_OPTIONS: SelectOption[] = [
  { value: "gentle", label: "Gentle" },
  { value: "plain", label: "Plain" },
  { value: "blunt", label: "Blunt" },
];
const HUMOR_OPTIONS: SelectOption[] = [
  { value: "none", label: "None" },
  { value: "dry", label: "Dry" },
  { value: "playful", label: "Playful" },
  { value: "comedian", label: "Comedian (jokes all the time)" },
];
// TK-301 (DEC-67c): proactivity gains a fourth, more-forward-than-forward level.
const PROACTIVITY_OPTIONS: SelectOption[] = [
  { value: "minimal", label: "Minimal" },
  { value: "balanced", label: "Balanced" },
  { value: "forward", label: "Forward" },
  { value: "eager", label: "Eager (very forward)" },
];

// wombat.settings_app.api.SettingsUpdate.wombat_asr_model's Literal, verbatim (TK-303/305).
const ASR_MODEL_OPTIONS: SelectOption[] = [
  { value: "tiny", label: "Tiny" },
  { value: "base", label: "Base" },
  { value: "small", label: "Small" },
  { value: "medium", label: "Medium" },
];

// TK-309 (DEC-68(b)): the ambient-observability consent toggles' Off/On vocabulary - App.tsx
// has no toggle/checkbox primitive (DEC-67's extension-only posture forbids minting one), so
// these ride the existing Select component like every other closed-vocabulary field here.
const ON_OFF_OPTIONS: SelectOption[] = [
  { value: "off", label: "Off" },
  { value: "on", label: "On" },
];

const KEY_PROVIDER_LABELS: Record<KeyProvider, string> = {
  elevenlabs: "ElevenLabs API key",
  deepgram: "Deepgram API key",
  fish: "Fish Audio API key",
};

const EMPTY_KEY_INPUTS: Record<KeyProvider, string> = {
  elevenlabs: "",
  deepgram: "",
  fish: "",
};

const EMPTY_KEYS_CONFIGURED: Record<KeyProvider, boolean> = {
  elevenlabs: false,
  deepgram: false,
  fish: false,
};

function toFormState(settings: SettingsFields): FormState {
  return {
    wombat_assistant_name: settings.wombat_assistant_name ?? DEFAULTS.wombat_assistant_name,
    wombat_user_name: settings.wombat_user_name ?? DEFAULTS.wombat_user_name,
    wombat_stt_provider: settings.wombat_stt_provider ?? DEFAULTS.wombat_stt_provider,
    wombat_tts_provider: settings.wombat_tts_provider ?? DEFAULTS.wombat_tts_provider,
    wombat_tts_voice_id: settings.wombat_tts_voice_id ?? DEFAULTS.wombat_tts_voice_id,
    wombat_stt_model: settings.wombat_stt_model ?? DEFAULTS.wombat_stt_model,
    wombat_asr_model: settings.wombat_asr_model ?? DEFAULTS.wombat_asr_model,
    wombat_reply_window_seconds:
      settings.wombat_reply_window_seconds ?? DEFAULTS.wombat_reply_window_seconds,
    wombat_spoken_reply_max_chars:
      settings.wombat_spoken_reply_max_chars ?? DEFAULTS.wombat_spoken_reply_max_chars,
    wombat_speak_full_replies:
      settings.wombat_speak_full_replies ?? DEFAULTS.wombat_speak_full_replies,
    wombat_persona_brevity: settings.wombat_persona_brevity ?? DEFAULTS.wombat_persona_brevity,
    wombat_persona_warmth: settings.wombat_persona_warmth ?? DEFAULTS.wombat_persona_warmth,
    wombat_persona_directness:
      settings.wombat_persona_directness ?? DEFAULTS.wombat_persona_directness,
    wombat_persona_humor: settings.wombat_persona_humor ?? DEFAULTS.wombat_persona_humor,
    wombat_persona_proactivity:
      settings.wombat_persona_proactivity ?? DEFAULTS.wombat_persona_proactivity,
    // TK-306: null -> "" (blank, placeholder-hinted) rather than a DEFAULTS fallback value -
    // the two HH:MM:SS time fields are truncated to the renderer's "HH:MM" shape.
    wombat_param_morning_brief_time: settings.wombat_param_morning_brief_time
      ? settings.wombat_param_morning_brief_time.slice(0, 5)
      : "",
    wombat_param_nightly_dream_time: settings.wombat_param_nightly_dream_time
      ? settings.wombat_param_nightly_dream_time.slice(0, 5)
      : "",
    wombat_param_urgency_threshold:
      settings.wombat_param_urgency_threshold != null
        ? String(settings.wombat_param_urgency_threshold)
        : "",
    wombat_param_per_class_daily_ceiling:
      settings.wombat_param_per_class_daily_ceiling != null
        ? String(settings.wombat_param_per_class_daily_ceiling)
        : "",
    wombat_param_decay_ttl_hours:
      settings.wombat_param_decay_ttl_seconds != null
        ? String(settings.wombat_param_decay_ttl_seconds / 3600)
        : "",
    wombat_quiet_start: settings.wombat_quiet_start ?? "",
    wombat_quiet_end: settings.wombat_quiet_end ?? "",
    wombat_param_mouth_model_timeout_seconds:
      settings.wombat_param_mouth_model_timeout_seconds != null
        ? String(settings.wombat_param_mouth_model_timeout_seconds)
        : "",
    wombat_param_mouth_daily_token_ceiling:
      settings.wombat_param_mouth_daily_token_ceiling != null
        ? String(settings.wombat_param_mouth_daily_token_ceiling)
        : "",
    wombat_param_mouth_max_usd_per_drive:
      settings.wombat_param_mouth_max_usd_per_drive != null
        ? String(settings.wombat_param_mouth_max_usd_per_drive)
        : "",
    wombat_observe_screen: settings.wombat_observe_screen ?? DEFAULTS.wombat_observe_screen,
    wombat_observe_webcam: settings.wombat_observe_webcam ?? DEFAULTS.wombat_observe_webcam,
    wombat_observe_mic: settings.wombat_observe_mic ?? DEFAULTS.wombat_observe_mic,
  };
}

/** "" -> "" (still unset, sent verbatim); else "HH:MM" -> "HH:MM:00" (the bridge's stored shape,
 * per the ticket's recorded time convention). */
function hhmmToBridgeTime(value: string): string {
  return value === "" ? "" : `${value}:00`;
}

/** Only the touched fields go in the PUT body - an untouched field is omitted, not re-sent. */
function buildPatch(formState: FormState, touched: ReadonlySet<FormField>): SettingsPatch {
  const patch: SettingsPatch = {};
  if (touched.has("wombat_assistant_name")) {
    patch.wombat_assistant_name = formState.wombat_assistant_name;
  }
  if (touched.has("wombat_user_name")) {
    patch.wombat_user_name = formState.wombat_user_name;
  }
  if (touched.has("wombat_stt_provider")) {
    patch.wombat_stt_provider = formState.wombat_stt_provider;
  }
  if (touched.has("wombat_tts_provider")) {
    patch.wombat_tts_provider = formState.wombat_tts_provider;
  }
  if (touched.has("wombat_tts_voice_id")) {
    patch.wombat_tts_voice_id = formState.wombat_tts_voice_id;
  }
  if (touched.has("wombat_stt_model")) {
    patch.wombat_stt_model = formState.wombat_stt_model;
  }
  if (touched.has("wombat_asr_model")) {
    patch.wombat_asr_model = formState.wombat_asr_model;
  }
  if (touched.has("wombat_reply_window_seconds")) {
    patch.wombat_reply_window_seconds = formState.wombat_reply_window_seconds;
  }
  if (touched.has("wombat_spoken_reply_max_chars")) {
    patch.wombat_spoken_reply_max_chars = formState.wombat_spoken_reply_max_chars;
  }
  if (touched.has("wombat_speak_full_replies")) {
    patch.wombat_speak_full_replies = formState.wombat_speak_full_replies;
  }
  if (touched.has("wombat_persona_brevity")) {
    patch.wombat_persona_brevity = formState.wombat_persona_brevity;
  }
  if (touched.has("wombat_persona_warmth")) {
    patch.wombat_persona_warmth = formState.wombat_persona_warmth;
  }
  if (touched.has("wombat_persona_directness")) {
    patch.wombat_persona_directness = formState.wombat_persona_directness;
  }
  if (touched.has("wombat_persona_humor")) {
    patch.wombat_persona_humor = formState.wombat_persona_humor;
  }
  if (touched.has("wombat_persona_proactivity")) {
    patch.wombat_persona_proactivity = formState.wombat_persona_proactivity;
  }
  if (touched.has("wombat_param_morning_brief_time")) {
    patch.wombat_param_morning_brief_time = hhmmToBridgeTime(
      formState.wombat_param_morning_brief_time,
    );
  }
  if (touched.has("wombat_param_nightly_dream_time")) {
    patch.wombat_param_nightly_dream_time = hhmmToBridgeTime(
      formState.wombat_param_nightly_dream_time,
    );
  }
  if (touched.has("wombat_param_urgency_threshold")) {
    patch.wombat_param_urgency_threshold = Number(formState.wombat_param_urgency_threshold);
  }
  if (touched.has("wombat_param_per_class_daily_ceiling")) {
    // TK-306 repair: an empty field means "cleared - back to default", NOT the numeric value 0
    // (which is itself a valid, distinct override meaning immediate voice off, ge=0 in the
    // backend's bounds). Number("") === 0 would silently save that override instead of clearing
    // it, so a blank field must PUT `null` (the backend already accepts `int | None`).
    patch.wombat_param_per_class_daily_ceiling =
      formState.wombat_param_per_class_daily_ceiling === ""
        ? null
        : Number(formState.wombat_param_per_class_daily_ceiling);
  }
  if (touched.has("wombat_param_decay_ttl_hours")) {
    patch.wombat_param_decay_ttl_seconds = Number(formState.wombat_param_decay_ttl_hours) * 3600;
  }
  if (touched.has("wombat_quiet_start")) {
    patch.wombat_quiet_start = formState.wombat_quiet_start;
  }
  if (touched.has("wombat_quiet_end")) {
    patch.wombat_quiet_end = formState.wombat_quiet_end;
  }
  if (touched.has("wombat_param_mouth_model_timeout_seconds")) {
    patch.wombat_param_mouth_model_timeout_seconds = Number(
      formState.wombat_param_mouth_model_timeout_seconds,
    );
  }
  if (touched.has("wombat_param_mouth_daily_token_ceiling")) {
    patch.wombat_param_mouth_daily_token_ceiling = Number(
      formState.wombat_param_mouth_daily_token_ceiling,
    );
  }
  if (touched.has("wombat_param_mouth_max_usd_per_drive")) {
    patch.wombat_param_mouth_max_usd_per_drive = Number(
      formState.wombat_param_mouth_max_usd_per_drive,
    );
  }
  if (touched.has("wombat_observe_screen")) {
    patch.wombat_observe_screen = formState.wombat_observe_screen;
  }
  if (touched.has("wombat_observe_webcam")) {
    patch.wombat_observe_webcam = formState.wombat_observe_webcam;
  }
  if (touched.has("wombat_observe_mic")) {
    patch.wombat_observe_mic = formState.wombat_observe_mic;
  }
  return patch;
}

export function App() {
  // TK-276 (DEC-58 a/b/e): mounted ONCE here so the persisted `wombat_ptt_binding` drives the
  // mic anywhere in the app while the window is focused - fully self-contained, see ptt.ts.
  const pushToTalk = usePushToTalk();
  const [view, setView] = useState<ViewId>("today");
  const [formState, setFormState] = useState<FormState | null>(null);
  const [touched, setTouched] = useState<ReadonlySet<FormField>>(new Set());
  const [keyInputs, setKeyInputs] = useState<Record<KeyProvider, string>>(EMPTY_KEY_INPUTS);
  const [keysConfigured, setKeysConfigured] =
    useState<Record<KeyProvider, boolean>>(EMPTY_KEYS_CONFIGURED);
  // TK-306 (RULING v2.172 r4): the read-only GET /settings timezone object - no form field,
  // no touched/patch involvement, refreshed on load and after every save exactly like keys.
  const [timezone, setTimezone] = useState<TimezoneInfo | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  // Split per DEC-37 - a save shows the restart notice, the persona
  // hot-apply hint, both, or neither, depending on what THAT save touched.
  const [notice, setNotice] = useState<{ restart: boolean; hotApply: boolean } | null>(null);

  useEffect(() => {
    let cancelled = false;
    getSettings()
      .then((response) => {
        if (cancelled) return;
        setFormState(toFormState(response.settings));
        setKeysConfigured({ ...EMPTY_KEYS_CONFIGURED, ...response.keys });
        setTimezone(response.timezone);
      })
      .catch((error: unknown) => {
        if (cancelled) return;
        setLoadError(error instanceof Error ? error.message : String(error));
      });
    return () => {
      cancelled = true;
    };
  }, []);

  function updateField<K extends FormField>(field: K, value: FormState[K]): void {
    setFormState((prev) => (prev ? { ...prev, [field]: value } : prev));
    setTouched((prev) => {
      const next = new Set(prev);
      next.add(field);
      return next;
    });
  }

  function updateKeyInput(provider: KeyProvider, value: string): void {
    setKeyInputs((prev) => ({ ...prev, [provider]: value }));
  }

  async function handleSave(): Promise<void> {
    if (!formState) return;
    setSaving(true);
    setSaveError(null);
    try {
      const patch = buildPatch(formState, touched);
      if (Object.keys(patch).length > 0) {
        await putSettings(patch);
      }
      const touchedKeyProviders = KEY_PROVIDERS.filter(
        (provider) => keyInputs[provider].trim() !== "",
      );
      for (const provider of touchedKeyProviders) {
        await putKey(provider, keyInputs[provider]);
      }

      const hasPersonaChange = PERSONA_FIELDS.some((field) => touched.has(field));
      const hasRestartChange =
        RESTART_FIELDS.some((field) => touched.has(field)) || touchedKeyProviders.length > 0;
      setNotice({ restart: hasRestartChange, hotApply: hasPersonaChange });

      const refreshed = await getSettings();
      setFormState(toFormState(refreshed.settings));
      setKeysConfigured({ ...EMPTY_KEYS_CONFIGURED, ...refreshed.keys });
      setTimezone(refreshed.timezone);
      setTouched(new Set());
      setKeyInputs(EMPTY_KEY_INPUTS);
    } catch (error) {
      setSaveError(error instanceof Error ? error.message : String(error));
    } finally {
      setSaving(false);
    }
  }

  const hasChanges =
    touched.size > 0 || KEY_PROVIDERS.some((provider) => keyInputs[provider].trim() !== "");

  // The Save bar is identical on every settings category page - one shared
  // save button over the one shared form/touched/keyInputs state above,
  // regardless of which category is on screen when it's clicked.
  function renderSaveBar() {
    return (
      <>
        {saveError && <p className={ink.primary}>Save failed: {saveError}</p>}

        {notice && (
          <Panel className="flex flex-col gap-1">
            {notice.hotApply && (
              <p className={ink.muted}>Persona changes apply on the next turn.</p>
            )}
            {notice.restart && (
              <p className={ink.muted}>Restart Wombat to apply these changes.</p>
            )}
          </Panel>
        )}

        <Button type="button" onClick={() => void handleSave()} disabled={!hasChanges || saving}>
          {saving ? "Saving..." : "Save"}
        </Button>
      </>
    );
  }

  const loadErrorBanner = loadError && (
    <Panel>
      <p className={ink.primary}>Failed to load settings: {loadError}</p>
    </Panel>
  );

  return (
    <div className={`${surface.canvas} ${font.sans} ${ink.primary} flex h-screen flex-col`}>
      {pushToTalk.active && (
        <div
          role="status"
          className={`fixed top-4 right-4 z-50 ${radius.md} ${interactive.danger.bg} ${interactive.danger.text} px-3 py-1.5 text-sm`}
        >
          Recording (push-to-talk)
        </div>
      )}
      {pushToTalk.degraded && (
        <div
          role="status"
          className={`fixed top-4 right-4 z-50 ${radius.md} ${surface.elevated} px-3 py-1.5 text-sm ${ink.muted}`}
        >
          voice drop-dir not configured - set WOMBAT_ASR_DROP_DIR
        </div>
      )}
      <Header />
      <div className="flex min-h-0 flex-1">
        <NavRail active={view} onSelect={setView} />

        <main className="min-w-0 flex-1 overflow-y-auto p-8">
          <div className="mx-auto flex max-w-2xl flex-col gap-4">
            {view === "today" && <Today />}

            {view === "persona" && (
              <>
                {loadErrorBanner}
                {formState && (
                  <>
                    <Panel className="flex flex-col gap-4">
                      <Field
                        id="assistant-name"
                        label="Assistant name"
                        value={formState.wombat_assistant_name}
                        onChange={(e) => updateField("wombat_assistant_name", e.target.value)}
                      />
                      <Field
                        id="user-name"
                        label="Your name"
                        value={formState.wombat_user_name}
                        onChange={(e) => updateField("wombat_user_name", e.target.value)}
                      />
                    </Panel>

                    <Panel className="flex flex-col gap-4">
                      <h2 className="text-sm font-semibold">Persona</h2>
                      <Select
                        id="persona-brevity"
                        label="Brevity"
                        options={BREVITY_OPTIONS}
                        value={formState.wombat_persona_brevity}
                        onChange={(e) =>
                          updateField("wombat_persona_brevity", e.target.value as Brevity)
                        }
                      />
                      <Select
                        id="persona-warmth"
                        label="Warmth"
                        options={WARMTH_OPTIONS}
                        value={formState.wombat_persona_warmth}
                        onChange={(e) =>
                          updateField("wombat_persona_warmth", e.target.value as Warmth)
                        }
                      />
                      <Select
                        id="persona-directness"
                        label="Directness"
                        options={DIRECTNESS_OPTIONS}
                        value={formState.wombat_persona_directness}
                        onChange={(e) =>
                          updateField("wombat_persona_directness", e.target.value as Directness)
                        }
                      />
                      <Select
                        id="persona-humor"
                        label="Humor"
                        options={HUMOR_OPTIONS}
                        value={formState.wombat_persona_humor}
                        onChange={(e) =>
                          updateField("wombat_persona_humor", e.target.value as Humor)
                        }
                      />
                      <Select
                        id="persona-proactivity"
                        label="Proactivity"
                        options={PROACTIVITY_OPTIONS}
                        value={formState.wombat_persona_proactivity}
                        onChange={(e) =>
                          updateField("wombat_persona_proactivity", e.target.value as Proactivity)
                        }
                      />
                    </Panel>

                    {renderSaveBar()}
                  </>
                )}
              </>
            )}

            {view === "voice" && (
              <>
                {loadErrorBanner}
                <AudioPanel />
                {formState && (
                  <>
                    <Panel className="flex flex-col gap-4">
                      <Select
                        id="stt-provider"
                        label="STT provider"
                        options={PROVIDER_OPTIONS}
                        value={formState.wombat_stt_provider}
                        onChange={(e) =>
                          updateField("wombat_stt_provider", e.target.value as Provider)
                        }
                      />
                      <Select
                        id="tts-provider"
                        label="TTS provider"
                        options={PROVIDER_OPTIONS}
                        value={formState.wombat_tts_provider}
                        onChange={(e) =>
                          updateField("wombat_tts_provider", e.target.value as Provider)
                        }
                      />
                      <Field
                        id="tts-voice-id"
                        label="TTS voice ID"
                        value={formState.wombat_tts_voice_id}
                        onChange={(e) => updateField("wombat_tts_voice_id", e.target.value)}
                      />
                      <Field
                        id="stt-model"
                        label="Cloud STT model"
                        value={formState.wombat_stt_model}
                        onChange={(e) => updateField("wombat_stt_model", e.target.value)}
                      />
                      <Select
                        id="asr-model"
                        label="Local ASR model"
                        options={ASR_MODEL_OPTIONS}
                        value={formState.wombat_asr_model}
                        onChange={(e) =>
                          updateField("wombat_asr_model", e.target.value as AsrModel)
                        }
                      />
                      <Field
                        id="reply-window-seconds"
                        label="Reply window (s)"
                        type="number"
                        value={formState.wombat_reply_window_seconds}
                        onChange={(e) =>
                          updateField("wombat_reply_window_seconds", Number(e.target.value))
                        }
                      />
                      <Field
                        id="spoken-reply-max-chars"
                        label="Spoken reply cap (chars)"
                        type="number"
                        value={formState.wombat_spoken_reply_max_chars}
                        onChange={(e) =>
                          updateField("wombat_spoken_reply_max_chars", Number(e.target.value))
                        }
                      />
                      <Select
                        id="speak-full-replies"
                        label="Speak full replies"
                        options={ON_OFF_OPTIONS}
                        value={formState.wombat_speak_full_replies ? "on" : "off"}
                        onChange={(e) =>
                          updateField("wombat_speak_full_replies", e.target.value === "on")
                        }
                      />
                    </Panel>

                    {renderSaveBar()}
                  </>
                )}
              </>
            )}

            {view === "keys" && (
              <>
                {loadErrorBanner}
                {formState && (
                  <>
                    <Panel className="flex flex-col gap-4">
                      <h2 className="text-sm font-semibold">Cloud voice-provider keys</h2>
                      {/* Write-only: the key input NEVER carries a stored value - the
                          configured indicator is driven solely by the GET /settings
                          `keys` booleans, never by what's typed here. */}
                      {KEY_PROVIDERS.map((provider) => (
                        <div key={provider} className="flex flex-col gap-1">
                          <Field
                            id={`key-${provider}`}
                            label={KEY_PROVIDER_LABELS[provider]}
                            type="password"
                            autoComplete="off"
                            value={keyInputs[provider]}
                            onChange={(e) => updateKeyInput(provider, e.target.value)}
                          />
                          <Indicator configured={keysConfigured[provider]} />
                        </div>
                      ))}
                    </Panel>

                    <GoogleConnections />

                    {renderSaveBar()}
                  </>
                )}
              </>
            )}

            {view === "system" && (
              <>
                <RuntimeControls />
                {formState && (
                  <>
                    <Panel className="flex flex-col gap-4">
                      <h2 className="text-sm font-semibold">Briefs & interruptions</h2>
                      <Field
                        id="brief-time"
                        label="Brief time"
                        type="time"
                        placeholder={`default ${PARAM_PLACEHOLDERS.wombat_param_morning_brief_time}`}
                        value={formState.wombat_param_morning_brief_time}
                        onChange={(e) =>
                          updateField("wombat_param_morning_brief_time", e.target.value)
                        }
                      />
                      <Field
                        id="reflection-time"
                        label="Reflection time"
                        type="time"
                        placeholder={`default ${PARAM_PLACEHOLDERS.wombat_param_nightly_dream_time}`}
                        value={formState.wombat_param_nightly_dream_time}
                        onChange={(e) =>
                          updateField("wombat_param_nightly_dream_time", e.target.value)
                        }
                      />
                      <Field
                        id="urgency-threshold"
                        label="Urgency threshold"
                        type="number"
                        min={0.6}
                        max={0.95}
                        step={0.01}
                        placeholder={`default ${PARAM_PLACEHOLDERS.wombat_param_urgency_threshold}`}
                        value={formState.wombat_param_urgency_threshold}
                        onChange={(e) =>
                          updateField("wombat_param_urgency_threshold", e.target.value)
                        }
                      />
                      <Field
                        id="per-class-daily-ceiling"
                        label="Max voice interruptions per sender class per day"
                        type="number"
                        min={0}
                        max={10}
                        step={1}
                        placeholder={`default ${PARAM_PLACEHOLDERS.wombat_param_per_class_daily_ceiling}`}
                        value={formState.wombat_param_per_class_daily_ceiling}
                        onChange={(e) =>
                          updateField("wombat_param_per_class_daily_ceiling", e.target.value)
                        }
                      />
                      <Field
                        id="item-decay-hours"
                        label="Item decay (hours)"
                        type="number"
                        min={1}
                        max={168}
                        step={1}
                        placeholder={`default ${PARAM_PLACEHOLDERS.wombat_param_decay_ttl_hours}`}
                        value={formState.wombat_param_decay_ttl_hours}
                        onChange={(e) =>
                          updateField("wombat_param_decay_ttl_hours", e.target.value)
                        }
                      />
                      <Field
                        id="quiet-start"
                        label="Quiet hours start"
                        type="time"
                        value={formState.wombat_quiet_start}
                        onChange={(e) => updateField("wombat_quiet_start", e.target.value)}
                      />
                      <Field
                        id="quiet-end"
                        label="Quiet hours end"
                        type="time"
                        value={formState.wombat_quiet_end}
                        onChange={(e) => updateField("wombat_quiet_end", e.target.value)}
                      />
                      <p className={ink.muted}>
                        Set both start and end to enable quiet hours, or leave both blank to
                        disable.
                      </p>
                    </Panel>

                    <Panel className="flex flex-col gap-4">
                      <h2 className="text-sm font-semibold">Limits</h2>
                      <Field
                        id="mouth-model-timeout-seconds"
                        label="Model response wait (s)"
                        type="number"
                        min={2}
                        max={60}
                        placeholder={`default ${PARAM_PLACEHOLDERS.wombat_param_mouth_model_timeout_seconds}`}
                        value={formState.wombat_param_mouth_model_timeout_seconds}
                        onChange={(e) =>
                          updateField("wombat_param_mouth_model_timeout_seconds", e.target.value)
                        }
                      />
                      <Field
                        id="mouth-daily-token-ceiling"
                        label="Daily token ceiling"
                        type="number"
                        min={10000}
                        max={1000000}
                        placeholder={`default ${PARAM_PLACEHOLDERS.wombat_param_mouth_daily_token_ceiling}`}
                        value={formState.wombat_param_mouth_daily_token_ceiling}
                        onChange={(e) =>
                          updateField("wombat_param_mouth_daily_token_ceiling", e.target.value)
                        }
                      />
                      <Field
                        id="mouth-max-usd-per-drive"
                        label="Per-conversation spend cap (USD)"
                        type="number"
                        min={0.05}
                        max={5.0}
                        step={0.01}
                        placeholder={`default ${PARAM_PLACEHOLDERS.wombat_param_mouth_max_usd_per_drive}`}
                        value={formState.wombat_param_mouth_max_usd_per_drive}
                        onChange={(e) =>
                          updateField("wombat_param_mouth_max_usd_per_drive", e.target.value)
                        }
                      />
                    </Panel>

                    <Panel className="flex flex-col gap-4">
                      <h2 className="text-sm font-semibold">Observation</h2>
                      <Select
                        id="observe-screen"
                        label="Screen"
                        options={ON_OFF_OPTIONS}
                        value={formState.wombat_observe_screen ? "on" : "off"}
                        onChange={(e) =>
                          updateField("wombat_observe_screen", e.target.value === "on")
                        }
                      />
                      <Select
                        id="observe-webcam"
                        label="Webcam"
                        options={ON_OFF_OPTIONS}
                        value={formState.wombat_observe_webcam ? "on" : "off"}
                        onChange={(e) =>
                          updateField("wombat_observe_webcam", e.target.value === "on")
                        }
                      />
                      <Select
                        id="observe-mic"
                        label="Microphone"
                        options={ON_OFF_OPTIONS}
                        value={formState.wombat_observe_mic ? "on" : "off"}
                        onChange={(e) =>
                          updateField("wombat_observe_mic", e.target.value === "on")
                        }
                      />
                    </Panel>

                    {timezone && (
                      <p className={ink.muted}>
                        Timezone: {timezone.name ?? "unresolved"} ({timezone.source})
                      </p>
                    )}

                    {renderSaveBar()}
                  </>
                )}
              </>
            )}
          </div>
        </main>

        <ChatDock />
      </div>
    </div>
  );
}
