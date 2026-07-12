import { ink } from "../tokens";
import { Panel } from "./Panel";

/**
 * TK-251 (DEC-48(d) honest render, recorded deferral): the Steward's
 * notepad section. NO scratch/notepad data route exists (out of scope for
 * this ticket - `wombat_scratchpad` has no read endpoint yet), so this
 * renders ONLY the design brief's designed EMPTY state (design brief
 * section 11.3/Board 5's "Notepad is empty." card) - never a fabricated
 * entry, never a loading/degraded state for data this app cannot fetch.
 */
export function NotepadCard() {
  return (
    <Panel className="flex flex-col gap-3">
      <h2 className="text-sm font-semibold">Steward's notepad</h2>
      <p className={ink.muted}>Notepad is empty.</p>
    </Panel>
  );
}
