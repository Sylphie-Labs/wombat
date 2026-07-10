import { readFileSync } from "node:fs";
import path from "node:path";

import { describe, expect, it } from "vitest";

// TK-198 AC2: the CSP and the loaded-URL posture are asserted at the source
// level (parsing the shipped index.html / grepping main.ts), so these tests
// run under plain vitest without booting Electron.

const INDEX_HTML_PATH = path.join(__dirname, "..", "index.html");
const MAIN_TS_PATH = path.join(__dirname, "main.ts");

const LOOPBACK_SOURCE = /^(https?|wss?):\/\/(127\.0\.0\.1|localhost)(:\*|:\d+)?$/;

function extractCsp(html: string): string {
  const match = html.match(
    /<meta\s+http-equiv="Content-Security-Policy"\s+content="([^"]*)"/,
  );
  if (!match) {
    throw new Error("No CSP meta tag found in index.html");
  }
  return match[1];
}

function extractDirective(csp: string, name: string): string[] {
  const directive = csp
    .split(";")
    .map((d) => d.trim())
    .find((d) => d.startsWith(`${name} `) || d === name);
  if (!directive) {
    throw new Error(`No ${name} directive found in CSP`);
  }
  return directive.split(/\s+/).slice(1);
}

describe("index.html CSP", () => {
  const html = readFileSync(INDEX_HTML_PATH, "utf-8");
  const csp = extractCsp(html);
  const connectSrc = extractDirective(csp, "connect-src");

  it("restricts connect-src to 'self' and loopback origins only", () => {
    expect(connectSrc.length).toBeGreaterThan(0);
    for (const source of connectSrc) {
      const isSelf = source === "'self'";
      const isLoopback = LOOPBACK_SOURCE.test(source);
      expect(isSelf || isLoopback, `unexpected connect-src origin: ${source}`).toBe(
        true,
      );
    }
  });

  it("never admits a wildcard or a non-loopback remote origin", () => {
    for (const source of connectSrc) {
      expect(source).not.toBe("*");
      expect(source.startsWith("https://")).toBe(false);
    }
  });
});

describe("main.ts loaded-URL posture", () => {
  const source = readFileSync(MAIN_TS_PATH, "utf-8");

  it("loads the renderer via loadFile (local file only)", () => {
    expect(source).toMatch(/\.loadFile\(/);
  });

  it("never calls loadURL anywhere (no remote content path exists)", () => {
    expect(source).not.toMatch(/\.loadURL\(/);
  });

  it("never references a remote http(s) URL literal", () => {
    expect(source).not.toMatch(/["'`]https?:\/\/(?!127\.0\.0\.1|localhost)/);
  });
});
