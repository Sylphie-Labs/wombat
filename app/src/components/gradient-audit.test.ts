import { describe, expect, it } from "vitest";

import { listAuditedSourceFiles, readAuditedFile } from "../test-support/audit-fs";

// TK-225 AC2(a): zero gradient utilities or CSS gradients anywhere in
// app/src - covers Tailwind's bg-gradient-*/from-*/via-*/to-* utilities and
// the CSS linear-/radial-/conic-gradient() functions in one case-insensitive
// substring check.
describe("gradient audit", () => {
  const files = listAuditedSourceFiles();

  it("scans at least one file", () => {
    expect(files.length).toBeGreaterThan(0);
  });

  it.each(files)("%s contains no gradient reference", (file) => {
    const source = readAuditedFile(file);
    expect(/gradient/i.test(source), `a gradient reference was found in ${file}`).toBe(false);
  });
});
