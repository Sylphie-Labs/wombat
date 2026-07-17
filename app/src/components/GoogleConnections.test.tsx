// @vitest-environment jsdom
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { resetBridgeCacheForTests } from "../api";
import { GoogleConnections } from "./GoogleConnections";

/**
 * TK-257 AC1/AC2/AC3: `GET /google/status` + `POST /google/{service}/connect`
 * client + the API Keys view's Google-connection rows, against a fake bridge
 * + fake fetch (no live Google, no live API - DEC-50).
 */

const PORT = 41420;
const TOKEN = "test-token";

function installFakeBridge(): void {
  (window as unknown as { wombatSettings: { getInfo: () => Promise<unknown> } }).wombatSettings = {
    getInfo: vi.fn().mockResolvedValue({ port: PORT, token: TOKEN }),
  };
}

type StatusBody = Record<string, { status: string; consent: string; error?: string }>;

/** A queue of `GET /google/status` bodies, served in order (last one repeats). */
function installFetch(statusQueue: StatusBody[], postStatus = 202): { postCalls: number } {
  const calls = { postCalls: 0 };
  let statusCallIndex = 0;
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const headers = new Headers(init?.headers);
    if (headers.get("X-Wombat-Token") !== TOKEN) {
      return new Response(null, { status: 401 });
    }
    const url = String(input);
    if (init?.method === "POST") {
      calls.postCalls += 1;
      expect(url).toContain("/connect");
      return new Response(null, { status: postStatus });
    }
    expect(url).toContain("/google/status");
    const body = statusQueue[Math.min(statusCallIndex, statusQueue.length - 1)];
    statusCallIndex += 1;
    return Response.json(body);
  });
  vi.stubGlobal("fetch", fetchMock);
  return calls;
}

beforeEach(() => {
  resetBridgeCacheForTests();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const BOTH_NOT_CONNECTED: StatusBody = {
  gcal: { status: "not_connected", consent: "idle" },
  gmail: { status: "not_connected", consent: "idle" },
};

describe("GoogleConnections (TK-257 AC1: the four honest statuses)", () => {
  it("renders Connect + Not connected chip for not_connected", async () => {
    installFakeBridge();
    installFetch([BOTH_NOT_CONNECTED]);
    render(<GoogleConnections />);

    expect((await screen.findAllByText("Not connected")).length).toBe(2);
    const connectButtons = await screen.findAllByRole("button", { name: "Connect" });
    expect(connectButtons.length).toBe(2);
  });

  it("renders Reconnect + Expired chip for expired", async () => {
    installFakeBridge();
    installFetch([
      {
        gcal: { status: "expired", consent: "idle" },
        gmail: { status: "not_connected", consent: "idle" },
      },
    ]);
    render(<GoogleConnections />);

    expect(await screen.findByText("Expired")).toBeTruthy();
    expect(await screen.findByRole("button", { name: "Reconnect" })).toBeTruthy();
  });

  it("renders Reconnect + Connected chip for connected", async () => {
    installFakeBridge();
    installFetch([
      {
        gcal: { status: "connected", consent: "idle" },
        gmail: { status: "not_connected", consent: "idle" },
      },
    ]);
    render(<GoogleConnections />);

    expect(await screen.findByText("Connected")).toBeTruthy();
    expect(await screen.findByRole("button", { name: "Reconnect" })).toBeTruthy();
  });

  it("renders no actionable Connect button for not_configured", async () => {
    installFakeBridge();
    installFetch([
      {
        gcal: { status: "not_configured", consent: "idle" },
        gmail: { status: "not_connected", consent: "idle" },
      },
    ]);
    render(<GoogleConnections />);

    expect(await screen.findByText("Not configured")).toBeTruthy();
    // Only Gmail's row gets an actionable button - gcal's not_configured must not.
    const buttons = await screen.findAllByRole("button", { name: "Connect" });
    expect(buttons.length).toBe(1);
  });

  it("renders the degraded state when the status fetch throws", async () => {
    installFakeBridge();
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new Error("network down");
      }),
    );
    render(<GoogleConnections />);

    expect(await screen.findByText(/Unavailable/)).toBeTruthy();
  });
});

describe("GoogleConnections (TK-257 AC2: consent lifecycle + restart notice)", () => {
  it(
    "disables the button and shows the waiting line, then the restart notice on connected",
    async () => {
      installFakeBridge();
      const calls = installFetch([
        BOTH_NOT_CONNECTED,
        {
          gcal: { status: "not_connected", consent: "in_progress" },
          gmail: { status: "not_connected", consent: "idle" },
        },
        {
          gcal: { status: "connected", consent: "idle" },
          gmail: { status: "not_connected", consent: "idle" },
        },
      ]);
      render(<GoogleConnections />);

      const [gcalConnect] = await screen.findAllByRole("button", { name: "Connect" });
      gcalConnect.click();

      await waitFor(() => expect(calls.postCalls).toBe(1));
      expect(await screen.findByText(/Waiting for you to approve/)).toBeTruthy();
      await waitFor(() => expect(gcalConnect).toHaveProperty("disabled", true));

      expect(await screen.findByText(/Restart Wombat/, {}, { timeout: 6000 })).toBeTruthy();
      expect(await screen.findByText(/Settings > System/)).toBeTruthy();
    },
    10000,
  );

  it(
    "surfaces a consent error and re-enables the button",
    async () => {
      installFakeBridge();
      installFetch([
        BOTH_NOT_CONNECTED,
        {
          gcal: { status: "not_connected", consent: "in_progress" },
          gmail: { status: "not_connected", consent: "idle" },
        },
        {
          gcal: {
            status: "not_connected",
            consent: "error",
            error: "consent flow failed: denied",
          },
          gmail: { status: "not_connected", consent: "idle" },
        },
      ]);
      render(<GoogleConnections />);

      const [gcalConnect] = await screen.findAllByRole("button", { name: "Connect" });
      gcalConnect.click();

      expect(
        await screen.findByText("consent flow failed: denied", {}, { timeout: 6000 }),
      ).toBeTruthy();
      await waitFor(() => expect(gcalConnect).toHaveProperty("disabled", false));
      // No restart notice on an error outcome.
      expect(screen.queryByText(/Restart Wombat/)).toBeNull();
    },
    10000,
  );
});
