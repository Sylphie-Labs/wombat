import { app, BrowserWindow } from "electron";
import path from "node:path";

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

function createWindow(): void {
  const win = new BrowserWindow({
    width: 1200,
    height: 800,
    webPreferences: WEB_PREFERENCES,
  });

  void win.loadFile(RENDERER_ENTRY_FILE);
}

app.whenReady().then(() => {
  createWindow();

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      createWindow();
    }
  });
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") {
    app.quit();
  }
});
