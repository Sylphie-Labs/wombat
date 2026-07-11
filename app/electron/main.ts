import { app, BrowserWindow, dialog, ipcMain, session } from "electron";
import path from "node:path";

import { describeFailure, startApiProcess, type ApiProcessHandle } from "./api-process";
import { readChatInfo } from "./chat-info";
import { resolveBackendRoot } from "./env-config";
import { isAllowedPermission } from "./permissions";
import { restartRuntime } from "./runtime-control";
import { saveCapture } from "./save-capture";
import { WEB_PREFERENCES } from "./window-options";

/**
 * TK-198: the renderer is ALWAYS loaded from the local built file - never a
 * remote URL. This constant is the single source of truth for what gets
 * loaded, so the security posture is pinned at source level (Q-109(e)):
 * grep this file and you will find exactly one `loadFile` call and no
 * `loadURL` call anywhere.
 */
export const RENDERER_ENTRY_FILE: string = path.join(
  __dirname,
  "..",
  "dist",
  "index.html",
);

// TK-199: the settings-API child's handle - set once app.whenReady's handshake
// resolves, torn down on quit. Never null while a window may be open.
let apiHandle: ApiProcessHandle | null = null;

function createWindow(): void {
  const win = new BrowserWindow({
    width: 1200,
    height: 800,
    webPreferences: WEB_PREFERENCES,
  });

  void win.loadFile(RENDERER_ENTRY_FILE);
}

function teardownApiProcess(): void {
  apiHandle?.teardown();
}

app.whenReady().then(async () => {
  // TK-199: spawn the settings-API child and parse its handshake BEFORE any
  // window opens - python missing, the module absent, or a handshake timeout
  // must show a visible error surface, never a silent blank window.
  // TK-201 (Q-111(c)): pin the child's cwd to the resolved backend root so
  // wombat.settings.json lands where the runtime reads it (see api-process.ts).
  const result = await startApiProcess({
    cwd: resolveBackendRoot(process.env, app.getAppPath()),
  });
  if (!result.ok) {
    dialog.showErrorBox(
      "Wombat settings API failed to start",
      describeFailure(result.reason),
    );
    app.exit(1);
    return;
  }

  // TK-224 (Q-111(b), TK-198 pinned-posture discipline): Electron's default
  // permission handler grants EVERYTHING - the wrong posture to ship mic
  // capture on. Pin it to the pure `isAllowedPermission` predicate ('media'
  // only) BEFORE any window (and therefore any permission request) can exist.
  session.defaultSession.setPermissionRequestHandler((_webContents, permission, callback) => {
    callback(isAllowedPermission(permission));
  });

  apiHandle = result.handle;
  const info = result.handle.info;
  // TK-199 pinned renderer surface (TK-200 binds against this channel name) -
  // the ONLY way the renderer learns the port+token is via this handle,
  // reached through the preload contextBridge; never a URL parameter.
  ipcMain.handle("wombat:settings-api-info", () => info);

  // TK-223 (Q-111(a)): the chat handshake is RE-RESOLVED and RE-READ on
  // EVERY invocation - deliberately no caching, since a runtime started
  // after the app is already open (no app restart) must be picked up, and a
  // stale file from a dead runtime is surfaced via chat.ts's send-failure
  // path rather than pinned here.
  ipcMain.handle("wombat:chat-info", () => readChatInfo(process.env, app.getAppPath()));

  // TK-224 (Q-111(b)): the mic-capture hand-off. The renderer never chooses a
  // filesystem path - only `saveCapture` (this main process) resolves the
  // operator-tier drop-dir and performs the write.
  ipcMain.handle("wombat:save-capture", (_event, buffer: ArrayBuffer) =>
    saveCapture(buffer, process.env, app.getAppPath()),
  );

  // TK-239 (DEC-42 second half, Q-116): the restart-server button's IPC seam
  // - spawns TK-238's restart script detached; the in-flight latch lives in
  // `runtime-control.ts` itself, single-flight process-wide.
  ipcMain.handle("wombat:restart-runtime", () =>
    restartRuntime(process.env, app.getAppPath()),
  );

  createWindow();

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      createWindow();
    }
  });
});

app.on("window-all-closed", () => {
  teardownApiProcess();
  if (process.platform !== "darwin") {
    app.quit();
  }
});

app.on("before-quit", () => {
  teardownApiProcess();
});
