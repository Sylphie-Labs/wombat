import { EventEmitter } from "node:events";
import type { ChildProcess } from "node:child_process";
import { spawn } from "node:child_process";
import path from "node:path";

import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("node:child_process", () => ({ spawn: vi.fn() }));

import { resolveWipeArchiveDir, resolveWipeScriptPath, wipeMemory } from "./wipe-control";

/**
 * TK-336 AC2/AC3: `child_process.spawn` is mocked throughout - no live
 * powershell spawn in CI, exactly `runtime-control.test.ts`'s discipline.
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
const NOW = new Date(2026, 7, 1, 9, 5, 3); // 2026-08-01 09:05:03 local

afterEach(() => {
  vi.mocked(spawn).mockReset();
});

describe("resolveWipeScriptPath", () => {
  it("resolves TK-337's script under resolveBackendRoot", () => {
    expect(resolveWipeScriptPath(ENV, APP_PATH)).toBe(
      path.join("C:/fake-backend-root", "scripts", "wipe-wombat.ps1"),
    );
  });
});

describe("resolveWipeArchiveDir", () => {
  it("computes archives/wipe-<yyyyMMdd-HHmmss> under the backend root (DEC-77 r3)", () => {
    expect(resolveWipeArchiveDir(ENV, APP_PATH, NOW)).toBe(
      path.join("C:/fake-backend-root", "archives", "wipe-20260801-090503"),
    );
  });
});

describe("wipeMemory", () => {
  it("resolves 'wiped' with the computed archivePath on a zero exit code", async () => {
    const child = makeFakeChild();
    vi.mocked(spawn).mockReturnValue(child as unknown as ChildProcess);

    const promise = wipeMemory(ENV, APP_PATH);
    child.emit("exit", 0, null);

    const result = await promise;
    expect(result.status).toBe("wiped");
    expect((result as { archivePath: string }).archivePath).toContain(
      path.join("archives", "wipe-"),
    );
  });

  it("resolves 'failed' with a detail on a nonzero exit code", async () => {
    const child = makeFakeChild();
    vi.mocked(spawn).mockReturnValue(child as unknown as ChildProcess);

    const promise = wipeMemory(ENV, APP_PATH);
    child.emit("exit", 1, null);

    const result = await promise;
    expect(result.status).toBe("failed");
    expect((result as { detail: string }).detail).toContain("1");
  });

  it("resolves 'failed' with the error message on a spawn error", async () => {
    const child = makeFakeChild();
    vi.mocked(spawn).mockReturnValue(child as unknown as ChildProcess);

    const promise = wipeMemory(ENV, APP_PATH);
    child.emit("error", new Error("ENOENT: powershell.exe not found"));

    await expect(promise).resolves.toEqual({
      status: "failed",
      detail: "ENOENT: powershell.exe not found",
    });
  });

  it("a second invocation while one is in flight returns 'busy' WITHOUT a second spawn", async () => {
    const child = makeFakeChild();
    vi.mocked(spawn).mockReturnValue(child as unknown as ChildProcess);

    const first = wipeMemory(ENV, APP_PATH);
    expect(vi.mocked(spawn)).toHaveBeenCalledTimes(1);

    const second = wipeMemory(ENV, APP_PATH);
    await expect(second).resolves.toEqual({ status: "busy" });
    expect(vi.mocked(spawn)).toHaveBeenCalledTimes(1);

    child.emit("exit", 0, null);
    await first;
  });

  it("allows a new spawn once the prior call has settled", async () => {
    const childOne = makeFakeChild();
    vi.mocked(spawn).mockReturnValueOnce(childOne as unknown as ChildProcess);

    const first = wipeMemory(ENV, APP_PATH);
    childOne.emit("exit", 0, null);
    await first;

    const childTwo = makeFakeChild();
    vi.mocked(spawn).mockReturnValueOnce(childTwo as unknown as ChildProcess);
    const second = wipeMemory(ENV, APP_PATH);
    childTwo.emit("exit", 0, null);

    await expect(second).resolves.toMatchObject({ status: "wiped" });
    expect(vi.mocked(spawn)).toHaveBeenCalledTimes(2);
  });

  it("(AC3) spawns detached and hidden, ignoring stdio, passing -ArchiveDir, and unrefs the child", async () => {
    const child = makeFakeChild();
    vi.mocked(spawn).mockReturnValue(child as unknown as ChildProcess);

    const promise = wipeMemory(ENV, APP_PATH);

    expect(vi.mocked(spawn)).toHaveBeenCalledWith(
      "powershell.exe",
      expect.arrayContaining([
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        resolveWipeScriptPath(ENV, APP_PATH),
        "-ArchiveDir",
      ]),
      expect.objectContaining({ windowsHide: true, detached: true, stdio: "ignore" }),
    );
    expect(child.unref).toHaveBeenCalledTimes(1);

    child.emit("exit", 0, null);
    await promise;
  });
});
