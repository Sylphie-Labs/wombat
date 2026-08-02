import { useEffect, useState } from "react";
import { AlertTriangle } from "lucide-react";

import { border, ink, radius, surface } from "../tokens";
import { Button } from "./Button";
import { cn } from "./cn";
import { Field } from "./Field";
import { Icon } from "./Icon";
import { Panel } from "./Panel";
import { RuntimeControls } from "./RuntimeControls";

/**
 * TK-336 (DEC-76 Jim-confirmed/binding, DEC-77 r3): the danger-zone "wipe
 * memory" surface. Rides `window.wombatWipe.wipe()` (the preload bridge,
 * `wipe-control.ts` on the main-process side) - the renderer never spawns
 * anything itself, exactly `RuntimeControls`' discipline. The confirmation
 * modal refuses to arm the destructive button until the operator types
 * WIPE exactly (case-sensitive, trimmed) - a typed confirmation, not a
 * second OK button, because the act is irreversible. A successful wipe
 * never restarts on its own (the script deliberately leaves the runtime
 * stopped, DEC-75f) - it renders the EXISTING `RuntimeControls` restart
 * control persistently rather than a second restart implementation. No
 * usage tracking of any kind (DEC-29).
 */

export interface WipeMemoryResult {
  readonly status: "wiped" | "failed" | "busy";
  readonly archivePath?: string;
  readonly detail?: string;
}

declare global {
  interface Window {
    wombatWipe: {
      wipe(): Promise<WipeMemoryResult>;
    };
  }
}

const CONFIRM_WORD = "WIPE";

// DEC-76 (Jim-confirmed, binding): settings/keys/the Google connection/
// wombat's tables themselves stay in what SURVIVES - never listed here.
const ARCHIVED_AND_WIPED: readonly string[] = [
  "the queue and pending set",
  "the day ledger",
  "the action trail",
  "behavior events",
  "observations",
  "external items",
  "the scratchpad",
  "seen-events",
  "user facts",
  "chat turns",
  "the brief, feedback, and voice-drop files",
];

const NOT_TOUCHED: readonly string[] = [
  "settings",
  "API keys",
  "the Google connection",
  "wombat's tables themselves",
];

type WipeState =
  | { readonly kind: "idle" }
  | { readonly kind: "pending" }
  | { readonly kind: "wiped"; readonly archivePath: string }
  | { readonly kind: "failed"; readonly detail: string };

export function DangerZone() {
  const [modalOpen, setModalOpen] = useState(false);
  const [confirmText, setConfirmText] = useState("");
  const [state, setState] = useState<WipeState>({ kind: "idle" });

  const canArm = confirmText.trim() === CONFIRM_WORD;

  function openModal(): void {
    setConfirmText("");
    setModalOpen(true);
  }

  function closeModal(): void {
    setModalOpen(false);
    setConfirmText("");
  }

  // Escape closes regardless of which element inside the modal has focus -
  // a document-level listener, active ONLY while the modal is open, rather
  // than one scoped to a particular focused child.
  useEffect(() => {
    if (!modalOpen) return;
    function onKeyDown(event: KeyboardEvent): void {
      if (event.key === "Escape") {
        closeModal();
      }
    }
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [modalOpen]);

  async function handleConfirm(): Promise<void> {
    // Renderer-side single-flight guard (TK-336 AC2), mirroring
    // RuntimeControls' own `state.kind === "pending"` check - a double-click
    // fires exactly one invocation since the button also disables on the
    // first click's synchronous state update.
    if (!canArm || state.kind === "pending") return;
    setState({ kind: "pending" });
    try {
      const result = await window.wombatWipe.wipe();
      if (result.status === "wiped") {
        setModalOpen(false);
        setConfirmText("");
        setState({ kind: "wiped", archivePath: result.archivePath ?? "" });
      } else if (result.status === "busy") {
        // A wipe was already in flight elsewhere - leave its own outcome to
        // settle rather than fabricating one here.
        setState({ kind: "idle" });
      } else {
        setModalOpen(false);
        setConfirmText("");
        setState({ kind: "failed", detail: result.detail ?? "unknown error" });
      }
    } catch (error) {
      setModalOpen(false);
      setConfirmText("");
      setState({
        kind: "failed",
        detail: error instanceof Error ? error.message : String(error),
      });
    }
  }

  return (
    <Panel className="flex flex-col gap-4">
      <h2 className="text-sm font-semibold">Danger zone</h2>
      <div className="flex items-center gap-2">
        <Button type="button" variant="danger" onClick={openModal}>
          <Icon icon={AlertTriangle} />
          Wipe memory
        </Button>
      </div>

      {state.kind === "wiped" && (
        <div className="flex flex-col gap-3">
          <p className={ink.primary}>
            Memory wiped. Archived to: {state.archivePath}
          </p>
          <RuntimeControls />
        </div>
      )}
      {state.kind === "failed" && (
        <p role="alert" className={ink.primary}>
          Wipe failed: {state.detail}
        </p>
      )}

      {modalOpen && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-ink-primary/50"
          onClick={closeModal}
        >
          <div
            role="alertdialog"
            aria-modal="true"
            aria-labelledby="danger-zone-modal-title"
            className={cn(
              surface.elevated,
              border.strong,
              radius.lg,
              "w-full max-w-md border p-6 shadow-sm flex flex-col gap-4",
            )}
            onClick={(event) => event.stopPropagation()}
          >
            <h3 id="danger-zone-modal-title" className={cn("text-sm font-semibold", ink.primary)}>
              Wipe wombat's memory?
            </h3>

            <div className="flex flex-col gap-1">
              <p className={ink.primary}>This will archive, then permanently erase:</p>
              <ul className={cn(ink.muted, "list-disc pl-5 text-sm")}>
                {ARCHIVED_AND_WIPED.map((item) => (
                  <li key={item}>{item}</li>
                ))}
              </ul>
            </div>

            <div className="flex flex-col gap-1">
              <p className={ink.primary}>This will NOT touch:</p>
              <ul className={cn(ink.muted, "list-disc pl-5 text-sm")}>
                {NOT_TOUCHED.map((item) => (
                  <li key={item}>{item}</li>
                ))}
              </ul>
            </div>

            <Field
              id="danger-zone-confirm-input"
              label={`Type ${CONFIRM_WORD} to confirm`}
              value={confirmText}
              onChange={(event) => setConfirmText(event.target.value)}
              autoComplete="off"
            />

            <div className="flex items-center justify-end gap-2">
              <Button type="button" variant="secondary" onClick={closeModal}>
                Cancel
              </Button>
              <Button
                type="button"
                variant="danger"
                disabled={!canArm || state.kind === "pending"}
                onClick={() => void handleConfirm()}
              >
                {state.kind === "pending" ? "Wiping..." : "Confirm wipe"}
              </Button>
            </div>
          </div>
        </div>
      )}
    </Panel>
  );
}
