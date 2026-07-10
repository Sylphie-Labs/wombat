import type { ButtonHTMLAttributes } from "react";

import { border, focusRing, interactive, radius, transition } from "../tokens";
import { cn } from "./cn";

export type ButtonVariant = "primary" | "secondary" | "danger";

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
}

const VARIANT_CLASSES: Record<ButtonVariant, string> = {
  primary: cn(
    interactive.brand.bg,
    interactive.brand.bgHover,
    interactive.brand.bgActive,
    interactive.brand.text,
  ),
  secondary: cn(
    interactive.neutral.bg,
    interactive.neutral.bgHover,
    interactive.neutral.bgActive,
    interactive.neutral.text,
    border.default,
    "border",
  ),
  danger: cn(
    interactive.danger.bg,
    interactive.danger.bgHover,
    interactive.danger.bgActive,
    interactive.danger.text,
  ),
};

/** TK-225: the one button every interactive action in the app renders through. */
export function Button({ variant = "primary", className, ...props }: ButtonProps) {
  return (
    <button
      className={cn(
        "inline-flex items-center justify-center gap-2 px-4 py-2 text-sm font-medium",
        radius.md,
        transition.colors,
        focusRing,
        "disabled:cursor-not-allowed disabled:opacity-50",
        VARIANT_CLASSES[variant],
        className,
      )}
      {...props}
    />
  );
}
