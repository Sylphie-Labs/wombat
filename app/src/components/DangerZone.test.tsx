// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { DangerZone } from "./DangerZone";

/**
 * TK-336 AC1/AC2/AC4: DangerZone against a fake window.wombatWipe bridge
 * (and, for AC4's restart-prompt assertion, a fake window.wombatRuntime -
 * RuntimeControls' own bridge, since a successful wipe renders the EXISTING
 * restart control rather than a second implementation).
 */

function stubWipeBridge(impl: () => Promise<unknown>): ReturnType<typeof vi.fn> {
  const wipe = vi.fn().mockImplementation(impl);
  (window as unknown as { wombatWipe: { wipe: typeof wipe } }).wombatWipe = { wipe };
  return wipe;
}

function stubRuntimeBridge(): void {
  (window as unknown as { wombatRuntime: { restart: () => Promise<unknown> } }).wombatRuntime = {
    restart: vi.fn().mockResolvedValue({ status: "restarted" }),
  };
}

function openModal(): void {
  fireEvent.click(screen.getByRole("button", { name: /wipe memory/i }));
}

function typeConfirm(value: string): void {
  fireEvent.change(screen.getByLabelText(/type wipe to confirm/i), { target: { value } });
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("DangerZone (TK-336 AC1: typed confirmation)", () => {
  it("clicking the wipe button opens a modal listing both archived-and-wiped and NOT-touched items", () => {
    render(<DangerZone />);
    openModal();

    expect(screen.getByRole("alertdialog")).toBeTruthy();
    expect(screen.getByText(/the queue and pending set/i)).toBeTruthy();
    expect(screen.getByText(/user facts/i)).toBeTruthy();
    expect(screen.getByText(/^settings$/i)).toBeTruthy();
    expect(screen.getByText(/api keys/i)).toBeTruthy();
    expect(screen.getByText(/the google connection/i)).toBeTruthy();
    expect(screen.getByText(/wombat's tables themselves/i)).toBeTruthy();
  });

  it("the destructive action is disabled until the input equals WIPE exactly (case-sensitive, trimmed)", () => {
    render(<DangerZone />);
    openModal();

    const confirmButton = screen.getByRole("button", { name: /confirm wipe/i });
    expect(confirmButton.hasAttribute("disabled")).toBe(true);

    typeConfirm("wipe");
    expect(confirmButton.hasAttribute("disabled")).toBe(true);

    typeConfirm("WIPE please");
    expect(confirmButton.hasAttribute("disabled")).toBe(true);

    typeConfirm("  WIPE  ");
    expect(confirmButton.hasAttribute("disabled")).toBe(false);

    typeConfirm("WIPE");
    expect(confirmButton.hasAttribute("disabled")).toBe(false);
  });

  it("Cancel closes the modal having made ZERO IPC calls", () => {
    const wipe = stubWipeBridge(() => Promise.resolve({ status: "wiped", archivePath: "x" }));
    render(<DangerZone />);
    openModal();
    typeConfirm("WIPE");

    fireEvent.click(screen.getByRole("button", { name: /^cancel$/i }));

    expect(screen.queryByRole("alertdialog")).toBeNull();
    expect(wipe).not.toHaveBeenCalled();
  });

  it("Escape closes the modal having made ZERO IPC calls", () => {
    const wipe = stubWipeBridge(() => Promise.resolve({ status: "wiped", archivePath: "x" }));
    render(<DangerZone />);
    openModal();
    typeConfirm("WIPE");

    fireEvent.keyDown(document, { key: "Escape" });

    expect(screen.queryByRole("alertdialog")).toBeNull();
    expect(wipe).not.toHaveBeenCalled();
  });

  it("backdrop dismissal closes the modal having made ZERO IPC calls", () => {
    const wipe = stubWipeBridge(() => Promise.resolve({ status: "wiped", archivePath: "x" }));
    const { container } = render(<DangerZone />);
    openModal();
    typeConfirm("WIPE");

    // The backdrop is the fixed overlay housing the dialog - click it
    // directly (not the dialog itself, which stops propagation).
    const backdrop = container.querySelector(".fixed.inset-0") as HTMLElement;
    fireEvent.click(backdrop);

    expect(screen.queryByRole("alertdialog")).toBeNull();
    expect(wipe).not.toHaveBeenCalled();
  });

  it("clicking inside the dialog itself does not close it", () => {
    render(<DangerZone />);
    openModal();

    fireEvent.click(screen.getByRole("alertdialog"));

    expect(screen.getByRole("alertdialog")).toBeTruthy();
  });
});

describe("DangerZone (TK-342 AC5: paired-device honesty line)", () => {
  it("names the paired devices' own on-device copies in NOT_TOUCHED when a device is paired", () => {
    render(<DangerZone devicesPaired />);
    openModal();

    expect(screen.getByText(/phone sync buffer/i)).toBeTruthy();
    expect(screen.getByText(/per-type anchors/i)).toBeTruthy();
    expect(screen.getByText(/untransferred watch audio/i)).toBeTruthy();
    // The existing NOT_TOUCHED items are still present, byte-unchanged.
    expect(screen.getByText(/^settings$/i)).toBeTruthy();
  });

  it("omits the paired-device line when no device is paired (the default)", () => {
    render(<DangerZone />);
    openModal();

    expect(screen.queryByText(/phone sync buffer/i)).toBeNull();
  });
});

describe("DangerZone (TK-336 AC2: single flight)", () => {
  it("confirming, then clicking again while in flight, makes exactly ONE wombat:wipe-memory IPC call", async () => {
    let resolveWipe: (value: unknown) => void = () => {};
    const wipe = stubWipeBridge(
      () =>
        new Promise((resolve) => {
          resolveWipe = resolve;
        }),
    );
    render(<DangerZone />);
    openModal();
    typeConfirm("WIPE");

    const confirmButton = screen.getByRole("button", { name: /confirm wipe/i });
    fireEvent.click(confirmButton);
    expect(wipe).toHaveBeenCalledTimes(1);

    // The button disables on the first click's synchronous state update -
    // a second click while in flight fires the handler's own guard too.
    fireEvent.click(screen.getByRole("button", { name: /wiping/i }));
    expect(wipe).toHaveBeenCalledTimes(1);

    resolveWipe({ status: "wiped", archivePath: "C:/fake/archives/wipe-1" });
    await waitFor(() => {
      expect(screen.queryByRole("alertdialog")).toBeNull();
    });
  });
});

describe("DangerZone (TK-336 AC4: result rendering)", () => {
  it("'wiped' shows the archive path plus a persistent restart prompt using the EXISTING restart control", async () => {
    stubRuntimeBridge();
    stubWipeBridge(() =>
      Promise.resolve({ status: "wiped", archivePath: "C:/fake/archives/wipe-20260801-090000" }),
    );
    render(<DangerZone />);
    openModal();
    typeConfirm("WIPE");
    fireEvent.click(screen.getByRole("button", { name: /confirm wipe/i }));

    expect(
      await screen.findByText(/C:\/fake\/archives\/wipe-20260801-090000/),
    ).toBeTruthy();
    // The EXISTING RuntimeControls surface, not a second restart implementation.
    expect(screen.getByRole("button", { name: /restart wombat/i })).toBeTruthy();
  });

  it("'failed' shows the detail loudly (role=alert) and no success state is ever rendered", async () => {
    stubWipeBridge(() => Promise.resolve({ status: "failed", detail: "script exited with 1" }));
    render(<DangerZone />);
    openModal();
    typeConfirm("WIPE");
    fireEvent.click(screen.getByRole("button", { name: /confirm wipe/i }));

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("script exited with 1");
    expect(screen.queryByText(/memory wiped/i)).toBeNull();
    expect(screen.queryByRole("button", { name: /restart wombat/i })).toBeNull();
  });

  it("a 'busy' response leaves state idle without fabricating a result", async () => {
    stubWipeBridge(() => Promise.resolve({ status: "busy" }));
    render(<DangerZone />);
    openModal();
    typeConfirm("WIPE");
    fireEvent.click(screen.getByRole("button", { name: /confirm wipe/i }));

    await waitFor(() => {
      expect(screen.getByRole("button", { name: /confirm wipe/i }).hasAttribute("disabled")).toBe(
        false,
      );
    });
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.queryByText(/memory wiped/i)).toBeNull();
  });
});
