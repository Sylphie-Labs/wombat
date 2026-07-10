/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

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
  plugins: [react(), tailwindcss()],
  test: {
    environment: "node",
  },
});
