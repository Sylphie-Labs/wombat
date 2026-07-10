import { describe, expect, it } from "vitest";

import { listAuditedSourceFiles, readAuditedFile } from "../test-support/audit-fs";

// TK-225 AC2(c): animation usage is confined to the DESIGN.md-pinned
// allowlist - transition-colors (hover/active color changes) and
// transition-shadow (the one focus-visible transition, applied via
// tokens.ts's `focusRing`). Nothing else - no animate-*, no duration-*/
// ease-* modifier - appears anywhere in app/src.
const ALLOWLIST = new Set(["transition-colors", "transition-shadow"]);
const CLASS_TOKEN = /\b(?:transition|animate|duration|ease)-[\w[\]/.%-]+/g;

describe("animation allowlist audit", () => {
  const files = listAuditedSourceFiles();

  it("scans at least one file", () => {
    expect(files.length).toBeGreaterThan(0);
  });

  it.each(files)("%s uses only allowlisted animation classes", (file) => {
    const source = readAuditedFile(file);
    const matches = source.match(CLASS_TOKEN) ?? [];
    const offenders = matches.filter((token) => !ALLOWLIST.has(token));
    expect(
      offenders,
      `non-allowlisted animation class in ${file}: ${offenders.join(", ")}`,
    ).toEqual([]);
  });

  it("the allowlist is exactly the two DESIGN.md-pinned classes", () => {
    expect([...ALLOWLIST].sort()).toEqual(["transition-colors", "transition-shadow"]);
  });
});
