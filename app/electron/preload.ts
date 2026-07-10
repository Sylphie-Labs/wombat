import { contextBridge, ipcRenderer } from "electron";

/**
 * TK-199 preload script.
 *
 * Runs in an isolated context (contextIsolation: true, sandbox: true) with
 * no direct Node integration in the renderer (nodeIntegration: false). It is
 * the ONLY bridge between the renderer and privileged APIs. The settings-API
 * port+token (main.ts's `wombat:settings-api-info` handle, TK-199) is
 * exposed EXCLUSIVELY here via `contextBridge` - never as a URL parameter or
 * renderer-visible env var. The renderer surface (`window.wombatSettings.getInfo()`)
 * is pinned - TK-200 binds against these exact names.
 */
contextBridge.exposeInMainWorld("wombatSettings", {
  getInfo: () => ipcRenderer.invoke("wombat:settings-api-info"),
});
