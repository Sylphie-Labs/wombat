/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { configDefaults } from "vitest/config";

// TK-198: minimal Vite config for the renderer. Tailwind v4 is wired via its
// first-party Vite plugin (DEC-39) - only a placeholder style ships here; the
// real token/theme system is TK-225. `test` configures vitest for the
// security-posture spec files (electron/*.test.ts) and the design-system
// audits (app/src/**/*.test.ts), which run under plain Node - no DOM needed.
// TK-200's component tests (app/src/App.test.tsx) are the one exception -
// they render real DOM via @testing-library/react, so that file opts itself
// into jsdom via a `// @vitest-environment jsdom` pragma instead of changing
// the default here.
export default defineConfig({
  // TK-201: the renderer is always opened via `loadFile` (a `file://` URL, see main.ts's
  // `RENDERER_ENTRY_FILE`), never a server origin - Vite's default `base: "/"` emits
  // root-absolute asset URLs that 404 under `file://` (confirmed via the TK-201 Playwright
  // smoke: the built app never rendered past a blank window). Relative `base` is what
  // `loadFile` needs; harmless for `vite dev`, which already serves everything from `/`.
  base: "./",
  plugins: [react(), tailwindcss()],
  test: {
    environment: "node",
    // TK-201: app/e2e/**/*.spec.ts is Playwright-for-Electron, deliberately kept OUT of the
    // vitest "test" run (it needs a real python + the settings-app extra; `npm run e2e` owns
    // it) - excluded here since vitest's default include glob also matches "*.spec.ts".
    // Spread configDefaults.exclude so this stays additive, not a silent drop of the defaults.
    exclude: [...configDefaults.exclude, "e2e/**"],
  },
});
