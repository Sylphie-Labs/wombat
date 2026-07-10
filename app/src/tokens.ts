/**
 * TK-225: typed token NAMES only.
 *
 * Every string below is a Tailwind utility class (or Tailwind utility class
 * fragment) generated FROM the single `@theme` block in `./theme.css` - no
 * color/radius/font VALUE is ever repeated here. Components import these
 * names instead of writing utility classes by hand, so renaming a design
 * concept (e.g. swapping which custom property backs "brand") is a
 * one-file change in `theme.css`; this file never needs to change to
 * re-theme the app, and there is no second place a value could drift out
 * of sync with the theme.
 */

export const surface = {
  canvas: "bg-surface-canvas",
  panel: "bg-surface-panel",
  elevated: "bg-surface-elevated",
} as const;

export const ink = {
  primary: "text-ink-primary",
  muted: "text-ink-muted",
} as const;

export const border = {
  default: "border-border-default",
  strong: "border-border-strong",
} as const;

export const radius = {
  sm: "rounded-sm",
  md: "rounded-md",
  lg: "rounded-lg",
  full: "rounded-full",
} as const;

export const font = {
  sans: "font-sans",
} as const;

/**
 * Animation allowlist (see ../DESIGN.md) - the ONLY two transition classes
 * used anywhere in app/src. `colors` covers hover/active state changes;
 * `focusRing` covers the focus-visible ring growing in via `box-shadow`.
 */
export const transition = {
  colors: "transition-colors",
  focusRing: "transition-shadow",
} as const;

/**
 * Shared focus-visible treatment - every interactive element applies this
 * exact class string so the focus ring is visually identical app-wide.
 */
export const focusRing =
  `${transition.focusRing} focus-visible:outline-none focus-visible:ring-2 ` +
  "focus-visible:ring-focus focus-visible:ring-offset-2 " +
  "focus-visible:ring-offset-surface-panel";

export const interactive = {
  brand: {
    bg: "bg-brand",
    bgHover: "hover:bg-brand-hover",
    bgActive: "active:bg-brand-active",
    text: "text-brand-ink",
  },
  danger: {
    bg: "bg-danger",
    bgHover: "hover:bg-danger-hover",
    bgActive: "active:bg-danger-active",
    text: "text-danger-ink",
  },
  neutral: {
    bg: "bg-surface-panel",
    bgHover: "hover:bg-surface-elevated",
    bgActive: "active:bg-surface-canvas",
    text: "text-ink-primary",
  },
} as const;

export const status = {
  positive: "bg-positive",
  neutral: "bg-border-strong",
} as const;
