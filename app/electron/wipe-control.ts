import { spawn } from "node:child_process";
import path from "node:path";

import { resolveBackendRoot } from "./env-config";

/**
 * TK-336 (DEC-77 r3 pinned shape): the danger-zone "wipe memory" button's
 * main-process seam - the SAME shape as TK-239's `runtime-control.ts`, down
 * to the in-flight latch, the spawn options, and the settled-latch `finish`.
 * Spawns `scripts/wipe-wombat.ps1` (TK-337's stop-then-wipe wrapper around
 * TK-334/TK-335's archive-then-truncate engine) DETACHED
 * (`windowsHide`+`detached`+`stdio: 'ignore'`, then `child.unref()`).
 *
 * DEC-77 r3 resolves the AC3 contradiction: `stdio` stays `'ignore'`
 * VERBATIM and this module NEVER parses stdout. Instead the archive path is
 * COMPUTED here - `<backendRoot>/archives/wipe-<yyyyMMdd-HHmmss>` (mirroring
 * `wombat.__main__._default_wipe_archive_dir`'s own format) - and handed to
 * the script as `-ArchiveDir`, so `archivePath` is KNOWN, never read back.
 *
 * The script's exit-code contract (TK-337, same as TK-238): 0 = wiped,
 * nonzero = failed. A spawn error is the same `failed` shape, carrying the
 * error message as `detail`. Closed vocabulary: `wiped` (with `archivePath`)
 * | `failed` (with `detail`) | `busy` - nothing else, never silent.
 */

export type WipeResult =
  | { readonly status: "wiped"; readonly archivePath: string }
  | { readonly status: "failed"; readonly detail: string }
  | { readonly status: "busy" };

// ASMP/TK-336: exactly one wipe may be in flight process-wide. A second
// invocation while one is running returns `busy` WITHOUT spawning a second
// script - cleared in `finish` so a later call after this one settles can
// spawn again.
let wipeInFlight = false;

/**
 * Resolves TK-337's script under `resolveBackendRoot` - no new root-finding
 * logic, the same seam `runtime-control.ts`/`chat-info.ts` use.
 */
export function resolveWipeScriptPath(env: NodeJS.ProcessEnv, appPath: string): string {
  return path.join(resolveBackendRoot(env, appPath), "scripts", "wipe-wombat.ps1");
}

/**
 * DEC-77 r3: `archives/wipe-<yyyyMMdd-HHmmss>` under the backend root,
 * computed from `now` (defaults to `new Date()`) so a caller can pin a
 * timestamp in a test. Mirrors `wombat.__main__._default_wipe_archive_dir`'s
 * local-time `%Y%m%d-%H%M%S` format.
 */
export function resolveWipeArchiveDir(
  env: NodeJS.ProcessEnv,
  appPath: string,
  now: Date = new Date(),
): string {
  const pad = (n: number, width = 2): string => String(n).padStart(width, "0");
  const timestamp =
    `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}-` +
    `${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`;
  return path.join(resolveBackendRoot(env, appPath), "archives", `wipe-${timestamp}`);
}

/**
 * Spawns the wipe script detached and resolves once it reports success or
 * failure (or immediately with `busy` if a prior call hasn't settled yet).
 * `main.ts` wires this behind `ipcMain.handle('wombat:wipe-memory', ...)`.
 */
export function wipeMemory(env: NodeJS.ProcessEnv, appPath: string): Promise<WipeResult> {
  if (wipeInFlight) {
    return Promise.resolve({ status: "busy" });
  }
  wipeInFlight = true;

  const scriptPath = resolveWipeScriptPath(env, appPath);
  const archivePath = resolveWipeArchiveDir(env, appPath);

  return new Promise((resolve) => {
    let settled = false;
    const finish = (result: WipeResult): void => {
      if (settled) {
        return;
      }
      settled = true;
      wipeInFlight = false;
      resolve(result);
    };

    const child = spawn(
      "powershell.exe",
      [
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        scriptPath,
        "-ArchiveDir",
        archivePath,
      ],
      { windowsHide: true, detached: true, stdio: "ignore" },
    );

    child.on("error", (error: Error) => {
      finish({ status: "failed", detail: error.message });
    });

    child.on("exit", (code: number | null) => {
      if (code === 0) {
        finish({ status: "wiped", archivePath });
      } else {
        finish({ status: "failed", detail: `wipe script exited with code ${String(code)}` });
      }
    });

    // TK-239 pinned shape (mirrored here): unref lets the app quit without
    // waiting on this child, but does NOT suppress the error/exit listeners
    // above.
    child.unref();
  });
}
