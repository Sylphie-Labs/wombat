import { readFileSync } from "node:fs";
import path from "node:path";

import { describe, expect, it } from "vitest";

const PACKAGE_JSON_PATH = path.join(__dirname, "..", "..", "package.json");

const OTHER_ICON_PACKAGES = [
  "react-icons",
  "@heroicons/react",
  "@fortawesome/fontawesome-svg-core",
  "@mui/icons-material",
  "phosphor-react",
  "@phosphor-icons/react",
  "react-feather",
];

interface PackageJson {
  dependencies?: Record<string, string>;
  devDependencies?: Record<string, string>;
}

// TK-225 AC2(d): exactly one icon package (lucide-react) is in use.
describe("icon package audit", () => {
  const pkg = JSON.parse(readFileSync(PACKAGE_JSON_PATH, "utf-8")) as PackageJson;
  const namesPresent = new Set(Object.keys({ ...pkg.dependencies, ...pkg.devDependencies }));

  it("depends on lucide-react", () => {
    expect(namesPresent.has("lucide-react")).toBe(true);
  });

  it("depends on no other icon package", () => {
    const others = OTHER_ICON_PACKAGES.filter((name) => namesPresent.has(name));
    expect(others, `additional icon package(s) found: ${others.join(", ")}`).toEqual([]);
  });
});
