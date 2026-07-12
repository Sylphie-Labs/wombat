import { readFileSync } from "node:fs";
import path from "node:path";

import { describe, expect, it } from "vitest";

// TK-198 AC2: the CSP and the loaded-URL posture are asserted at the source
// level (parsing the shipped index.html / grepping main.ts), so these tests
// run under plain vitest without booting Electron.

const INDEX_HTML_PATH = path.join(__dirname, "..", "index.html");
const MAIN_TS_PATH = path.join(__dirname, "main.ts");
const PRELOAD_TS_PATH = path.join(__dirname, "preload.ts");

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

// TK-223 AC3(ii): chat port+token reach the renderer ONLY via the
// contextBridge, never a URL parameter or a raw Node/Electron global -
// extends the existing preload/security scan pattern above.
describe("preload.ts chat bridge posture", () => {
  const source = readFileSync(PRELOAD_TS_PATH, "utf-8");

  it("exposes wombatChat via contextBridge.exposeInMainWorld", () => {
    expect(source).toMatch(/contextBridge\.exposeInMainWorld\(\s*["']wombatChat["']/);
  });

  it("wombatChat's getInfo is backed by ipcRenderer.invoke, not a raw ipcRenderer exposure", () => {
    const match = source.match(
      /contextBridge\.exposeInMainWorld\(\s*["']wombatChat["'],\s*\{([\s\S]*?)\}\s*\);/,
    );
    expect(match).not.toBeNull();
    const body = (match as RegExpMatchArray)[1];
    expect(body).toMatch(/getInfo:\s*\(\)\s*=>\s*ipcRenderer\.invoke\(\s*["']wombat:chat-info["']/);
  });

  it("never exposes the raw ipcRenderer object or a Node global to the renderer", () => {
    expect(source).not.toMatch(/exposeInMainWorld\(\s*["'](?:ipcRenderer|electron|require|process)["']/);
  });
});

// TK-224 (Q-111(b)): the mic-capture hand-off never lets the renderer choose
// or learn a filesystem path - it exposes ONLY a buffer-in bridge, backed by
// ipcRenderer.invoke, the same posture as the settings/chat bridges above.
describe("preload.ts audio bridge posture", () => {
  const source = readFileSync(PRELOAD_TS_PATH, "utf-8");

  it("exposes wombatAudio via contextBridge.exposeInMainWorld", () => {
    expect(source).toMatch(/contextBridge\.exposeInMainWorld\(\s*["']wombatAudio["']/);
  });

  it("wombatAudio's saveCapture is backed by ipcRenderer.invoke, not a raw ipcRenderer exposure", () => {
    const match = source.match(
      /contextBridge\.exposeInMainWorld\(\s*["']wombatAudio["'],\s*\{([\s\S]*?)\}\s*\);/,
    );
    expect(match).not.toBeNull();
    const body = (match as RegExpMatchArray)[1];
    expect(body).toMatch(
      /saveCapture:\s*\(buffer[^)]*\)\s*=>\s*ipcRenderer\.invoke\(\s*["']wombat:save-capture["']/,
    );
  });
});

describe("main.ts permission-request posture", () => {
  const source = readFileSync(MAIN_TS_PATH, "utf-8");

  it("wires setPermissionRequestHandler to the pure isAllowedPermission predicate", () => {
    expect(source).toMatch(/setPermissionRequestHandler/);
    expect(source).toMatch(/isAllowedPermission\(permission\)/);
  });
});

// TK-251 (RULING r3): the "open in Gmail" bridge never lets the renderer
// reach `shell` directly or supply a URL - it exposes ONLY a message-id-in
// bridge, backed by ipcRenderer.invoke, the same posture as the bridges
// above.
describe("preload.ts gmail bridge posture", () => {
  const source = readFileSync(PRELOAD_TS_PATH, "utf-8");

  it("exposes wombatGmail via contextBridge.exposeInMainWorld", () => {
    expect(source).toMatch(/contextBridge\.exposeInMainWorld\(\s*["']wombatGmail["']/);
  });

  it("wombatGmail's openMessage is backed by ipcRenderer.invoke, not a raw ipcRenderer exposure", () => {
    const match = source.match(
      /contextBridge\.exposeInMainWorld\(\s*["']wombatGmail["'],\s*\{([\s\S]*?)\}\s*\);/,
    );
    expect(match).not.toBeNull();
    const body = (match as RegExpMatchArray)[1];
    expect(body).toMatch(
      /openMessage:\s*\(messageId[^)]*\)\s*=>\s*ipcRenderer\.invoke\(\s*["']wombat:open-gmail-message["']/,
    );
  });

  it("never passes a renderer-supplied URL - only messageId crosses the bridge", () => {
    const match = source.match(
      /contextBridge\.exposeInMainWorld\(\s*["']wombatGmail["'],\s*\{([\s\S]*?)\}\s*\);/,
    );
    expect(match).not.toBeNull();
    const body = (match as RegExpMatchArray)[1];
    expect(body).not.toMatch(/\burl\b/i);
  });
});

describe("main.ts gmail-open posture", () => {
  const source = readFileSync(MAIN_TS_PATH, "utf-8");

  it("wires the open-gmail-message channel through gmail-open.ts's validator, never a raw shell.openExternal(messageId) call", () => {
    expect(source).toMatch(/openGmailMessage\(\s*messageId/);
    expect(source).not.toMatch(/shell\.openExternal\(\s*messageId\s*\)/);
  });
});
