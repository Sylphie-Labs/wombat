// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { RuntimeControls } from "./RuntimeControls";

/**
 * TK-239 AC2: `window.wombatRuntime.restart()` is faked - no real preload,
 * no real Electron. The live IPC round trip is `runtime-control.test.ts`'s
 * spawn-shape/exit-code coverage.
 */

function installFakeRuntimeBridge(restart: ReturnType<typeof vi.fn>): void {
  (window as unknown as { wombatRuntime: { restart: typeof restart } }).wombatRuntime = {
    restart,
  };
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("RuntimeControls", () => {
  it("disables the button while pending - a double-click fires exactly one invocation", async () => {
    let resolveRestart: (value: { status: string }) => void = () => {};
    const restart = vi.fn(
      () =>
        new Promise((resolve) => {
          resolveRestart = resolve;
        }),
    );
    installFakeRuntimeBridge(restart);
    render(<RuntimeControls />);

    const button = screen.getByRole("button", { name: /restart wombat/i });
    fireEvent.click(button);
    fireEvent.click(button);

    expect(restart).toHaveBeenCalledTimes(1);
    expect((screen.getByRole("button", { name: /restarting/i }) as HTMLButtonElement).disabled).toBe(
      true,
    );

    resolveRestart({ status: "restarted" });
    await waitFor(() => expect(screen.getByText("Wombat restarted.")).toBeTruthy());
  });

  it("shows a visible success state on 'restarted'", async () => {
    const restart = vi.fn().mockResolvedValue({ status: "restarted" });
    installFakeRuntimeBridge(restart);
    render(<RuntimeControls />);

    fireEvent.click(screen.getByRole("button", { name: /restart wombat/i }));

    expect(await screen.findByText("Wombat restarted.")).toBeTruthy();
    expect(
      (screen.getByRole("button", { name: /restart wombat/i }) as HTMLButtonElement).disabled,
    ).toBe(false);
  });

  it("shows a loud error state carrying the detail on 'failed'", async () => {
    const restart = vi.fn().mockResolvedValue({
      status: "failed",
      detail: "restart script exited with code 1",
    });
    installFakeRuntimeBridge(restart);
    render(<RuntimeControls />);

    fireEvent.click(screen.getByRole("button", { name: /restart wombat/i }));

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("restart script exited with code 1");
  });

  it("returns to idle (re-enabled, no fabricated success/error) on 'busy'", async () => {
    const restart = vi.fn().mockResolvedValue({ status: "busy" });
    installFakeRuntimeBridge(restart);
    render(<RuntimeControls />);

    fireEvent.click(screen.getByRole("button", { name: /restart wombat/i }));

    await waitFor(() =>
      expect(
        (screen.getByRole("button", { name: /restart wombat/i }) as HTMLButtonElement).disabled,
      ).toBe(false),
    );
    expect(screen.queryByText("Wombat restarted.")).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
  });
});
