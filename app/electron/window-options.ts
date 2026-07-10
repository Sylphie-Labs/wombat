import path from "node:path";

/**
 * TK-198: pinned Electron security posture.
 *
 * This constant is consumed verbatim by `createWindow` in main.ts. It is kept
 * as a standalone, side-effect-free module so vitest can import and assert on
 * the literal values without booting Electron (Q-109(e)).
 *
 * - contextIsolation: true  - renderer JS never shares a context with preload/Node.
 * - nodeIntegration: false  - the renderer never gets direct Node globals.
 * - sandbox: true           - the renderer process runs OS-sandboxed.
 * - preload                 - the ONLY bridge into the renderer; exposes nothing
 *   beyond what preload.ts explicitly puts on `contextBridge`.
 */
export const PRELOAD_PATH: string = path.join(__dirname, "preload.js");

export const WEB_PREFERENCES = {
  contextIsolation: true,
  nodeIntegration: false,
  sandbox: true,
  preload: PRELOAD_PATH,
} as const;
