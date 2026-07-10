import { readFileSync } from "node:fs";
import path from "node:path";

import { describe, expect, it } from "vitest";

const PACKAGE_JSON_PATH = path.join(__dirname, "..", "..", "package.json");

interface PackageJson {
  dependencies?: Record<string, string>;
  devDependencies?: Record<string, string>;
}

// TK-225 AC2(b): MUI (or any other component library) is absent from
// package.json - Tailwind is the styling system, never MUI (DEC-39).
describe("MUI absence audit", () => {
  const pkg = JSON.parse(readFileSync(PACKAGE_JSON_PATH, "utf-8")) as PackageJson;
  const names = Object.keys({ ...pkg.dependencies, ...pkg.devDependencies });

  it("has at least one dependency to check", () => {
    expect(names.length).toBeGreaterThan(0);
  });

  it("never depends on MUI or another component library", () => {
    const offenders = names.filter((name) => /mui|material-ui/i.test(name));
    expect(offenders, `component-library dependency found: ${offenders.join(", ")}`).toEqual([]);
  });
});
