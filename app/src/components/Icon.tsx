import type { LucideIcon } from "lucide-react";

import { ink } from "../tokens";
import { cn } from "./cn";

export interface IconProps {
  icon: LucideIcon;
  label?: string;
  className?: string;
  size?: number;
}

/**
 * TK-225: the one seam every icon renders through, so icon color/sizing
 * stays consistent app-wide and lucide-react stays the app's sole icon
 * dependency (see the icon-package audit).
 */
export function Icon({ icon: LucideIconComponent, label, className, size = 18 }: IconProps) {
  return (
    <LucideIconComponent
      size={size}
      className={cn(ink.primary, className)}
      aria-hidden={label ? undefined : true}
      role={label ? "img" : undefined}
      aria-label={label}
    />
  );
}
