import path from "node:path";

import { describe, expect, it } from "vitest";

import { listAuditedSourceFiles, readAuditedFile } from "../test-support/audit-fs";

// TK-225 AC1 (re-theme proof): every color value lives in theme.css's
// single @theme block. No other app/src file may contain a hex color
// literal or an rgb()/rgba()/hsl()/hsla() function call - components must
// carry token/utility class names only, so changing one value in
// theme.css is the only way to change what renders.
const HEX_COLOR = /#[0-9a-fA-F]{3,8}\b/;
const COLOR_FUNCTION = /\b(rgb|rgba|hsl|hsla)\(/i;

describe("color literal audit", () => {
  const files = listAuditedSourceFiles().filter((file) => path.basename(file) !== "theme.css");

  it("scans at least one file outside theme.css", () => {
    expect(files.length).toBeGreaterThan(0);
  });

  it.each(files)("%s carries no hex or rgb()/hsl() color literal", (file) => {
    const source = readAuditedFile(file);
    expect(HEX_COLOR.test(source), `hex color literal found in ${file}`).toBe(false);
    expect(COLOR_FUNCTION.test(source), `rgb()/hsl() literal found in ${file}`).toBe(false);
  });
});
