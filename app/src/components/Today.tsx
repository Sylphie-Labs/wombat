import { ink } from "../tokens";
import { Panel } from "./Panel";
import { Upcoming } from "./Upcoming";

/**
 * TK-249: the default landing view. Ships with honest placeholder sections
 * only - the morning brief, inbox highlights, and the steward's notepad
 * arrive with real data in TK-251; until then each section says so rather
 * than faking content. TK-250 replaces the Upcoming placeholder with the
 * live `GET /external/calendar` event-card section.
 */
export function Today() {
  return (
    <div className="flex flex-col gap-6">
      <Panel>
        <h2 className="text-sm font-semibold">Morning brief</h2>
        <p className={ink.muted}>Not available yet.</p>
      </Panel>
      <Upcoming />
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
