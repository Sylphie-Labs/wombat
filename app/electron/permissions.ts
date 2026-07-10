/**
 * TK-224 (Q-111(b) ruled shape; TK-198 pinned-posture discipline): Electron's
 * DEFAULT permission-request handler grants EVERYTHING - the wrong posture to
 * ship mic capture on. `isAllowedPermission` is the pure predicate wired into
 * `session.defaultSession.setPermissionRequestHandler` in main.ts: `"media"`
 * (mic/camera access - only mic is actually used) is the ONLY permission ever
 * granted; every other request (notifications, geolocation, clipboard-read,
 * MIDI, etc.) is denied.
 */
export function isAllowedPermission(permission: string): boolean {
  return permission === "media";
}
