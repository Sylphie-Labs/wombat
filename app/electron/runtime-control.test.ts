import { EventEmitter } from "node:events";
import type { ChildProcess } from "node:child_process";
import { spawn } from "node:child_process";
import path from "node:path";

import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("node:child_process", () => ({ spawn: vi.fn() }));

import { resolveRestartScriptPath, restartRuntime } from "./runtime-control";

/**
 * TK-239 AC1/AC3: `child_process.spawn` is mocked throughout - no live
 * powershell spawn in CI. The caller-exit persistence proof (that a
 * detached, unref'd child survives Electron quitting) rides TK-238's own
 * script-level smoke, not this suite.
 */

interface FakeChild extends EventEmitter {
  unref: ReturnType<typeof vi.fn>;
}

function makeFakeChild(): FakeChild {
  const child = new EventEmitter() as FakeChild;
  child.unref = vi.fn();
  return child;
}

const ENV = { WOMBAT_BACKEND_CWD: "C:/fake-backend-root" };
const APP_PATH = "C:/fake-backend-root/app";

afterEach(() => {
  vi.mocked(spawn).mockReset();
});

describe("resolveRestartScriptPath", () => {
  it("resolves TK-238's script under resolveBackendRoot", () => {
    expect(resolveRestartScriptPath(ENV, APP_PATH)).toBe(
      path.join("C:/fake-backend-root", "scripts", "restart-wombat.ps1"),
    );
  });
});

describe("restartRuntime", () => {
  it("resolves 'restarted' on a zero exit code", async () => {
    const child = makeFakeChild();
    vi.mocked(spawn).mockReturnValue(child as unknown as ChildProcess);

    const promise = restartRuntime(ENV, APP_PATH);
    child.emit("exit", 0, null);

    await expect(promise).resolves.toEqual({ status: "restarted" });
  });

  it("resolves 'failed' with a detail on a nonzero exit code", async () => {
    const child = makeFakeChild();
    vi.mocked(spawn).mockReturnValue(child as unknown as ChildProcess);

    const promise = restartRuntime(ENV, APP_PATH);
    child.emit("exit", 1, null);

    const result = await promise;
    expect(result.status).toBe("failed");
    expect((result as { detail: string }).detail).toContain("1");
  });

  it("resolves 'failed' with the error message on a spawn error", async () => {
    const child = makeFakeChild();
    vi.mocked(spawn).mockReturnValue(child as unknown as ChildProcess);

    const promise = restartRuntime(ENV, APP_PATH);
    child.emit("error", new Error("ENOENT: powershell.exe not found"));

    await expect(promise).resolves.toEqual({
      status: "failed",
      detail: "ENOENT: powershell.exe not found",
    });
  });

  it("a second invocation while one is in flight returns 'busy' WITHOUT a second spawn", async () => {
    const child = makeFakeChild();
    vi.mocked(spawn).mockReturnValue(child as unknown as ChildProcess);

    const first = restartRuntime(ENV, APP_PATH);
    expect(vi.mocked(spawn)).toHaveBeenCalledTimes(1);

    const second = restartRuntime(ENV, APP_PATH);
    await expect(second).resolves.toEqual({ status: "busy" });
    expect(vi.mocked(spawn)).toHaveBeenCalledTimes(1);

    child.emit("exit", 0, null);
    await expect(first).resolves.toEqual({ status: "restarted" });
  });

  it("allows a new spawn once the prior call has settled", async () => {
    const childOne = makeFakeChild();
    vi.mocked(spawn).mockReturnValueOnce(childOne as unknown as ChildProcess);

    const first = restartRuntime(ENV, APP_PATH);
    childOne.emit("exit", 0, null);
    await first;

    const childTwo = makeFakeChild();
    vi.mocked(spawn).mockReturnValueOnce(childTwo as unknown as ChildProcess);
    const second = restartRuntime(ENV, APP_PATH);
    childTwo.emit("exit", 0, null);

    await expect(second).resolves.toEqual({ status: "restarted" });
    expect(vi.mocked(spawn)).toHaveBeenCalledTimes(2);
  });

  it("(AC3) spawns detached and hidden, ignoring stdio, and unrefs the child", async () => {
    const child = makeFakeChild();
    vi.mocked(spawn).mockReturnValue(child as unknown as ChildProcess);

    const promise = restartRuntime(ENV, APP_PATH);

    expect(vi.mocked(spawn)).toHaveBeenCalledWith(
      "powershell.exe",
      expect.arrayContaining([
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        resolveRestartScriptPath(ENV, APP_PATH),
      ]),
      expect.objectContaining({ windowsHide: true, detached: true, stdio: "ignore" }),
    );
    expect(child.unref).toHaveBeenCalledTimes(1);

    child.emit("exit", 0, null);
    await promise;
  });
});
