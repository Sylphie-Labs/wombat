import { spawn } from "node:child_process";
import path from "node:path";

import { resolveBackendRoot } from "./env-config";

/**
 * TK-239 (DEC-42 second half, Q-116 pinned shape): the "restart wombat"
 * button's main-process seam. Spawns TK-238's `scripts/restart-wombat.ps1`
 * DETACHED (`windowsHide`+`detached`+`stdio: 'ignore'`, then `child.unref()`)
 * so the runtime console it starts is never parented to Electron and
 * survives the app closing - `unref()` does NOT suppress the child's own
 * `exit`/`error` events, so this handler still awaits one of them to report
 * a result. The script's exit-code contract (TK-238): 0 = restarted,
 * nonzero = failed. A spawn error is the same `failed` shape, carrying the
 * error message as `detail`. Closed vocabulary: `restarted` | `failed` (with
 * `detail`) | `busy` - nothing else, never silent.
 */

export type RestartResult =
  | { readonly status: "restarted" }
  | { readonly status: "failed"; readonly detail: string }
  | { readonly status: "busy" };

// ASMP/TK-239: exactly one restart may be in flight process-wide. A second
// invocation while one is running returns `busy` WITHOUT spawning a second
// script - cleared in `finally` so a later call after this one settles can
// spawn again.
let restartInFlight = false;

/**
 * Resolves TK-238's script under `resolveBackendRoot` - no new root-finding
 * logic, the same seam `chat-info.ts`/`save-capture.ts` use.
 */
export function resolveRestartScriptPath(env: NodeJS.ProcessEnv, appPath: string): string {
  return path.join(resolveBackendRoot(env, appPath), "scripts", "restart-wombat.ps1");
}

/**
 * Spawns the restart script detached and resolves once it reports success or
 * failure (or immediately with `busy` if a prior call hasn't settled yet).
 * `main.ts` wires this behind `ipcMain.handle('wombat:restart-runtime', ...)`.
 */
export function restartRuntime(
  env: NodeJS.ProcessEnv,
  appPath: string,
): Promise<RestartResult> {
  if (restartInFlight) {
    return Promise.resolve({ status: "busy" });
  }
  restartInFlight = true;

  const scriptPath = resolveRestartScriptPath(env, appPath);

  return new Promise((resolve) => {
    let settled = false;
    const finish = (result: RestartResult): void => {
      if (settled) {
        return;
      }
      settled = true;
      restartInFlight = false;
      resolve(result);
    };

    const child = spawn(
      "powershell.exe",
      ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", scriptPath],
      { windowsHide: true, detached: true, stdio: "ignore" },
    );

    child.on("error", (error: Error) => {
      finish({ status: "failed", detail: error.message });
    });

    child.on("exit", (code: number | null) => {
      if (code === 0) {
        finish({ status: "restarted" });
      } else {
        finish({ status: "failed", detail: `restart script exited with code ${String(code)}` });
      }
    });

    // TK-239 pinned shape: unref lets the app quit without waiting on this
    // child, but does NOT suppress the error/exit listeners above.
    child.unref();
  });
}
