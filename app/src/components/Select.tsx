import type { SelectHTMLAttributes } from "react";

import { border, focusRing, ink, radius, surface, transition } from "../tokens";
import { cn } from "./cn";

export interface SelectOption {
  value: string;
  label: string;
}

export interface SelectProps extends SelectHTMLAttributes<HTMLSelectElement> {
  id: string;
  label: string;
  options: SelectOption[];
}

/** TK-225: a label+select pair - provider/persona-axis selects (TK-200) build on this. */
export function Select({ id, label, options, className, ...props }: SelectProps) {
  return (
    <div className="flex flex-col gap-1">
      <label htmlFor={id} className={cn("text-sm font-medium", ink.muted)}>
        {label}
      </label>
      <select
        id={id}
        className={cn(
          surface.panel,
          border.default,
          "border px-3 py-2 text-sm",
          ink.primary,
          radius.md,
          transition.colors,
          focusRing,
          "disabled:cursor-not-allowed disabled:opacity-50",
          className,
        )}
        {...props}
      >
        {options.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
    </div>
  );
}
