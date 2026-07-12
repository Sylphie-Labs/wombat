import { useState } from "react";
import { PanelRightClose, PanelRightOpen } from "lucide-react";

import { border, ink, surface, transition } from "../tokens";
import { ChatPane } from "./ChatPane";
import { cn } from "./cn";
import { Icon } from "./Icon";

/**
 * TK-249: the shell's persistent, collapsible 320px chat pane - a dock
 * wrapping the existing `ChatPane` (TK-223) unchanged, so its own honest
 * "wombat is not running" state still renders exactly as before. Collapsing
 * hides the transcript/composer behind a slim strip; the pane is never
 * fully removed from any view, per the approved mock.
 */
export function ChatDock() {
  const [collapsed, setCollapsed] = useState(false);

  return (
    <aside
      className={cn(
        surface.panel,
        border.default,
        "flex flex-none flex-col border-l",
        collapsed ? "w-12" : "w-[320px]",
      )}
    >
      <div
        className={cn(
          border.default,
          "flex h-[42px] flex-none items-center gap-2 border-b px-2",
        )}
      >
        {!collapsed && <span className={cn("text-sm font-semibold", ink.primary)}>Chat</span>}
        <button
          type="button"
          onClick={() => setCollapsed((prev) => !prev)}
          aria-label={collapsed ? "Show chat" : "Hide chat"}
          className={cn(
            "ml-auto flex size-6 flex-none items-center justify-center",
            transition.colors,
            ink.muted,
          )}
        >
          <Icon icon={collapsed ? PanelRightOpen : PanelRightClose} size={14} />
        </button>
      </div>
      {!collapsed && (
        <div className="min-h-0 flex-1 overflow-y-auto p-3">
          <ChatPane />
        </div>
      )}
    </aside>
  );
}
