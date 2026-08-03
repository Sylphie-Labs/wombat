import { useEffect, useState } from "react";
import QRCode from "qrcode";

import {
  getDevices,
  getSettings,
  mintDevice,
  putSettings,
  revokeDevice,
  type PairedDevice,
} from "../api";
import { ink } from "../tokens";
import { Button } from "./Button";
import { cn } from "./cn";
import { Field } from "./Field";
import { Panel } from "./Panel";
import { Select, type SelectOption } from "./Select";

/**
 * TK-342 (EP-39, DEC-78(d)): the Devices panel - the human end of companion-device pairing. Two
 * SEPARATE consent rows (never one, DEC-68(b)'s per-channel thesis), the paired-device list with
 * per-device revoke, and a Pair-a-device control that mints a device and renders its QR (the
 * `wire-contract.md` §8 payload, encoded fully offline via the `qrcode` package - R4, no hosted
 * QR service) plus the plaintext token EXACTLY ONCE. Self-contained (its own `GET /devices`/
 * `GET /settings` load, its own `PUT /settings` per toggle) - not part of the App.tsx shared
 * form, the `GoogleConnections`/`RuntimeControls` precedent, so this panel is fully testable in
 * isolation.
 */

export interface DevicesPanelProps {
  /** Fires after the initial load and after every mint/revoke, with the CURRENT device count -
   * lets a parent (the DangerZone wipe-dialog honesty line, AC5) stay live within the session
   * without a second independent device fetch. */
  onDeviceCountChange?: (count: number) => void;
}

const ON_OFF_OPTIONS: SelectOption[] = [
  { value: "off", label: "Off" },
  { value: "on", label: "On" },
];

type DevicesLoadState = "loading" | "loaded" | "error";

interface MintedDisplay {
  device_id: string;
  name: string;
  token: string;
  qrDataUrl: string;
}

export function DevicesPanel({ onDeviceCountChange }: DevicesPanelProps) {
  const [devices, setDevices] = useState<PairedDevice[]>([]);
  const [loadState, setLoadState] = useState<DevicesLoadState>("loading");
  const [remoteVoice, setRemoteVoice] = useState(false);
  const [observeBiometrics, setObserveBiometrics] = useState(false);
  const [restartNotice, setRestartNotice] = useState(false);
  const [pairName, setPairName] = useState("");
  const [pairing, setPairing] = useState(false);
  const [pairError, setPairError] = useState<string | null>(null);
  const [minted, setMinted] = useState<MintedDisplay | null>(null);
  const [copyDone, setCopyDone] = useState(false);
  const [revokedNotice, setRevokedNotice] = useState(false);

  useEffect(() => {
    let cancelled = false;
    Promise.all([getDevices(), getSettings()])
      .then(([devicesResponse, settingsResponse]) => {
        if (cancelled) return;
        setDevices(devicesResponse.devices);
        onDeviceCountChange?.(devicesResponse.devices.length);
        setRemoteVoice(settingsResponse.settings.wombat_remote_voice ?? false);
        setObserveBiometrics(settingsResponse.settings.wombat_observe_biometrics ?? false);
        setLoadState("loaded");
      })
      .catch(() => {
        if (cancelled) return;
        setLoadState("error");
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- fires once per mount by design.
  }, []);

  async function handleToggle(
    field: "wombat_remote_voice" | "wombat_observe_biometrics",
    value: boolean,
  ): Promise<void> {
    if (field === "wombat_remote_voice") {
      setRemoteVoice(value);
      await putSettings({ wombat_remote_voice: value });
    } else {
      setObserveBiometrics(value);
      await putSettings({ wombat_observe_biometrics: value });
    }
    setRestartNotice(true);
  }

  async function handlePair(): Promise<void> {
    const name = pairName.trim();
    if (!name || pairing) return;
    setPairing(true);
    setPairError(null);
    try {
      const result = await mintDevice(name);
      // wire-contract.md §8, verbatim key order - single-line UTF-8 JSON, no whitespace.
      const payload = JSON.stringify({
        v: 1,
        host: result.host,
        port: result.port,
        token: result.token,
        name: result.name,
      });
      const qrDataUrl = await QRCode.toDataURL(payload);
      setMinted({
        device_id: result.device_id,
        name: result.name,
        token: result.token,
        qrDataUrl,
      });
      setCopyDone(false);
      setDevices((prev) => {
        const next = [
          ...prev,
          { device_id: result.device_id, name: result.name, paired_at: result.paired_at },
        ];
        onDeviceCountChange?.(next.length);
        return next;
      });
      setPairName("");
    } catch (error) {
      setPairError(error instanceof Error ? error.message : String(error));
    } finally {
      setPairing(false);
    }
  }

  async function handleRevoke(deviceId: string): Promise<void> {
    await revokeDevice(deviceId);
    setDevices((prev) => {
      const next = prev.filter((device) => device.device_id !== deviceId);
      onDeviceCountChange?.(next.length);
      return next;
    });
    setRevokedNotice(true);
    setMinted((prev) => (prev?.device_id === deviceId ? null : prev));
  }

  async function handleCopyToken(): Promise<void> {
    if (!minted) return;
    await navigator.clipboard.writeText(minted.token);
    setCopyDone(true);
  }

  return (
    <Panel className="flex flex-col gap-4">
      <h2 className="text-sm font-semibold">Devices</h2>

      <Select
        id="device-consent-remote-voice"
        label="Remote voice"
        options={ON_OFF_OPTIONS}
        value={remoteVoice ? "on" : "off"}
        onChange={(e) => void handleToggle("wombat_remote_voice", e.target.value === "on")}
      />
      <Select
        id="device-consent-observe-biometrics"
        label="Biometrics"
        options={ON_OFF_OPTIONS}
        value={observeBiometrics ? "on" : "off"}
        onChange={(e) => void handleToggle("wombat_observe_biometrics", e.target.value === "on")}
      />
      {restartNotice && <p className={ink.muted}>Restart Wombat to apply these changes.</p>}

      <div className="flex flex-col gap-2">
        <h3 className="text-sm font-medium">Paired devices</h3>
        {loadState === "error" && (
          <p className={ink.muted}>Unavailable — couldn't load paired devices.</p>
        )}
        {loadState === "loaded" && devices.length === 0 && (
          <p className={ink.muted}>No devices paired yet.</p>
        )}
        {devices.map((device) => (
          <div key={device.device_id} className="flex items-center justify-between gap-3">
            <div className="flex flex-col">
              <span className="text-sm font-medium">{device.name}</span>
              <span className={cn("text-sm", ink.muted)}>Paired {device.paired_at}</span>
            </div>
            <Button
              type="button"
              variant="secondary"
              onClick={() => void handleRevoke(device.device_id)}
            >
              Revoke
            </Button>
          </div>
        ))}
        {revokedNotice && (
          <p className={ink.muted}>The revoked device must be re-paired to reconnect.</p>
        )}
      </div>

      <div className="flex flex-col gap-2">
        <Field
          id="pair-device-name"
          label="Device name"
          value={pairName}
          onChange={(e) => setPairName(e.target.value)}
        />
        <Button
          type="button"
          onClick={() => void handlePair()}
          disabled={!pairName.trim() || pairing}
        >
          {pairing ? "Pairing..." : "Pair a device"}
        </Button>
        {pairError && (
          <p role="alert" className={ink.primary}>
            {pairError}
          </p>
        )}
      </div>

      {minted && (
        <div className="flex flex-col gap-2">
          <p className={ink.primary}>
            Scan this on {minted.name} now — the token will not be shown again.
          </p>
          <img
            src={minted.qrDataUrl}
            alt={`Pairing QR code for ${minted.name}`}
            width={200}
            height={200}
          />
          <Field id="minted-device-token" label="Token (shown once)" value={minted.token} readOnly />
          <Button type="button" variant="secondary" onClick={() => void handleCopyToken()}>
            {copyDone ? "Copied" : "Copy token"}
          </Button>
        </div>
      )}
    </Panel>
  );
}
