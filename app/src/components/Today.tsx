import { ink } from "../tokens";
import { InboxHighlights } from "./InboxHighlights";
import { NotepadCard } from "./NotepadCard";
import { Panel } from "./Panel";
import { Upcoming } from "./Upcoming";

/**
 * TK-249/250/251: the default landing view. The morning brief has no data
 * route yet (still an honest placeholder); Upcoming (TK-250) and Inbox
 * highlights (TK-251) load their live `GET /external/*` sections, and the
 * Steward's notepad (TK-251, DEC-48(d)) renders its designed honest-empty
 * state - no scratch/notepad data route exists yet.
 */
export function Today() {
  return (
    <div className="flex flex-col gap-6">
      <Panel>
        <h2 className="text-sm font-semibold">Morning brief</h2>
        <p className={ink.muted}>Not available yet.</p>
      </Panel>
      <Upcoming />
      <InboxHighlights />
      <NotepadCard />
    </div>
  );
}
