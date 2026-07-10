import { existsSync, mkdirSync, mkdtempSync, readdirSync, rmSync } from "node:fs";
import os from "node:os";
import path from "node:path";

import { afterEach, describe, expect, it } from "vitest";

import { saveCapture } from "./save-capture";

/**
 * TK-224 AC1: saveCapture is exercised end-to-end against a throwaway temp
 * directory standing in for the drop-dir - real node:fs writes, no mocking -
 * mirroring env-config.test.ts/chat-info.test.ts.
 */

describe("saveCapture", () => {
  let tempDir: string;

  afterEach(() => {
    if (tempDir) {
      rmSync(tempDir, { recursive: true, force: true });
    }
  });

  it("writes a .wav file into WOMBAT_ASR_DROP_DIR and returns ok + its path", () => {
    tempDir = mkdtempSync(path.join(os.tmpdir(), "wombat-save-capture-"));
    const env = { WOMBAT_BACKEND_CWD: tempDir, WOMBAT_ASR_DROP_DIR: tempDir };

    const result = saveCapture(new ArrayBuffer(44), env, tempDir);

    expect(result.ok).toBe(true);
    if (!result.ok) throw new Error("expected ok result");
    expect(existsSync(result.path)).toBe(true);
    expect(result.path.endsWith(".wav")).toBe(true);
    expect(path.dirname(result.path)).toBe(tempDir);

    const files = readdirSync(tempDir).filter((f) => f.endsWith(".wav"));
    expect(files.length).toBe(1);
  });

  it("resolves a relative WOMBAT_ASR_DROP_DIR against the backend root", () => {
    tempDir = mkdtempSync(path.join(os.tmpdir(), "wombat-save-capture-"));
    const dropDir = path.join(tempDir, "drop");
    mkdirSync(dropDir);
    const env = { WOMBAT_BACKEND_CWD: tempDir, WOMBAT_ASR_DROP_DIR: "drop" };

    const result = saveCapture(new ArrayBuffer(10), env, tempDir);

    expect(result.ok).toBe(true);
    if (!result.ok) throw new Error("expected ok result");
    expect(path.dirname(result.path)).toBe(dropDir);
  });

  it("returns drop-dir-not-configured when WOMBAT_ASR_DROP_DIR is unset", () => {
    tempDir = mkdtempSync(path.join(os.tmpdir(), "wombat-save-capture-"));
    const env = { WOMBAT_BACKEND_CWD: tempDir };

    const result = saveCapture(new ArrayBuffer(10), env, tempDir);

    expect(result).toEqual({ ok: false, reason: "drop-dir-not-configured" });
  });

  it("returns write-failed when the resolved directory does not exist", () => {
    tempDir = mkdtempSync(path.join(os.tmpdir(), "wombat-save-capture-"));
    const env = { WOMBAT_BACKEND_CWD: tempDir, WOMBAT_ASR_DROP_DIR: "does-not-exist" };

    const result = saveCapture(new ArrayBuffer(10), env, tempDir);

    expect(result).toEqual({ ok: false, reason: "write-failed" });
  });
});
