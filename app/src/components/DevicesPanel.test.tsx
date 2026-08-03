// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { resetBridgeCacheForTests } from "../api";
import { DevicesPanel } from "./DevicesPanel";

/**
 * TK-342 AC1-AC4/AC6: the Devices panel against a fake bridge + fake fetch (no live settings
 * API, no live device credential store) - mirrors the `GoogleConnections.test.tsx` self-contained
 * pattern.
 */

const PORT = 41421;
const TOKEN = "test-token";

interface FetchCall {
  url: string;
  method: string;
  body: unknown;
}

interface DeviceRecord {
  device_id: string;
  name: string;
  paired_at: string;
}

function installFakeBridge(): void {
  (window as unknown as { wombatSettings: { getInfo: () => Promise<unknown> } }).wombatSettings = {
    getInfo: vi.fn().mockResolvedValue({ port: PORT, token: TOKEN }),
  };
}

/** A stateful fake over GET/POST/DELETE /devices + GET/PUT /settings - the two toggles + the
 * device list live in one in-memory store the mock fetch reads/writes, mirroring App.test.tsx's
 * `installFakeApi` shape. */
function installFetch(options?: {
  initialDevices?: DeviceRecord[];
  initialRemoteVoice?: boolean;
  initialObserveBiometrics?: boolean;
}): { calls: FetchCall[] } {
  const calls: FetchCall[] = [];
  const devices: DeviceRecord[] = [...(options?.initialDevices ?? [])];
  let remoteVoice = options?.initialRemoteVoice ?? false;
  let observeBiometrics = options?.initialObserveBiometrics ?? false;
  let mintCounter = 0;

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
        settings: {
          wombat_remote_voice: remoteVoice,
          wombat_observe_biometrics: observeBiometrics,
        },
        keys: { elevenlabs: false, deepgram: false, fish: false },
        timezone: { name: "America/Chicago", source: "system" },
      });
    }
    if (method === "PUT" && url.endsWith("/settings")) {
      const patch = body as Record<string, boolean>;
      if ("wombat_remote_voice" in patch) remoteVoice = patch.wombat_remote_voice;
      if ("wombat_observe_biometrics" in patch) observeBiometrics = patch.wombat_observe_biometrics;
      return Response.json({ settings: { wombat_remote_voice: remoteVoice, wombat_observe_biometrics: observeBiometrics } });
    }
    if (method === "GET" && url.endsWith("/devices")) {
      return Response.json({ devices: [...devices] });
    }
    if (method === "POST" && url.endsWith("/devices")) {
      mintCounter += 1;
      const record: DeviceRecord = {
        device_id: `device-${mintCounter}`,
        name: (body as { name: string }).name,
        paired_at: "2026-08-03T12:00:00+00:00",
      };
      devices.push(record);
      return Response.json(
        { ...record, token: `token-${mintCounter}`, host: "127.0.0.1", port: 8788 },
        { status: 201 },
      );
    }
    const deleteMatch = /\/devices\/([^/]+)$/.exec(url);
    if (method === "DELETE" && deleteMatch) {
      const deviceId = deleteMatch[1];
      const index = devices.findIndex((d) => d.device_id === deviceId);
      if (index >= 0) devices.splice(index, 1);
      return Response.json({ ok: true });
    }
    throw new Error(`unhandled fetch: ${method} ${url}`);
  });

  vi.stubGlobal("fetch", fetchMock);
  return { calls };
}

function stubClipboard(): { writeText: ReturnType<typeof vi.fn> } {
  const writeText = vi.fn().mockResolvedValue(undefined);
  Object.assign(navigator, { clipboard: { writeText } });
  return { writeText };
}

beforeEach(() => {
  resetBridgeCacheForTests();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("DevicesPanel (TK-342 AC1: nothing paired)", () => {
  it("renders both consent rows at Off, an empty device list, and a Pair-a-device control - no QR/token exist", async () => {
    installFakeBridge();
    installFetch();
    render(<DevicesPanel />);

    expect(await screen.findByText("No devices paired yet.")).toBeTruthy();
    expect((screen.getByLabelText("Remote voice") as HTMLSelectElement).value).toBe("off");
    expect((screen.getByLabelText("Biometrics") as HTMLSelectElement).value).toBe("off");
    expect(screen.getByRole("button", { name: /pair a device/i })).toBeTruthy();
    expect(screen.queryByAltText(/pairing qr code/i)).toBeNull();
    expect(screen.queryByLabelText(/token \(shown once\)/i)).toBeNull();
  });
});

describe("DevicesPanel (TK-342 AC2: mint shows the QR + token exactly once)", () => {
  it("shows a QR and the plaintext token once after pairing, then never again after a remount", async () => {
    installFakeBridge();
    const { calls } = installFetch();
    const { unmount } = render(<DevicesPanel />);

    await screen.findByText("No devices paired yet.");
    fireEvent.change(screen.getByLabelText("Device name"), { target: { value: "iphone" } });
    fireEvent.click(screen.getByRole("button", { name: /pair a device/i }));

    await waitFor(() => {
      expect(calls.some((c) => c.method === "POST" && c.url.endsWith("/devices"))).toBe(true);
    });

    expect(await screen.findByAltText("Pairing QR code for iphone")).toBeTruthy();
    const tokenField = (await screen.findByLabelText(
      /token \(shown once\)/i,
    )) as HTMLInputElement;
    expect(tokenField.value).toBe("token-1");
    expect(screen.getByText("iphone")).toBeTruthy();
    expect(screen.getByText(/paired 2026-08-03/i)).toBeTruthy();

    // Re-mounting the panel (simulating a re-open) shows the device but never the token again.
    unmount();
    render(<DevicesPanel />);
    await screen.findByText("iphone");
    expect(screen.queryByLabelText(/token \(shown once\)/i)).toBeNull();
    expect(screen.queryByAltText(/pairing qr code/i)).toBeNull();
  });

  it("copies the token via the copy affordance", async () => {
    installFakeBridge();
    installFetch();
    const { writeText } = stubClipboard();
    render(<DevicesPanel />);

    await screen.findByText("No devices paired yet.");
    fireEvent.change(screen.getByLabelText("Device name"), { target: { value: "iphone" } });
    fireEvent.click(screen.getByRole("button", { name: /pair a device/i }));

    await screen.findByLabelText(/token \(shown once\)/i);
    fireEvent.click(screen.getByRole("button", { name: /copy token/i }));

    await waitFor(() => expect(writeText).toHaveBeenCalledWith("token-1"));
    expect(await screen.findByRole("button", { name: /^copied$/i })).toBeTruthy();
  });
});

describe("DevicesPanel (TK-342 AC3: revoke)", () => {
  it("revoking one of two devices removes only that one and states re-pairing is required", async () => {
    installFakeBridge();
    installFetch({
      initialDevices: [
        { device_id: "d-1", name: "iphone", paired_at: "2026-08-01T00:00:00+00:00" },
        { device_id: "d-2", name: "watch", paired_at: "2026-08-02T00:00:00+00:00" },
      ],
    });
    render(<DevicesPanel />);

    await screen.findByText("iphone");
    await screen.findByText("watch");

    const revokeButtons = screen.getAllByRole("button", { name: /^revoke$/i });
    // Revoke the row for "watch" (the second one, by DOM order).
    fireEvent.click(revokeButtons[1]);

    await waitFor(() => {
      expect(screen.queryByText("watch")).toBeNull();
    });
    expect(screen.getByText("iphone")).toBeTruthy();
    expect(await screen.findByText(/must be re-paired to reconnect/i)).toBeTruthy();
  });
});

describe("DevicesPanel (TK-342 AC4: independent consent toggles + restart notice)", () => {
  it("switching Remote voice On never touches Biometrics, and shows the restart notice", async () => {
    installFakeBridge();
    const { calls } = installFetch();
    render(<DevicesPanel />);

    await screen.findByText("No devices paired yet.");
    expect(screen.queryByText("Restart Wombat to apply these changes.")).toBeNull();

    fireEvent.change(screen.getByLabelText("Remote voice"), { target: { value: "on" } });

    await waitFor(() => {
      const puts = calls.filter((c) => c.method === "PUT" && c.url.endsWith("/settings"));
      expect(puts.length).toBe(1);
      expect(puts[0].body).toEqual({ wombat_remote_voice: true });
    });
    expect(await screen.findByText("Restart Wombat to apply these changes.")).toBeTruthy();
    expect((screen.getByLabelText("Biometrics") as HTMLSelectElement).value).toBe("off");
  });

  it("switching Biometrics On never touches Remote voice, and each is independently settable", async () => {
    installFakeBridge();
    const { calls } = installFetch();
    render(<DevicesPanel />);

    await screen.findByText("No devices paired yet.");
    fireEvent.change(screen.getByLabelText("Biometrics"), { target: { value: "on" } });

    await waitFor(() => {
      const puts = calls.filter((c) => c.method === "PUT" && c.url.endsWith("/settings"));
      expect(puts.length).toBe(1);
      expect(puts[0].body).toEqual({ wombat_observe_biometrics: true });
    });
    expect((screen.getByLabelText("Remote voice") as HTMLSelectElement).value).toBe("off");
  });

  it("renders On when the stored values are true", async () => {
    installFakeBridge();
    installFetch({ initialRemoteVoice: true, initialObserveBiometrics: true });
    render(<DevicesPanel />);

    expect(await screen.findByLabelText("Remote voice")).toBeTruthy();
    expect((screen.getByLabelText("Remote voice") as HTMLSelectElement).value).toBe("on");
    expect((screen.getByLabelText("Biometrics") as HTMLSelectElement).value).toBe("on");
  });
});

describe("DevicesPanel (device-count callback)", () => {
  it("reports the count on load, after mint, and after revoke", async () => {
    installFakeBridge();
    installFetch({
      initialDevices: [{ device_id: "d-1", name: "iphone", paired_at: "2026-08-01T00:00:00+00:00" }],
    });
    const onDeviceCountChange = vi.fn();
    render(<DevicesPanel onDeviceCountChange={onDeviceCountChange} />);

    await waitFor(() => expect(onDeviceCountChange).toHaveBeenCalledWith(1));

    fireEvent.change(screen.getByLabelText("Device name"), { target: { value: "watch" } });
    fireEvent.click(screen.getByRole("button", { name: /pair a device/i }));
    await waitFor(() => expect(onDeviceCountChange).toHaveBeenCalledWith(2));

    const revokeButtons = await screen.findAllByRole("button", { name: /^revoke$/i });
    fireEvent.click(revokeButtons[0]);
    await waitFor(() => expect(onDeviceCountChange).toHaveBeenCalledWith(1));
  });
});
