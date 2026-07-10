import type { ReactNode } from "react";

import { border, radius, surface } from "../tokens";
import { cn } from "./cn";

export interface PanelProps {
  children: ReactNode;
  className?: string;
}

/**
 * TK-225: the base grouped-content surface - the chat pane (TK-223), audio
 * controls (TK-224), and any future avatar surface all start from a Panel.
 */
export function Panel({ children, className }: PanelProps) {
  return (
    <div
      className={cn(surface.panel, border.default, radius.lg, "border p-4 shadow-sm", className)}
    >
      {children}
    </div>
  );
}
