import type { InputHTMLAttributes } from "react";

import { border, focusRing, ink, radius, surface, transition } from "../tokens";
import { cn } from "./cn";

export interface FieldProps extends InputHTMLAttributes<HTMLInputElement> {
  id: string;
  label: string;
}

/** TK-225: a label+input pair - the base unit TK-200's settings form fields build on. */
export function Field({ id, label, className, ...props }: FieldProps) {
  return (
    <div className="flex flex-col gap-1">
      <label htmlFor={id} className={cn("text-sm font-medium", ink.muted)}>
        {label}
      </label>
      <input
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
      />
    </div>
  );
}
