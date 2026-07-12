import { ink } from "../tokens";
import { Panel } from "./Panel";

/**
 * TK-249: the default landing view. Ships with honest placeholder sections
 * only - the morning brief, upcoming events, inbox highlights, and the
 * steward's notepad all arrive with real data in TK-250/251; until then
 * each section says so rather than faking content.
 */
export function Today() {
  return (
    <div className="flex flex-col gap-6">
      <Panel>
        <h2 className="text-sm font-semibold">Morning brief</h2>
        <p className={ink.muted}>Not available yet.</p>
      </Panel>
      <Panel>
        <h2 className="text-sm font-semibold">Upcoming</h2>
        <p className={ink.muted}>Not available yet.</p>
      </Panel>
      <Panel>
        <h2 className="text-sm font-semibold">Inbox highlights</h2>
        <p className={ink.muted}>Not available yet.</p>
      </Panel>
      <Panel>
        <h2 className="text-sm font-semibold">Steward's notepad</h2>
        <p className={ink.muted}>Not available yet.</p>
      </Panel>
    </div>
  );
}
