import { spawn, type ChildProcess } from "node:child_process";

/**
 * TK-199: pure-testable Electron main <-> settings-API process lifecycle
 * (Q-110(c) ruled shape).
 *
 * The spawn command resolves from the `WOMBAT_PYTHON` env var (default
 * "python"), invoking `-m wombat.settings_app` (DEC-31 v1 dev-mode). That
 * child's handshake contract (src/wombat/settings_app/__main__.py) is: print
 * EXACTLY ONE machine-readable JSON line `{"port": <int>, "token": <str>}` to
 * stdout, flushed, with nothing preceding it, before serving. Every failure
 * mode - spawn error, premature exit, an unparsable line, or a timeout - is
 * classified distinctly so the caller (main.ts) can show a loud, specific
 * error surface rather than a silent blank window.
 */

/** Env var naming the python interpreter to spawn; falls back to "python". */
export const WOMBAT_PYTHON_ENV_VAR = "WOMBAT_PYTHON";
export const DEFAULT_PYTHON_COMMAND = "python";

/** Fixed dev-mode invocation (DEC-31 v1) - runs the settings API as a module. */
export const SETTINGS_APP_MODULE_ARGS: readonly string[] = ["-m", "wombat.settings_app"];

/** How long to wait for the handshake line before classifying a timeout. */
export const HANDSHAKE_TIMEOUT_MS = 15000;

export interface HandshakeInfo {
  readonly port: number;
  readonly token: string;
}

export type HandshakeFailureReason =
  | { readonly kind: "spawn-error"; readonly error: Error }
  | {
      readonly kind: "premature-exit";
      readonly code: number | null;
      readonly signal: NodeJS.Signals | null;
    }
  | { readonly kind: "parse-error"; readonly line: string }
  | { readonly kind: "timeout" };

export interface ApiProcessHandle {
  readonly child: ChildProcess;
  readonly info: HandshakeInfo;
  /** Idempotent - guarantees the underlying child.kill() fires exactly once. */
  teardown(): void;
}

export type StartApiProcessResult =
  | { readonly ok: true; readonly handle: ApiProcessHandle }
  | { readonly ok: false; readonly reason: HandshakeFailureReason };

/** Resolves the python command per Q-110(c): `WOMBAT_PYTHON` env var, default "python". */
export function resolvePythonCommand(env: NodeJS.ProcessEnv = process.env): string {
  return env[WOMBAT_PYTHON_ENV_VAR] || DEFAULT_PYTHON_COMMAND;
}

/**
 * Parses ONE handshake line. Accepts exactly one JSON object with an integer
 * `port` and a nonempty string `token`; anything else (malformed JSON,
 * missing/wrong-typed fields, an empty token) is a parse failure - returns
 * `null` so the caller can wrap it into a distinct `parse-error` reason.
 */
export function parseHandshake(line: string): HandshakeInfo | null {
  let parsed: unknown;
  try {
    parsed = JSON.parse(line);
  } catch {
    return null;
  }
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    return null;
  }
  const { port, token } = parsed as Record<string, unknown>;
  if (!Number.isInteger(port)) {
    return null;
  }
  if (typeof token !== "string" || token.length === 0) {
    return null;
  }
  return { port: port as number, token };
}

function makeTeardown(child: ChildProcess): () => void {
  let killed = false;
  return () => {
    if (killed) {
      return;
    }
    killed = true;
    child.kill();
  };
}

export interface StartApiProcessOptions {
  /** Overrides the resolved python command - tests substitute a Node stand-in. */
  readonly command?: string;
  /** Overrides SETTINGS_APP_MODULE_ARGS - tests script a stand-in child. */
  readonly args?: readonly string[];
  readonly env?: NodeJS.ProcessEnv;
  readonly timeoutMs?: number;
}

/**
 * Spawns the settings-API child and resolves once its handshake line is read
 * (or a distinct failure is classified). `command`/`args` are overridable so
 * tests can substitute a scripted `node -e ...` stand-in in place of python -
 * the real python spawn is exercised by TK-201's smoke, not this unit path.
 */
export function startApiProcess(
  options: StartApiProcessOptions = {},
): Promise<StartApiProcessResult> {
  const env = options.env ?? process.env;
  const command = options.command ?? resolvePythonCommand(env);
  const args = options.args ?? SETTINGS_APP_MODULE_ARGS;
  const timeoutMs = options.timeoutMs ?? HANDSHAKE_TIMEOUT_MS;

  return new Promise((resolve) => {
    let settled = false;
    let buffer = "";

    const child = spawn(command, args, {
      env,
      stdio: ["ignore", "pipe", "pipe"],
    });

    const finish = (result: StartApiProcessResult): void => {
      if (settled) {
        return;
      }
      settled = true;
      clearTimeout(timer);
      child.stdout?.removeListener("data", onStdoutData);
      child.removeListener("error", onError);
      child.removeListener("exit", onExit);
      resolve(result);
    };

    const onStdoutData = (chunk: Buffer): void => {
      buffer += chunk.toString("utf-8");
      const newlineIndex = buffer.indexOf("\n");
      if (newlineIndex === -1) {
        return;
      }
      const line = buffer.slice(0, newlineIndex).trim();
      const info = parseHandshake(line);
      if (info === null) {
        finish({ ok: false, reason: { kind: "parse-error", line } });
        child.kill();
        return;
      }
      finish({ ok: true, handle: { child, info, teardown: makeTeardown(child) } });
    };

    const onError = (error: Error): void => {
      finish({ ok: false, reason: { kind: "spawn-error", error } });
    };

    const onExit = (code: number | null, signal: NodeJS.Signals | null): void => {
      finish({ ok: false, reason: { kind: "premature-exit", code, signal } });
    };

    const timer = setTimeout(() => {
      finish({ ok: false, reason: { kind: "timeout" } });
      child.kill();
    }, timeoutMs);

    child.stdout?.on("data", onStdoutData);
    child.on("error", onError);
    child.on("exit", onExit);
  });
}

/**
 * Maps a failure reason to a human-readable message naming WHICH failure
 * occurred, for `dialog.showErrorBox` (main.ts wires this into showErrorBox +
 * `app.exit(1)` - the loud-degrade rule: never a silent blank window).
 */
export function describeFailure(reason: HandshakeFailureReason): string {
  switch (reason.kind) {
    case "spawn-error":
      return `Failed to launch the Python settings process: ${reason.error.message}`;
    case "premature-exit":
      return `The Python settings process exited before completing its handshake (code=${String(reason.code)}, signal=${String(reason.signal)}).`;
    case "parse-error":
      return `The Python settings process sent an unreadable handshake line: ${reason.line}`;
    case "timeout":
      return "Timed out waiting for the Python settings process to become ready.";
    default: {
      const exhaustive: never = reason;
      return exhaustive;
    }
  }
}
