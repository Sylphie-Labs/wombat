/**
 * TK-225: a tiny class-name joiner - no new dependency (`clsx`/`cva`) for
 * something this small. Falsy entries drop out so conditional token classes
 * read naturally at call sites.
 */
export function cn(...classes: Array<string | false | null | undefined>): string {
  return classes.filter(Boolean).join(" ");
}
