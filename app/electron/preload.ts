/**
 * TK-198 scaffold preload script.
 *
 * Runs in an isolated context (contextIsolation: true, sandbox: true) with
 * no direct Node integration in the renderer (nodeIntegration: false). It is
 * the ONLY bridge between the renderer and privileged APIs; nothing is
 * exposed yet - later tickets (e.g. TK-199) will add `contextBridge.exposeInMainWorld`
 * calls here for the settings API handshake.
 */
export {};
