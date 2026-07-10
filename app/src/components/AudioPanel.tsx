import { useEffect, useRef, useState } from "react";

import { getSettings, putSettings } from "../api";
import { listInputDevices, MicCapture, type AudioCaptureDevice } from "../audio";
import { ink } from "../tokens";
import { Button } from "./Button";
import { Panel } from "./Panel";
import { Select, type SelectOption } from "./Select";

/**
 * TK-224 (Q-111(b) ruled shape): mic capture into the EXISTING ASR drop-dir
 * (ASRSource, watched non-recursively for .wav/.m4a/.mp3/.flac - zero new
 * Python ingest code) plus the voice on/off toggle
 * (`wombat_voice_enabled`, a bootstrap-read app-editable field - DEC-32
 * restart notice, the TK-200 notice-split pattern). Self-contained, like
 * ChatPane - it round-trips its own field through the settings API rather
 * than joining App.tsx's big form.
 *
 * NO-PLACEBO discipline: capture controls stay enabled until the main
 * process's hand-off (`window.wombatAudio.saveCapture`) actually reports
 * `drop-dir-not-configured` - at that point they DISABLE WITH AN
 * EXPLANATION rather than staying offered as if they work. Mute disables
 * the underlying track AND suppresses delivery - while muted, nothing
 * reaches `saveCapture` (`audio.ts`'s `MicCapture`). NO output-volume
 * control anywhere (DEC-39 - winsound has no gain seam).
 */

function deviceOptions(devices: readonly AudioCaptureDevice[]): SelectOption[] {
  return devices.map((device) => ({ value: device.deviceId, label: device.label }));
}

export function AudioPanel() {
  const [voiceEnabled, setVoiceEnabled] = useState(false);
  const [voiceEnabledLoaded, setVoiceEnabledLoaded] = useState(false);
  const [savingVoice, setSavingVoice] = useState(false);
  const [restartNotice, setRestartNotice] = useState(false);

  const [devices, setDevices] = useState<AudioCaptureDevice[]>([]);
  const [selectedDeviceId, setSelectedDeviceId] = useState("");
  const [muted, setMuted] = useState(false);
  const [recording, setRecording] = useState(false);
  const [dropDirConfigured, setDropDirConfigured] = useState(true);
  const [captureError, setCaptureError] = useState<string | null>(null);

  const captureRef = useRef<MicCapture | null>(null);

  useEffect(() => {
    let cancelled = false;
    getSettings()
      .then((response) => {
        if (cancelled) return;
        setVoiceEnabled(Boolean(response.settings.wombat_voice_enabled));
      })
      .catch(() => {
        /* handled below via the finally-equivalent state flip */
      })
      .finally(() => {
        if (!cancelled) setVoiceEnabledLoaded(true);
      });
    listInputDevices()
      .then((list) => {
        if (!cancelled) setDevices(list);
      })
      .catch(() => {
        /* no input devices available (e.g. no mic permission yet) - the
           Select just stays empty; recording will surface its own error. */
      });
    return () => {
      cancelled = true;
    };
  }, []);

  async function handleToggleVoice(): Promise<void> {
    const next = !voiceEnabled;
    setSavingVoice(true);
    try {
      await putSettings({ wombat_voice_enabled: next });
      setVoiceEnabled(next);
      setRestartNotice(true);
    } finally {
      setSavingVoice(false);
    }
  }

  async function handleRecordClick(): Promise<void> {
    if (recording) {
      const capture = captureRef.current;
      captureRef.current = null;
      setRecording(false);
      if (!capture) return;
      const outcome = await capture.stop();
      if (outcome.kind === "saved" && !outcome.result.ok) {
        if (outcome.result.reason === "drop-dir-not-configured") {
          setDropDirConfigured(false);
        } else {
          setCaptureError("Recording failed to save.");
        }
      }
      return;
    }

    setCaptureError(null);
    const capture = new MicCapture();
    try {
      await capture.start(selectedDeviceId || undefined, muted);
    } catch {
      setCaptureError("Could not access the microphone.");
      return;
    }
    captureRef.current = capture;
    setRecording(true);
  }

  function handleMuteClick(): void {
    const next = !muted;
    setMuted(next);
    captureRef.current?.setMuted(next);
  }

  const controlsDisabled = !dropDirConfigured;

  return (
    <Panel className="flex flex-col gap-4">
      <h2 className="text-sm font-semibold">Voice</h2>

      <div className="flex items-center gap-2">
        <Button
          type="button"
          variant="secondary"
          onClick={() => void handleToggleVoice()}
          disabled={savingVoice || !voiceEnabledLoaded}
        >
          {voiceEnabled ? "Voice on" : "Voice off"}
        </Button>
      </div>
      {restartNotice && <p className={ink.muted}>Restart Wombat to apply this change.</p>}

      {!dropDirConfigured && (
        <p className={ink.muted}>voice drop-dir not configured - set WOMBAT_ASR_DROP_DIR</p>
      )}

      <Select
        id="audio-input-device"
        label="Input device"
        options={deviceOptions(devices)}
        value={selectedDeviceId}
        disabled={controlsDisabled}
        onChange={(e) => setSelectedDeviceId(e.target.value)}
      />

      <div className="flex items-center gap-2">
        <Button type="button" onClick={() => void handleRecordClick()} disabled={controlsDisabled}>
          {recording ? "Stop" : "Record"}
        </Button>
        <Button
          type="button"
          variant="secondary"
          onClick={handleMuteClick}
          disabled={controlsDisabled}
        >
          {muted ? "Unmute" : "Mute"}
        </Button>
      </div>

      {captureError && <p className={ink.muted}>{captureError}</p>}
    </Panel>
  );
}
