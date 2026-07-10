# Wombat app design system (TK-225)

Per DEC-39(4): a bright, color-theory-guided palette that is NOT
cookie-cutter and NOT "ai-slop" (the purple-gradient-on-dark-slate look most
AI tool UIs default to), fully tokenized so one file re-themes the app,
Tailwind (never MUI), zero gradients, one coherent icon set, and very
little animation. This document is the palette rationale and the rules
future surfaces (chat pane, audio controls, the eventual avatar surface)
follow when they extend the system.

## Token source of truth

`app/src/theme.css` holds exactly one `@theme` block - every color, radius,
and font-family custom property the app uses. `app/src/tokens.ts` exports
typed **names** of the Tailwind utility classes those custom properties
generate (`surface.panel === "bg-surface-panel"`, etc.) - it never repeats
a value. Change a color in `theme.css` and every component using that
token's name picks it up; nothing outside `theme.css` can drift out of
sync because nothing outside it holds a value to drift.

Components consume `tokens.ts` names, not raw Tailwind color utilities
(`bg-brand`, not `bg-amber-400`) and never a hex/`rgb()`/`hsl()` literal.
This is enforced by `app/src/components/color-audit.test.ts`, which scans
every file under `app/src` other than `theme.css` for color literals and
fails the moment one appears.

## The palette and its color theory

The palette is a **triadic scheme**: three hues spaced 120 degrees apart
on the OKLCH hue wheel, which is the classical color-theory construction
for a set of hues that read as clearly distinct while staying balanced
(no single hue dominates or reads as an afterthought).

| Token     | Hue (OKLCH) | Role                                          |
| --------- | ----------- | ---------------------------------------------- |
| `brand`   | ~75deg (amber)  | primary actions, brand identity            |
| `accent`  | ~195deg (teal)  | the focus ring, exclusively                |
| (open)    | ~315deg (magenta) | reserved triadic point - see Extension rules |

Amber and teal were picked as the two *named* triadic hues (rather than,
say, amber and its direct complement at 255deg/blue-violet) because a
120deg split keeps enough hue distance for accessibility while landing
accent teal opposite enough on the wheel from brand amber that a focus
ring never reads as "just another brand hover state." Today accent powers
only the focus ring - no component fills a background with it - so
`theme.css` declares just `--color-accent`, not a full hover/active/ink
ladder; the ladder pattern (see `brand` below) is ready to extend the
moment a real accent-filled control exists (DESIGN.md's extension rules).
`danger` (a warm red-orange, ~25deg) and `positive` (a green, ~150deg) sit
off the triad entirely and are used for exactly one purpose each
(destructive actions; the configured/not-configured `Indicator`'s dot), so
their meaning never competes with brand/accent.

Surfaces and text ("ink") are **warm near-neutrals**: very low chroma at
the same ~80deg hue family as brand, instead of the cold blue-gray
("slate") neutrals most dashboards reach for by default. Paired with a
bright triadic accent scheme, this warm-neutral base is the single
biggest visual differentiator from the generic AI-tool look.

All values are declared in OKLCH (`oklch(L% C H)`), which is perceptually
uniform - the same lightness number looks equally light across every hue,
so states (default/hover/active) can be built as pure lightness steps on
each color without visually clashing between hues.

## Theme completeness

- **Surfaces**: `surface-canvas` (app background) -> `surface-panel`
  (cards, the `Panel` component) -> `surface-elevated` (popovers/overlays
  when they exist) - three deliberate elevation steps.
- **Text**: `ink-primary` (body text) and `ink-muted` (labels, secondary
  text). Text placed on top of a filled interactive color uses that
  color's own `-ink` companion (`brand-ink`, `danger-ink`) rather than a
  separate generic "inverted" token, so contrast is always computed
  against the specific background it sits on.
- **Interactive states**: every actionable *fill* color (`brand`,
  `danger`) ships `DEFAULT` / `-hover` / `-active` steps plus an `-ink`
  companion for text/icon color placed on top of the filled color, so
  contrast is correct in every state without recomputing anything.
  Disabled state is handled uniformly via Tailwind's `disabled:opacity-50
  disabled:cursor-not-allowed` rather than a fourth color step.
- **Focus**: every interactive component (`Button`, `Field`, `Select`)
  applies the exact same `focusRing` token from `tokens.ts` -
  `focus-visible:ring-2 focus-visible:ring-focus focus-visible:ring-offset-2`
  - so focus is visually identical everywhere and never depends on the
  browser's default outline.
- **Typography**: one system-native font stack (`font-sans` in
  `theme.css`) - no network font request is ever made (the CSP's
  `style-src`/`script-src` are `'self'`-only, so an external font
  wouldn't load anyway). Type *sizes* ride Tailwind v4's built-in scale
  (`text-sm`, `text-lg`, ...), which is itself a set of CSS custom
  properties - already "one place," so it is not re-declared here.
- **Spacing**: rides Tailwind v4's built-in single `--spacing` base unit
  that drives the whole numeric scale (`p-4`, `gap-2`, ...) - likewise
  already one token, not re-declared.
- **Icons**: `lucide-react` is the app's one icon dependency, always
  rendered through the `Icon` wrapper component so size/color stay
  consistent. No other icon package is present (`icon-audit.test.ts`).

## Animation allowlist (pinned)

Exactly two transition classes are permitted anywhere in `app/src`, both
exported from `tokens.ts` as `transition.colors` / `transition.focusRing`:

1. `transition-colors` - smooths hover/active color changes on buttons,
   fields, and selects. Functional (state change is easier to perceive),
   not decorative.
2. `transition-shadow` - smooths the `box-shadow`-based focus-visible ring
   growing in via `focusRing`. Functional (focus is announced), not
   decorative.

Nothing else - no `animate-*` utility, no keyframe animation, no
transform/scale/opacity transition - is used anywhere in `app/src`.
`components/animation-audit.test.ts` scans every source file for any
`transition-*` / `animate-*` / `duration-*` / `ease-*` class token and
fails if it finds anything outside this list.

## No gradients

No `bg-gradient-*`, no CSS `linear-gradient()`/`radial-gradient()`/
`conic-gradient()`, anywhere in `app/src`, enforced by
`components/gradient-audit.test.ts` (a case-insensitive grep for
`gradient` across every source file).

## Tailwind, never MUI

`app/package.json` never gains `@mui/*`/`material-ui` (or any other
component library) as a dependency, enforced by
`components/mui-audit.test.ts`. `lucide-react` is an icon dependency, not
a component library, and does not count against this bar (DEC-39,
recorded verbatim in TK-225's Q-110 ruling).

## Extension rules (for the chat pane, audio controls, and the avatar surface)

1. **New colors extend the triad, they don't invent a new wheel.** The
   reserved ~315deg (magenta) triadic point is the first place to look for
   a genuinely new semantic color (e.g. an unread-chat-message badge);
   adding it means one more `--color-*` declaration in `theme.css` plus
   one more entry in `tokens.ts` - never a literal in a component.
2. **New components live in `app/src/components/` and consume
   `tokens.ts`, never a raw hex value or a raw Tailwind palette color.**
   The color/gradient/MUI/animation/icon audits run over the whole of
   `app/src`, so a violation in a brand-new file fails the same way a
   violation in an existing one would.
3. **New elevation needs a new `surface-*` step, not an ad-hoc shadow or
   opacity hack.** If a fourth elevation level is ever needed (e.g. a
   modal above the avatar surface), add `--color-surface-<name>` to the
   theme and a matching entry to `tokens.ts`'s `surface` object.
4. **Any new interactive/animated affordance must be added to the
   animation allowlist explicitly, in this document, before its class
   appears in code** - the audit test's allowlist and this document are
   kept in lockstep on purpose; growing the allowlist should always be a
   visible, deliberate two-line diff (test + doc), never a silent one.
5. **No control renders unless it actuates something real** (the DEC-39
   no-placebo bar carries into design: a disabled/absent state, like the
   `Indicator` in the current placeholder app, is honest; a control wired
   to nothing is not).

## Flagged follow-ups (not built here)

- **Dark/light theme switcher**: does not fall out of the token layer for
  free - the current palette is a single bright theme, not a light/dark
  pair with matched contrast in both directions. Building a real dark
  variant means a second, deliberately designed set of surface/ink/state
  values, not just inverting lightness - left for a future ticket if
  Jim wants it (per TK-225's non-goal).
- **A reserved magenta triadic token** is documented above as the
  extension path but is not declared in `theme.css` yet, since nothing
  in the app currently consumes it (no unused token is added
  speculatively - see Extension rule 1 for how to add it when something
  needs it).
