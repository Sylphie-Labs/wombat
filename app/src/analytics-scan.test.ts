import path from "node:path";

import { describe, expect, it } from "vitest";

import { listAuditedSourceFiles, readAuditedFile } from "./test-support/audit-fs";

/**
 * TK-200 AC3/DEC-29: the renderer is configuration read/write ONLY - no
 * analytics/telemetry/metrics collection and no network target beyond the
 * loopback settings-API origin the TK-199 bridge hands back (`api.ts`'s
 * sole `fetch` call site). This is the structural half of the AC; the human
 * review half confirms no metrics/event-log/gate-history/behavior view
 * exists anywhere in the rendered app.
 */
const ANALYTICS_KEYWORD =
  /\b(analytics|telemetry|mixpanel|amplitude|segment-analytics|posthog|gtag|google-analytics|event-log|eventlog|gate-history|gatehistory)\b/i;
const NETWORK_CALL = /\b(fetch|XMLHttpRequest|WebSocket|sendBeacon)\s*\(/;

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

  it("only api.ts makes a network call", () => {
    const offenders = files.filter((file) => {
      if (path.basename(file) === "api.ts") return false;
      return NETWORK_CALL.test(readAuditedFile(file));
    });
    expect(offenders, `unexpected network call site(s): ${offenders.join(", ")}`).toEqual([]);
  });

  it("api.ts's network call targets only the loopback bridge-derived origin", () => {
    const apiFile = files.find((file) => path.basename(file) === "api.ts");
    expect(apiFile).toBeDefined();
    const source = readAuditedFile(apiFile as string);
    expect(NETWORK_CALL.test(source)).toBe(true);

    // Every literal URL in the file is loopback - no hardcoded non-loopback
    // host string is present anywhere, so the fetch target can only ever be
    // the bridge-derived `http://127.0.0.1:<port>` origin.
    const urlLiterals = source.match(/https?:\/\/[^\s"'`]+/g) ?? [];
    expect(urlLiterals.length).toBeGreaterThan(0);
    for (const literal of urlLiterals) {
      expect(literal.startsWith("http://127.0.0.1")).toBe(true);
    }
  });
});
