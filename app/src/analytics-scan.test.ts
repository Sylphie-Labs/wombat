import path from "node:path";

import { describe, expect, it } from "vitest";

import { listAuditedSourceFiles, readAuditedFile } from "./test-support/audit-fs";

/**
 * TK-200 AC3/DEC-29: the renderer is configuration read/write ONLY - no
 * analytics/telemetry/metrics collection and no network target beyond the
 * loopback origins the TK-199/TK-223 bridges hand back (`api.ts`'s settings
 * call, `chat.ts`'s chat call - the only two `fetch` call sites). This is
 * the structural half of the AC; the human review half confirms no
 * metrics/event-log/gate-history/behavior view exists anywhere in the
 * rendered app.
 */
const ANALYTICS_KEYWORD =
  /\b(analytics|telemetry|mixpanel|amplitude|segment-analytics|posthog|gtag|google-analytics|event-log|eventlog|gate-history|gatehistory)\b/i;
const NETWORK_CALL = /\b(fetch|XMLHttpRequest|WebSocket|sendBeacon)\s*\(/;

// TK-223: chat.ts is the second (and only other) legitimate network-call
// site - it POSTs to the TK-222 chat surface's loopback, bridge-derived
// origin, the same discipline api.ts already follows for the settings API.
const PERMITTED_NETWORK_CALLERS = new Set(["api.ts", "chat.ts"]);

describe("zero-analytics scan (DEC-29)", () => {
  const files = listAuditedSourceFiles().filter(
    (file) => file.endsWith(".ts") || file.endsWith(".tsx"),
  );

  it("scans at least one file", () => {
    expect(files.length).toBeGreaterThan(0);
  });

  it.each(files)("%s carries no analytics/telemetry/metrics keyword", (file) => {
    const source = readAuditedFile(file);
    expect(ANALYTICS_KEYWORD.test(source), `analytics-shaped keyword found in ${file}`).toBe(
      false,
    );
  });

  it("only api.ts/chat.ts make a network call", () => {
    const offenders = files.filter((file) => {
      if (PERMITTED_NETWORK_CALLERS.has(path.basename(file))) return false;
      return NETWORK_CALL.test(readAuditedFile(file));
    });
    expect(offenders, `unexpected network call site(s): ${offenders.join(", ")}`).toEqual([]);
  });

  it.each([...PERMITTED_NETWORK_CALLERS])(
    "%s's network call targets only the loopback bridge-derived origin",
    (basename) => {
      const callerFile = files.find((file) => path.basename(file) === basename);
      expect(callerFile).toBeDefined();
      const source = readAuditedFile(callerFile as string);
      expect(NETWORK_CALL.test(source)).toBe(true);

      // Every literal URL in the file is loopback - no hardcoded
      // non-loopback host string is present anywhere, so the fetch target
      // can only ever be the bridge-derived `http://127.0.0.1:<port>` origin.
      const urlLiterals = source.match(/https?:\/\/[^\s"'`]+/g) ?? [];
      expect(urlLiterals.length).toBeGreaterThan(0);
      for (const literal of urlLiterals) {
        expect(literal.startsWith("http://127.0.0.1")).toBe(true);
      }
    },
  );
});
