import { readFileSync, readdirSync, statSync } from "node:fs";
import path from "node:path";

const SOURCE_ROOT = path.join(__dirname, "..");
const SCANNED_EXTENSIONS = new Set([".ts", ".tsx", ".css"]);
const EXCLUDED_DIRECTORY_NAMES = new Set(["test-support"]);

/**
 * TK-225: lists every real app/src source file the honesty audits scan -
 * every .ts/.tsx/.css file under app/src, excluding vitest spec files
 * (*.test.ts(x), which necessarily quote the very substrings the audits
 * check for) and this support directory itself.
 */
export function listAuditedSourceFiles(): string[] {
  const results: string[] = [];

  function walk(dir: string): void {
    for (const entry of readdirSync(dir)) {
      if (EXCLUDED_DIRECTORY_NAMES.has(entry)) continue;
      const entryPath = path.join(dir, entry);
      const stats = statSync(entryPath);
      if (stats.isDirectory()) {
        walk(entryPath);
        continue;
      }
      const ext = path.extname(entry);
      if (!SCANNED_EXTENSIONS.has(ext)) continue;
      if (/\.test\.tsx?$/.test(entry)) continue;
      results.push(entryPath);
    }
  }

  walk(SOURCE_ROOT);
  return results;
}

export function readAuditedFile(filePath: string): string {
  return readFileSync(filePath, "utf-8");
}
