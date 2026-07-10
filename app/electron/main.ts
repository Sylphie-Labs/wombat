import { app, BrowserWindow, dialog, ipcMain } from "electron";
import path from "node:path";

import { describeFailure, startApiProcess, type ApiProcessHandle } from "./api-process";
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
  const result = await startApiProcess();
  if (!result.ok) {
    dialog.showErrorBox(
      "Wombat settings API failed to start",
      describeFailure(result.reason),
    );
    app.exit(1);
    return;
  }

  apiHandle = result.handle;
  const info = result.handle.info;
  // TK-199 pinned renderer surface (TK-200 binds against this channel name) -
  // the ONLY way the renderer learns the port+token is via this handle,
  // reached through the preload contextBridge; never a URL parameter.
  ipcMain.handle("wombat:settings-api-info", () => info);

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
