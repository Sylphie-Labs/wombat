import { describe, expect, it } from "vitest";

import { isAllowedPermission } from "./permissions";

/** TK-224 (TK-198 pinned-posture discipline): 'media' is the only allowed permission. */
describe("isAllowedPermission", () => {
  it("allows 'media'", () => {
    expect(isAllowedPermission("media")).toBe(true);
  });

  it.each([
    "notifications",
    "geolocation",
    "clipboard-read",
    "clipboard-sanitized-write",
    "midi",
    "midiSysex",
    "pointerLock",
    "fullscreen",
    "openExternal",
    "display-capture",
  ])("denies %s", (permission) => {
    expect(isAllowedPermission(permission)).toBe(false);
  });
});
