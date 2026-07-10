import { describe, expect, it, vi } from "vitest";

import {
  DEFAULT_PYTHON_COMMAND,
  HANDSHAKE_TIMEOUT_MS,
  SETTINGS_APP_MODULE_ARGS,
  WOMBAT_PYTHON_ENV_VAR,
  describeFailure,
  parseHandshake,
  resolvePythonCommand,
  startApiProcess,
} from "./api-process";

// TK-199: the real python spawn is TK-201's smoke - these tests drive the
// pure spawn/parse/timeout/teardown functions against a scripted Node
// stand-in child (`node -e ...`) in place of python.

describe("resolvePythonCommand", () => {
  it("defaults to 'python' when WOMBAT_PYTHON is unset", () => {
    expect(resolvePythonCommand({})).toBe(DEFAULT_PYTHON_COMMAND);
  });

  it("honors the WOMBAT_PYTHON env var", () => {
    expect(resolvePythonCommand({ WOMBAT_PYTHON: "/usr/bin/python3.11" })).toBe(
      "/usr/bin/python3.11",
    );
  });
});

describe("parseHandshake", () => {
  it("accepts a well-formed handshake line", () => {
    expect(parseHandshake('{"port": 54321, "token": "abc123"}')).toEqual({
      port: 54321,
      token: "abc123",
    });
  });

  it("rejects malformed JSON", () => {
    expect(parseHandshake("not json")).toBeNull();
  });

  it("rejects a missing port", () => {
    expect(parseHandshake('{"token": "abc123"}')).toBeNull();
  });

  it("rejects a non-integer port", () => {
    expect(parseHandshake('{"port": "54321", "token": "abc123"}')).toBeNull();
  });

  it("rejects a missing token", () => {
    expect(parseHandshake('{"port": 54321}')).toBeNull();
  });

  it("rejects an empty token", () => {
    expect(parseHandshake('{"port": 54321, "token": ""}')).toBeNull();
  });

  it("rejects a JSON array", () => {
    expect(parseHandshake("[54321, \"abc123\"]")).toBeNull();
  });
});

describe("startApiProcess against a scripted Node stand-in", () => {
  it("resolves {port, token} from the stand-in's stdout", async () => {
    const result = await startApiProcess({
      command: "node",
      args: ["-e", "console.log(JSON.stringify({port: 54321, token: 'tok-abc'}))"],
    });

    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.handle.info).toEqual({ port: 54321, token: "tok-abc" });
      result.handle.teardown();
    }
  });

  it("classifies an unparsable handshake line distinctly", async () => {
    const result = await startApiProcess({
      command: "node",
      args: ["-e", "console.log('not json')"],
    });

    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.reason.kind).toBe("parse-error");
    }
  });

  it("classifies a spawn error distinctly", async () => {
    const result = await startApiProcess({
      command: "wombat-definitely-not-a-real-binary",
      args: [],
    });

    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.reason.kind).toBe("spawn-error");
    }
  });

  it("classifies a premature exit distinctly", async () => {
    const result = await startApiProcess({
      command: "node",
      args: ["-e", "process.exit(1)"],
    });

    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.reason.kind).toBe("premature-exit");
      if (result.reason.kind === "premature-exit") {
        expect(result.reason.code).toBe(1);
      }
    }
  });

  it("classifies a handshake timeout distinctly", async () => {
    const result = await startApiProcess({
      command: "node",
      args: ["-e", "setInterval(() => {}, 1000)"],
      timeoutMs: 300,
    });

    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.reason.kind).toBe("timeout");
    }
  }, 5000);

  it("kills the stand-in child exactly once on teardown - no orphan", async () => {
    const result = await startApiProcess({
      command: "node",
      args: [
        "-e",
        "console.log(JSON.stringify({port: 1, token: 'x'})); setInterval(() => {}, 1000)",
      ],
    });

    expect(result.ok).toBe(true);
    if (result.ok) {
      const killSpy = vi.spyOn(result.handle.child, "kill");
      result.handle.teardown();
      result.handle.teardown();
      expect(killSpy).toHaveBeenCalledTimes(1);
    }
  });
});

describe("describeFailure", () => {
  it("names the spawn-error failure", () => {
    const message = describeFailure({ kind: "spawn-error", error: new Error("boom") });
    expect(message).toMatch(/launch/i);
    expect(message).toMatch(/boom/);
  });

  it("names the premature-exit failure", () => {
    const message = describeFailure({ kind: "premature-exit", code: 1, signal: null });
    expect(message).toMatch(/exit/i);
  });

  it("names the parse-error failure", () => {
    const message = describeFailure({ kind: "parse-error", line: "garbage" });
    expect(message).toMatch(/handshake|unreadable/i);
  });

  it("names the timeout failure", () => {
    const message = describeFailure({ kind: "timeout" });
    expect(message).toMatch(/timed out/i);
  });
});

describe("module constants", () => {
  it("pins the handshake timeout to 15000ms", () => {
    expect(HANDSHAKE_TIMEOUT_MS).toBe(15000);
  });

  it("pins the default spawn args to the settings_app module invocation", () => {
    expect(SETTINGS_APP_MODULE_ARGS).toEqual(["-m", "wombat.settings_app"]);
  });

  it("names the WOMBAT_PYTHON env var", () => {
    expect(WOMBAT_PYTHON_ENV_VAR).toBe("WOMBAT_PYTHON");
  });
});
