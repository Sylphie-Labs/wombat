import { readFileSync } from "node:fs";
import path from "node:path";

/**
 * TK-223 (Q-111(a) ruled shape): pure, dependency-free resolution of the
 * operator-config tier - the `.env`/process-env layer `src/wombat/config.py`
 * (pydantic-settings) reads on the Python side. TK-224 and TK-201 reuse this
 * module too, so it stays free of any electron-specific import - only
 * `node:fs`/`node:path`.
 */

/**
 * `WOMBAT_BACKEND_CWD` (a test/ops override) if set, else the repo root
 * inferred from the Electron app path (`app.getAppPath()` under `electron .`
 * is `app/`, so the backend root is one directory up).
 */
export function resolveBackendRoot(env: NodeJS.ProcessEnv, appPath: string): string {
  return env.WOMBAT_BACKEND_CWD ?? path.resolve(appPath, "..");
}

/**
 * A minimal best-effort `.env` parser, scoped to the `WOMBAT_*` keys this
 * app reads: `KEY=VALUE` lines, blank lines and `#`-comments skipped, one
 * surrounding pair of single/double quotes stripped from the value. This is
 * NOT a general-purpose `.env` parser - no multiline values, no `export`
 * prefix, no variable expansion/interpolation.
 */
export function parseEnvFile(contents: string): Record<string, string> {
  const result: Record<string, string> = {};
  for (const rawLine of contents.split(/\r?\n/)) {
    const line = rawLine.trim();
    if (line === "" || line.startsWith("#")) continue;
    const eqIndex = line.indexOf("=");
    if (eqIndex === -1) continue;
    const key = line.slice(0, eqIndex).trim();
    if (key === "") continue;
    let value = line.slice(eqIndex + 1).trim();
    const isQuoted =
      value.length >= 2 &&
      ((value.startsWith('"') && value.endsWith('"')) ||
        (value.startsWith("'") && value.endsWith("'")));
    if (isQuoted) {
      value = value.slice(1, -1);
    }
    result[key] = value;
  }
  return result;
}

function readBackendDotEnv(backendRoot: string): Record<string, string> | null {
  try {
    return parseEnvFile(readFileSync(path.join(backendRoot, ".env"), "utf-8"));
  } catch {
    return null;
  }
}

/**
 * Resolves ONE operator-tier setting by name: the process env wins, else the
 * backend root's `.env` file, else `null` - precedence-faithful to
 * pydantic-settings, which resolves the same env-var-over-dotenv order for
 * every operator-tier field (e.g. `WOMBAT_CHAT_HANDSHAKE_FILE`).
 */
export function resolveOperatorSetting(
  name: string,
  env: NodeJS.ProcessEnv,
  backendRoot: string,
): string | null {
  const fromEnv = env[name];
  if (fromEnv !== undefined) {
    return fromEnv;
  }
  const dotEnv = readBackendDotEnv(backendRoot);
  return dotEnv?.[name] ?? null;
}
