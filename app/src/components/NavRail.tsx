import type { LucideIcon } from "lucide-react";
import { CalendarClock, KeyRound, Settings2, SlidersHorizontal, Sparkles } from "lucide-react";

import { border, ink, radius, surface, transition } from "../tokens";
import { cn } from "./cn";
import { Icon } from "./Icon";

export type ViewId = "today" | "persona" | "voice" | "keys" | "system";

interface NavItem {
  readonly id: ViewId;
  readonly label: string;
  readonly icon: LucideIcon;
}

const TOP_ITEM: NavItem = { id: "today", label: "Today", icon: CalendarClock };

const SETTINGS_ITEMS: readonly NavItem[] = [
  { id: "persona", label: "Persona", icon: Sparkles },
  { id: "voice", label: "Voice & Audio", icon: SlidersHorizontal },
  { id: "keys", label: "API Keys", icon: KeyRound },
  { id: "system", label: "System", icon: Settings2 },
];

export interface NavRailProps {
  active: ViewId;
  onSelect: (view: ViewId) => void;
}

/**
 * TK-249: the shell's left nav rail - Today first (the default landing
 * view), then the four settings categories the re-housed form now lives
 * across (Persona, Voice & Audio, API Keys, System).
 */
export function NavRail({ active, onSelect }: NavRailProps) {
  function renderItem(item: NavItem) {
    const isActive = item.id === active;
    return (
      <button
        key={item.id}
        type="button"
        onClick={() => onSelect(item.id)}
        aria-current={isActive ? "page" : undefined}
        className={cn(
          "flex h-8 items-center gap-2 px-2.5 text-sm",
          radius.md,
          transition.colors,
          isActive ? cn(surface.elevated, ink.primary, "font-semibold") : ink.muted,
        )}
      >
        <Icon icon={item.icon} size={14} />
        {item.label}
      </button>
    );
  }

  return (
    <nav
      className={cn(
        surface.panel,
        border.default,
        "flex w-[184px] flex-none flex-col gap-0.5 border-r p-2.5",
      )}
    >
      {renderItem(TOP_ITEM)}
      <div
        className={cn(
          "mt-4 mb-1 px-2.5 text-[10px] font-semibold tracking-widest uppercase",
          ink.muted,
        )}
      >
        Settings
      </div>
      {SETTINGS_ITEMS.map(renderItem)}
    </nav>
  );
}
