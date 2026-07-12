import { useEffect, useState } from "react";

import {
  AudioPanel,
  Button,
  ChatDock,
  Field,
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
import { font, ink, surface } from "./tokens";
import {
  getSettings,
  putKey,
  putSettings,
  KEY_PROVIDERS,
  type Brevity,
  type Directness,
  type Humor,
  type KeyProvider,
  type Proactivity,
  type Provider,
  type SettingsFields,
  type SettingsPatch,
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
  wombat_stt_provider: Provider;
  wombat_tts_provider: Provider;
  wombat_tts_voice_id: string;
  wombat_persona_brevity: Brevity;
  wombat_persona_warmth: Warmth;
  wombat_persona_directness: Directness;
  wombat_persona_humor: Humor;
  wombat_persona_proactivity: Proactivity;
}

type FormField = keyof FormState;

// DEFAULT_MATRIX (src/wombat/persona/matrix.py) - the fallback shown when a
// field is `null` (never customized) in wombat.settings.json.
const DEFAULTS: FormState = {
  wombat_assistant_name: "",
  wombat_stt_provider: "local",
  wombat_tts_provider: "local",
  wombat_tts_voice_id: "",
  wombat_persona_brevity: "terse",
  wombat_persona_warmth: "reserved",
  wombat_persona_directness: "plain",
  wombat_persona_humor: "none",
  wombat_persona_proactivity: "balanced",
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
  "wombat_stt_provider",
  "wombat_tts_provider",
  "wombat_tts_voice_id",
];

// wombat.settings_app.api.SettingsUpdate's provider Literal, verbatim.
const PROVIDER_OPTIONS: SelectOption[] = [
  { value: "local", label: "Local" },
  { value: "deepgram", label: "Deepgram" },
  { value: "elevenlabs", label: "ElevenLabs" },
  { value: "fish", label: "Fish Audio" },
];

// src/wombat/persona/matrix.py's five closed axes, verbatim named levels.
const BREVITY_OPTIONS: SelectOption[] = [
  { value: "terse", label: "Terse" },
  { value: "balanced", label: "Balanced" },
  { value: "expansive", label: "Expansive" },
];
const WARMTH_OPTIONS: SelectOption[] = [
  { value: "reserved", label: "Reserved" },
  { value: "neutral", label: "Neutral" },
  { value: "warm", label: "Warm" },
];
const DIRECTNESS_OPTIONS: SelectOption[] = [
  { value: "gentle", label: "Gentle" },
  { value: "plain", label: "Plain" },
  { value: "blunt", label: "Blunt" },
];
const HUMOR_OPTIONS: SelectOption[] = [
  { value: "none", label: "None" },
  { value: "dry", label: "Dry" },
];
const PROACTIVITY_OPTIONS: SelectOption[] = [
  { value: "minimal", label: "Minimal" },
  { value: "balanced", label: "Balanced" },
  { value: "forward", label: "Forward" },
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
    wombat_stt_provider: settings.wombat_stt_provider ?? DEFAULTS.wombat_stt_provider,
    wombat_tts_provider: settings.wombat_tts_provider ?? DEFAULTS.wombat_tts_provider,
    wombat_tts_voice_id: settings.wombat_tts_voice_id ?? DEFAULTS.wombat_tts_voice_id,
    wombat_persona_brevity: settings.wombat_persona_brevity ?? DEFAULTS.wombat_persona_brevity,
    wombat_persona_warmth: settings.wombat_persona_warmth ?? DEFAULTS.wombat_persona_warmth,
    wombat_persona_directness:
      settings.wombat_persona_directness ?? DEFAULTS.wombat_persona_directness,
    wombat_persona_humor: settings.wombat_persona_humor ?? DEFAULTS.wombat_persona_humor,
    wombat_persona_proactivity:
      settings.wombat_persona_proactivity ?? DEFAULTS.wombat_persona_proactivity,
  };
}

/** Only the touched fields go in the PUT body - an untouched field is omitted, not re-sent. */
function buildPatch(formState: FormState, touched: ReadonlySet<FormField>): SettingsPatch {
  const patch: SettingsPatch = {};
  if (touched.has("wombat_assistant_name")) {
    patch.wombat_assistant_name = formState.wombat_assistant_name;
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
  return patch;
}

export function App() {
  const [view, setView] = useState<ViewId>("today");
  const [formState, setFormState] = useState<FormState | null>(null);
  const [touched, setTouched] = useState<ReadonlySet<FormField>>(new Set());
  const [keyInputs, setKeyInputs] = useState<Record<KeyProvider, string>>(EMPTY_KEY_INPUTS);
  const [keysConfigured, setKeysConfigured] =
    useState<Record<KeyProvider, boolean>>(EMPTY_KEYS_CONFIGURED);
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

                    {renderSaveBar()}
                  </>
                )}
              </>
            )}

            {view === "system" && <RuntimeControls />}
          </div>
        </main>

        <ChatDock />
      </div>
    </div>
  );
}
