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

/**
 * TK-223 (Q-111(a)): the runtime chat handshake (port + token, or `null`
 * when wombat isn't running / chat is disabled) is exposed the SAME way -
 * EXCLUSIVELY via `contextBridge`, never a URL parameter. The renderer
 * surface (`window.wombatChat.getInfo()`) is pinned - `app/src/chat.ts`
 * binds against this exact name.
 */
contextBridge.exposeInMainWorld("wombatChat", {
  getInfo: () => ipcRenderer.invoke("wombat:chat-info"),
});

/**
 * TK-224 (Q-111(b)): mic-capture hand-off. The renderer hands over raw WAV
 * bytes ONLY - it never chooses or learns a filesystem path; `main.ts`'s
 * `wombat:save-capture` handler resolves the drop-dir itself and does the
 * write. The renderer surface (`window.wombatAudio.saveCapture(buffer)`) is
 * pinned - `app/src/audio.ts` binds against this exact name.
 */
contextBridge.exposeInMainWorld("wombatAudio", {
  saveCapture: (buffer: ArrayBuffer) => ipcRenderer.invoke("wombat:save-capture", buffer),
});
