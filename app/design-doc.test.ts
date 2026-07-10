import { readFileSync } from "node:fs";
import path from "node:path";

import { describe, expect, it } from "vitest";

const DESIGN_MD_PATH = path.join(__dirname, "DESIGN.md");

// TK-225 review AC: app/DESIGN.md must record theme completeness, the
// bright color-theory rationale, and the extension rules future surfaces
// follow (DEC-39(4)). This is a cheap structural check, not a substitute
// for the human review pass - it only guards against the sections silently
// disappearing later.
describe("DESIGN.md completeness", () => {
  const doc = readFileSync(DESIGN_MD_PATH, "utf-8");

  it.each([
    "color theory",
    "theme completeness",
    "animation allowlist",
    "extension rules",
    "no gradients",
    "never mui",
  ])('mentions "%s"', (phrase) => {
    expect(doc.toLowerCase()).toContain(phrase);
  });
});
