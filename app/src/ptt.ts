import { useEffect, useRef, useState } from "react";

import { getSettings } from "./api";
import { MicCapture } from "./audio";

/**
 * TK-276 (DEC-58 a/b/e, depends on TK-275's `wombat_ptt_binding` capture):
 * app-level HOLD-to-talk listener into the EXISTING `MicCapture` -> `encodeWav`
 * -> `window.wombatAudio.saveCapture` -> `WOMBAT_ASR_DROP_DIR` -> `ASRSource`
 * path, byte-untouched. Zero new Python, zero new transcription machinery.
 *
 * `usePushToTalk` is self-contained (mirrors `AudioPanel`'s own-fetch style,
 * TK-224's docstring) - it reads `wombat_ptt_binding` itself via
 * `getSettings()` rather than being prop-drilled, so it is mounted ONCE at
 * App level with no wiring beyond that. `announcePttBinding` is the one
 * seam back from `AudioPanel`: DEC-58 c/d's "Set push-to-talk" flow PUTs a
 * fresh binding without a restart notice (TK-275, ruled), so the App-level
 * listener needs a way to pick up that change immediately rather than only
 * on the next full settings fetch.
 */

export type ParsedBinding =
  | { readonly kind: "key"; readonly code: string }
  | { readonly kind: "mouse"; readonly button: number };

/** Pure parser for the "key:<KeyboardEvent.code>" / "mouse:<MouseEvent.button>" wire format
 * (`AudioPanel.tsx`'s `describeBinding` counterpart) - "" (unbound) and anything malformed
 * parse to `null`. */
export function parseBinding(binding: string): ParsedBinding | null {
  if (!binding) return null;
  const [kind, value] = binding.split(":", 2);
  if (kind === "key" && value) {
    return { kind: "key", code: value };
  }
  if (kind === "mouse" && value) {
    const button = Number(value);
    if (Number.isInteger(button)) {
      return { kind: "mouse", button };
    }
  }
  return null;
}

/** Guard (b): typing in the chat pane (or any input/textarea/contenteditable) never opens the
 * mic, even while a binding is held down. */
export function isEditableTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  return (
    target.tagName === "INPUT" ||
    target.tagName === "TEXTAREA" ||
    target.isContentEditable ||
    // jsdom (the test environment) never computes `isContentEditable` - fall back to the
    // attribute directly so the guard is provable under vitest, not just in a real browser.
    target.getAttribute("contenteditable") === "true"
  );
}

export type CaptureLatchOwner = "ptt" | "manual";

let latchOwner: CaptureLatchOwner | null = null;

/**
 * Shared single-capture-at-a-time latch: PTT and `AudioPanel`'s manual Record session both
 * acquire/release it around their own capture session so only one `MicCapture` is ever open at
 * once. A failed acquire is never a silent no-op - the caller that lost has its OWN visible
 * truth already (the Record button's Stop state for the manual side; the PTT press simply
 * defers).
 */
export function acquireCaptureLatch(owner: CaptureLatchOwner): boolean {
  if (latchOwner !== null) return false;
  latchOwner = owner;
  return true;
}

export function releaseCaptureLatch(owner: CaptureLatchOwner): void {
  if (latchOwner === owner) latchOwner = null;
}

export function isCaptureLatchHeld(): boolean {
  return latchOwner !== null;
}

/** Test-only escape hatch, mirroring `resetBridgeCacheForTests` in `api.ts`. */
export function resetCaptureLatchForTests(): void {
  latchOwner = null;
}

type BindingListener = (binding: string) => void;
const bindingListeners = new Set<BindingListener>();

/** Called by `AudioPanel` right after it persists a newly-captured binding so the App-level
 * listener adopts it immediately - DEC-58 c/d's binding capture carries no restart notice. */
export function announcePttBinding(binding: string): void {
  for (const listener of bindingListeners) listener(binding);
}

function subscribePttBinding(listener: BindingListener): () => void {
  bindingListeners.add(listener);
  return () => {
    bindingListeners.delete(listener);
  };
}

export interface PushToTalkState {
  /** A hold is currently open - the visible recording indicator. */
  readonly active: boolean;
  /** The last hold's `saveCapture` hand-off reported `drop-dir-not-configured` (guard e) - the
   * same degrade truth `AudioPanel` renders. */
  readonly degraded: boolean;
}

/**
 * Mounted ONCE at App level. Installs document-level keydown/keyup +
 * mousedown/mouseup listeners only while a non-empty binding is set (guard
 * d - unbound installs nothing). HOLD semantics only (DEC-58 a - no
 * toggle): a matching press starts a new `MicCapture` on the default device
 * and a matching release stops it through the existing save path. Release
 * IS the mute - PTT never consults `AudioPanel`'s muted state.
 */
export function usePushToTalk(): PushToTalkState {
  const [binding, setBinding] = useState("");
  const [active, setActive] = useState(false);
  const [degraded, setDegraded] = useState(false);
  const captureRef = useRef<MicCapture | null>(null);

  useEffect(() => {
    let cancelled = false;
    getSettings()
      .then((response) => {
        if (!cancelled) setBinding(String(response.settings.wombat_ptt_binding ?? ""));
      })
      .catch(() => {
        /* stays unbound - no listeners install, per guard d */
      });
    const unsubscribe = subscribePttBinding((next) => {
      if (!cancelled) setBinding(next);
    });
    return () => {
      cancelled = true;
      unsubscribe();
    };
  }, []);

  useEffect(() => {
    const parsed = parseBinding(binding);
    if (!parsed) return; // guard d: unbound installs no listeners

    function startCapture(): void {
      if (captureRef.current) return; // already open - never double-start
      if (!acquireCaptureLatch("ptt")) return; // guard c: manual Record session holds the latch
      const capture = new MicCapture();
      captureRef.current = capture;
      setActive(true);
      capture.start().catch(() => {
        captureRef.current = null;
        releaseCaptureLatch("ptt");
        setActive(false);
      });
    }

    function stopCapture(): void {
      const capture = captureRef.current;
      if (!capture) return;
      captureRef.current = null;
      setActive(false);
      releaseCaptureLatch("ptt");
      void capture.stop().then((outcome) => {
        if (
          outcome.kind === "saved" &&
          !outcome.result.ok &&
          outcome.result.reason === "drop-dir-not-configured"
        ) {
          setDegraded(true);
        }
      });
    }

    // Narrowed to plain locals (not re-read off `parsed` inside the closures below) - TS
    // doesn't retain a closure-captured union's narrowing across function boundaries.
    const keyCode = parsed.kind === "key" ? parsed.code : null;
    const mouseButton = parsed.kind === "mouse" ? parsed.button : null;

    function onKeyDown(event: KeyboardEvent): void {
      if (keyCode === null || event.code !== keyCode) return;
      if (event.repeat) return; // guard a: auto-repeat never restarts capture
      if (isEditableTarget(event.target)) return; // guard b
      startCapture();
    }

    function onKeyUp(event: KeyboardEvent): void {
      if (keyCode === null || event.code !== keyCode) return;
      stopCapture();
    }

    function onMouseDown(event: MouseEvent): void {
      if (mouseButton === null || event.button !== mouseButton) return;
      if (isEditableTarget(event.target)) return; // guard b
      startCapture();
    }

    function onMouseUp(event: MouseEvent): void {
      if (mouseButton === null || event.button !== mouseButton) return;
      stopCapture();
    }

    document.addEventListener("keydown", onKeyDown);
    document.addEventListener("keyup", onKeyUp);
    document.addEventListener("mousedown", onMouseDown);
    document.addEventListener("mouseup", onMouseUp);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      document.removeEventListener("keyup", onKeyUp);
      document.removeEventListener("mousedown", onMouseDown);
      document.removeEventListener("mouseup", onMouseUp);
      // The binding changed (or the app is tearing down) while a hold was open - release
      // rather than leak the latch forever.
      if (captureRef.current) {
        const capture = captureRef.current;
        captureRef.current = null;
        releaseCaptureLatch("ptt");
        setActive(false);
        void capture.stop();
      }
    };
  }, [binding]);

  return { active, degraded };
}
