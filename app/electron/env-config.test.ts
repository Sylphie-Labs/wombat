import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";

import { afterEach, describe, expect, it } from "vitest";

import { parseEnvFile, resolveBackendRoot, resolveOperatorSetting } from "./env-config";

/**
 * TK-223 (Q-111(a)): env-config.ts is pure and dependency-free apart from
 * node:fs/node:path, so these tests exercise it directly against a
 * throwaway temp directory standing in for the backend root - no Electron,
 * no mocking of node:fs internals.
 */

describe("resolveBackendRoot", () => {
  it("prefers WOMBAT_BACKEND_CWD when set", () => {
    expect(resolveBackendRoot({ WOMBAT_BACKEND_CWD: "/custom/backend" }, "/app/dist")).toBe(
      "/custom/backend",
    );
  });

  it("falls back to one directory above appPath (app.getAppPath() under electron . is app/)", () => {
    const appPath = path.join("repo-root", "app");
    expect(resolveBackendRoot({}, appPath)).toBe(path.resolve(appPath, ".."));
  });
});

describe("parseEnvFile", () => {
  it("parses simple KEY=VALUE lines", () => {
    expect(parseEnvFile("WOMBAT_FOO=bar\nWOMBAT_BAZ=qux")).toEqual({
      WOMBAT_FOO: "bar",
      WOMBAT_BAZ: "qux",
    });
  });

  it("skips blank lines and #-comments", () => {
    expect(parseEnvFile("# a comment\n\nWOMBAT_FOO=bar\n  # indented comment\n")).toEqual({
      WOMBAT_FOO: "bar",
    });
  });

  it("strips one surrounding pair of double or single quotes", () => {
    expect(parseEnvFile('WOMBAT_FOO="bar"\nWOMBAT_BAZ=\'qux\'')).toEqual({
      WOMBAT_FOO: "bar",
      WOMBAT_BAZ: "qux",
    });
  });

  it("does not strip mismatched or partial quotes", () => {
    expect(parseEnvFile("WOMBAT_FOO=\"bar'\nWOMBAT_BAZ='qux")).toEqual({
      WOMBAT_FOO: "\"bar'",
      WOMBAT_BAZ: "'qux",
    });
  });

  it("ignores a line with no '='", () => {
    expect(parseEnvFile("not-a-setting\nWOMBAT_FOO=bar")).toEqual({ WOMBAT_FOO: "bar" });
  });

  it("handles CRLF line endings", () => {
    expect(parseEnvFile("WOMBAT_FOO=bar\r\nWOMBAT_BAZ=qux\r\n")).toEqual({
      WOMBAT_FOO: "bar",
      WOMBAT_BAZ: "qux",
    });
  });
});

describe("resolveOperatorSetting", () => {
  let tempDir: string;

  afterEach(() => {
    if (tempDir) {
      rmSync(tempDir, { recursive: true, force: true });
    }
  });

  it("prefers the process env over the .env file (precedence)", () => {
    tempDir = mkdtempSync(path.join(os.tmpdir(), "wombat-env-config-"));
    writeFileSync(path.join(tempDir, ".env"), "WOMBAT_THING=from-dotenv\n");

    expect(resolveOperatorSetting("WOMBAT_THING", { WOMBAT_THING: "from-env" }, tempDir)).toBe(
      "from-env",
    );
  });

  it("falls back to the .env file at the backend root when unset in process env", () => {
    tempDir = mkdtempSync(path.join(os.tmpdir(), "wombat-env-config-"));
    writeFileSync(path.join(tempDir, ".env"), "WOMBAT_THING=from-dotenv\n");

    expect(resolveOperatorSetting("WOMBAT_THING", {}, tempDir)).toBe("from-dotenv");
  });

  it("returns null when the setting is in neither the env nor an existing .env", () => {
    tempDir = mkdtempSync(path.join(os.tmpdir(), "wombat-env-config-"));
    writeFileSync(path.join(tempDir, ".env"), "WOMBAT_OTHER=x\n");

    expect(resolveOperatorSetting("WOMBAT_THING", {}, tempDir)).toBeNull();
  });

  it("returns null (never throws) when no .env file exists at the backend root", () => {
    tempDir = mkdtempSync(path.join(os.tmpdir(), "wombat-env-config-"));

    expect(resolveOperatorSetting("WOMBAT_THING", {}, tempDir)).toBeNull();
  });
});
