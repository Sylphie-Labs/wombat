// @vitest-environment jsdom
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { resetBridgeCacheForTests } from "../api";
import { Upcoming } from "./Upcoming";

/**
 * TK-250 AC1/AC2: `GET /external/calendar` client + iteration-4 event
 * cards, against a fake bridge + fake fetch (no live API). Times below are
 * fixed UTC instants deliberately chosen to be unambiguous across
 * reasonable local timezones for the "same local day" bucketing assertions.
 */

const PORT = 41418;
const TOKEN = "test-token";

function installFakeBridge(): void {
  (window as unknown as { wombatSettings: { getInfo: () => Promise<unknown> } }).wombatSettings = {
    getInfo: vi.fn().mockResolvedValue({ port: PORT, token: TOKEN }),
  };
}

function installFetch(body: unknown, status = 200): void {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const headers = new Headers(init?.headers);
    if (headers.get("X-Wombat-Token") !== TOKEN) {
      return new Response(null, { status: 401 });
    }
    expect(String(input)).toContain("/external/calendar");
    return Response.json(body, { status });
  });
  vi.stubGlobal("fetch", fetchMock);
}

beforeEach(() => {
  resetBridgeCacheForTests();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("Upcoming (TK-250 AC2: loading/degraded/empty states)", () => {
  it("renders card-shaped skeleton frames while loading", () => {
    installFakeBridge();
    let resolveFetch: (value: Response) => void = () => {};
    vi.stubGlobal(
      "fetch",
      vi.fn(
        () =>
          new Promise<Response>((resolve) => {
            resolveFetch = resolve;
          }),
      ),
    );
    render(<Upcoming />);

    expect(screen.getAllByText("Loading events…").length).toBeGreaterThanOrEqual(1);
    resolveFetch(Response.json({ items: [], storage_unavailable: false }));
  });

  it("renders the degraded body inside the card shell when storage_unavailable is true", async () => {
    installFakeBridge();
    installFetch({ items: [], storage_unavailable: true });
    render(<Upcoming />);

    expect(await screen.findByText(/Unavailable/)).toBeTruthy();
  });

  it("renders the designed empty state on zero items - never blank", async () => {
    installFakeBridge();
    installFetch({ items: [], storage_unavailable: false });
    render(<Upcoming />);

    expect(await screen.findByText("No meetings in the next 7 days.")).toBeTruthy();
  });
});

describe("Upcoming (TK-250 AC1: event cards from the five payload fields only)", () => {
  it("renders a card per item, honest to event_id/title/start/end/all_day only", async () => {
    installFakeBridge();
    const now = new Date();
    const todayAt = (h: number, m: number) => {
      const d = new Date(now.getFullYear(), now.getMonth(), now.getDate(), h, m);
      return d.toISOString();
    };
    installFetch({
      items: [
        {
          event_id: "evt-1",
          title: "Call with Mom",
          start: todayAt(10, 0),
          end: todayAt(10, 45),
          all_day: false,
        },
        {
          event_id: "evt-2",
          title: "Pick up bike from shop",
          start: todayAt(15, 0),
          end: todayAt(16, 0),
          all_day: false,
        },
      ],
      storage_unavailable: false,
    });
    render(<Upcoming />);

    expect(await screen.findByText("Call with Mom")).toBeTruthy();
    expect(screen.getByText("Pick up bike from shop")).toBeTruthy();
    // Earliest today event is emphasized as "next".
    expect(screen.getByText("next")).toBeTruthy();
    expect(screen.getByText("Today")).toBeTruthy();
  });

  it("renders an All-day pill (no time block) for all_day events", async () => {
    installFakeBridge();
    const now = new Date();
    const start = new Date(now.getFullYear(), now.getMonth(), now.getDate(), 0, 0).toISOString();
    installFetch({
      items: [
        { event_id: "evt-3", title: "Laundry + meal prep", start, end: start, all_day: true },
      ],
      storage_unavailable: false,
    });
    render(<Upcoming />);

    await waitFor(() => expect(screen.getByText("Laundry + meal prep")).toBeTruthy());
    expect(screen.getByText("All-day")).toBeTruthy();
  });
});

