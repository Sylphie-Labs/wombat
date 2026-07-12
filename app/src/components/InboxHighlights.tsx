import { useEffect, useState } from "react";

import { getGmailMessages, type GmailMessageItem, type PriorityBand } from "../api";
import { border, ink, interactive, surface } from "../tokens";
import { cn } from "./cn";
import { Panel } from "./Panel";

/**
 * TK-251: the Today "Inbox highlights" section. Loads `GET /external/gmail`
 * once on mount (load-on-view only, no polling) and renders the iteration-4
 * inbox cards (design brief section 11.3) - honest to the DEC-45 five-field
 * gmail payload verbatim (`message_id, subject, sender, received_at,
 * priority_band`): no snippet/body field exists in the store, so no preview
 * line is ever rendered (DEC-48(d)). `priority_band` is a closed two-value
 * enum (`high`/`normal`) mapped to a filled/outline chip only - the
 * needs-reply/fyi label refinement stays deferred (DEC-47(c)).
 *
 * Row click is the ONLY interaction: it invokes the TK-251 "open in Gmail"
 * bridge (`window.wombatGmail.openMessage`) with the message's `message_id`
 * ONLY - this component never constructs or passes a URL itself.
 */

declare global {
  interface Window {
    wombatGmail: {
      openMessage(messageId: string): Promise<{ ok: boolean }>;
    };
  }
}

type LoadState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "loaded"; items: GmailMessageItem[]; storageUnavailable: boolean };

function formatReceivedAt(iso: string): string {
  const d = new Date(iso);
  const weekday = d.toLocaleDateString(undefined, { weekday: "short" });
  const time = `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  return `${weekday} ${time}`;
}

function chipClass(band: PriorityBand): string {
  return band === "high"
    ? cn(
        "inline-flex h-[18px] flex-none items-center rounded-full px-2 text-[9.5px] font-semibold tracking-wide uppercase",
        interactive.brand.bg,
        interactive.brand.text,
      )
    : cn(
        "inline-flex h-[18px] flex-none items-center rounded-full border px-2 text-[9.5px] font-semibold tracking-wide uppercase",
        border.strong,
        ink.muted,
      );
}

function openMessage(messageId: string): void {
  void window.wombatGmail.openMessage(messageId);
}

function MessageCard({ item }: { item: GmailMessageItem }) {
  return (
    <div
      role="button"
      tabIndex={0}
      onClick={() => openMessage(item.message_id)}
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          openMessage(item.message_id);
        }
      }}
      className={cn(
        "flex cursor-pointer items-center gap-[11px] rounded-md border px-3 py-2",
        surface.elevated,
        border.default,
      )}
    >
      <span
        className={cn(
          "flex h-7 w-7 flex-none items-center justify-center rounded-md text-xs font-semibold",
          interactive.brand.bg,
          interactive.brand.text,
        )}
      >
        {item.sender.charAt(0).toUpperCase()}
      </span>
      <span className="flex min-w-0 flex-1 flex-col gap-[1px]">
        <span className="truncate text-[13px] font-semibold">{item.subject}</span>
        <span className={cn("truncate text-[11.5px]", ink.muted)}>{item.sender}</span>
      </span>
      <span className="flex flex-none flex-col items-end gap-[3px]">
        <span className={cn("text-[10.5px]", ink.muted)}>{formatReceivedAt(item.received_at)}</span>
        <span className={chipClass(item.priority_band)}>{item.priority_band}</span>
      </span>
    </div>
  );
}

function SkeletonCard() {
  return (
    <div
      className={cn(
        "flex h-[46px] items-center rounded-md border border-dashed px-3 text-[11.5px]",
        border.strong,
        surface.elevated,
        ink.muted,
      )}
    >
      Loading inbox…
    </div>
  );
}

export function InboxHighlights() {
  const [state, setState] = useState<LoadState>({ status: "loading" });

  useEffect(() => {
    let cancelled = false;
    getGmailMessages()
      .then((response) => {
        if (cancelled) return;
        setState({
          status: "loaded",
          items: response.items,
          storageUnavailable: response.storage_unavailable,
        });
      })
      .catch((error: unknown) => {
        if (cancelled) return;
        setState({
          status: "error",
          message: error instanceof Error ? error.message : String(error),
        });
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <Panel className="flex flex-col gap-3">
      <h2 className="text-sm font-semibold">Inbox highlights</h2>

      {state.status === "loading" && (
        <div className="flex flex-col gap-2">
          <SkeletonCard />
          <SkeletonCard />
        </div>
      )}

      {state.status === "error" && (
        <p className={ink.muted}>Unavailable — couldn't load inbox.</p>
      )}

      {state.status === "loaded" && state.storageUnavailable && (
        <p className={ink.muted}>Unavailable — storage offline.</p>
      )}

      {state.status === "loaded" && !state.storageUnavailable && state.items.length === 0 && (
        <p className={ink.muted}>Nothing in the inbox needs you.</p>
      )}

      {state.status === "loaded" && !state.storageUnavailable && state.items.length > 0 && (
        <div className="flex flex-col gap-2">
          {state.items.map((item) => (
            <MessageCard key={item.message_id} item={item} />
          ))}
        </div>
      )}
    </Panel>
  );
}
