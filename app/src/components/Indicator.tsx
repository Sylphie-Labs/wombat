import { ink, radius, status } from "../tokens";
import { cn } from "./cn";

export interface IndicatorProps {
  configured: boolean;
  label?: string;
}

/**
 * TK-225: the configured/not-configured dot TK-200's write-only key fields
 * need - it never claims a value is present, only whether one is set.
 */
export function Indicator({ configured, label }: IndicatorProps) {
  return (
    <span className="inline-flex items-center gap-2 text-sm">
      <span
        className={cn("size-2", radius.full, configured ? status.positive : status.neutral)}
        aria-hidden="true"
      />
      <span className={ink.muted}>{label ?? (configured ? "Configured" : "Not configured")}</span>
    </span>
  );
}
