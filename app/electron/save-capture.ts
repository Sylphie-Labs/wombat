import { writeFileSync } from "node:fs";
import { randomBytes } from "node:crypto";
import path from "node:path";

import { resolveBackendRoot, resolveOperatorSetting } from "./env-config";

/**
 * TK-224 (Q-111(b) ruled shape): the mic-capture hand-off's write side. The
 * renderer NEVER chooses a filesystem path - it hands over raw WAV bytes via
 * `window.wombatAudio.saveCapture` (preload.ts), and THIS module resolves
 * `WOMBAT_ASR_DROP_DIR` (operator .env tier - env > .env > unset, the SAME
 * `resolveOperatorSetting` precedence `chat-info.ts` uses; `wombat_asr_drop_dir`
 * deliberately does NOT join the app-editable tier) and does the actual
 * write, straight into the directory `src/wombat/sources/asr.py`'s
 * `ASRSource` already watches non-recursively for `.wav`/`.m4a`/`.mp3`/
 * `.flac` - ZERO new Python ingest code.
 */

const DROP_DIR_ENV_VAR = "WOMBAT_ASR_DROP_DIR";

export type SaveCaptureResult =
  | { readonly ok: true; readonly path: string }
  | { readonly ok: false; readonly reason: "drop-dir-not-configured" | "write-failed" };

/** `capture-<utc-stamp>-<rand>.wav` - colons/dots stripped from the stamp so it's a valid
 * filename on Windows too. */
function captureFilename(): string {
  const stamp = new Date().toISOString().replace(/[:.]/g, "-");
  const rand = randomBytes(4).toString("hex");
  return `capture-${stamp}-${rand}.wav`;
}

/**
 * Resolves the drop-dir and writes `buffer` into it as a new capture file.
 * The directory itself is NEVER created here - a missing/unwritable
 * directory (an operator misconfiguration distinct from "unset") collapses
 * to `write-failed`, the same closed failure shape a permissions error or a
 * full disk would produce.
 */
export function saveCapture(
  buffer: ArrayBuffer,
  env: NodeJS.ProcessEnv,
  appPath: string,
): SaveCaptureResult {
  const backendRoot = resolveBackendRoot(env, appPath);
  const configuredDir = resolveOperatorSetting(DROP_DIR_ENV_VAR, env, backendRoot);
  if (configuredDir === null || configuredDir.trim() === "") {
    return { ok: false, reason: "drop-dir-not-configured" };
  }

  const dropDir = path.resolve(backendRoot, configuredDir.trim());
  const filePath = path.join(dropDir, captureFilename());
  try {
    writeFileSync(filePath, Buffer.from(buffer));
  } catch {
    return { ok: false, reason: "write-failed" };
  }
  return { ok: true, path: filePath };
}
