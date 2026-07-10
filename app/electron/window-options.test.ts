import { describe, expect, it } from "vitest";

import { WEB_PREFERENCES } from "./window-options";

// TK-198 AC2: the security posture is pinned by literal-value assertions on
// the exported webPreferences constant - no need to boot Electron.
describe("WEB_PREFERENCES", () => {
  it("isolates the renderer context", () => {
    expect(WEB_PREFERENCES.contextIsolation).toBe(true);
  });

  it("never enables Node integration in the renderer", () => {
    expect(WEB_PREFERENCES.nodeIntegration).toBe(false);
  });

  it("sandboxes the renderer process", () => {
    expect(WEB_PREFERENCES.sandbox).toBe(true);
  });

  it("names a preload script path", () => {
    expect(typeof WEB_PREFERENCES.preload).toBe("string");
    expect(WEB_PREFERENCES.preload.length).toBeGreaterThan(0);
    expect(WEB_PREFERENCES.preload.endsWith("preload.js")).toBe(true);
  });
});
