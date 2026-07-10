import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { LayoutPanelLeft } from "lucide-react";

import "./theme.css";
import { Icon, Indicator, Panel } from "./components";
import { font, ink, surface } from "./tokens";

/**
 * TK-198 AC3: the pinned webPreferences (contextIsolation, nodeIntegration,
 * sandbox) should make Node globals structurally unreachable from the
 * renderer. This boot assertion is the belt-and-suspenders check that fails
 * loudly if that posture ever regresses. (The full in-page Playwright probe
 * is TK-201's recorded scope - this is not a substitute for it.)
 */
function assertNoNodeGlobals(): void {
  const maybeNodeWindow = window as unknown as {
    require?: unknown;
    process?: unknown;
  };

  if (maybeNodeWindow.require !== undefined || maybeNodeWindow.process !== undefined) {
    // eslint-disable-next-line no-console
    console.error(
      "SECURITY REGRESSION: window.require/window.process is reachable from the " +
        "renderer. contextIsolation/nodeIntegration/sandbox posture must be restored.",
    );
    throw new Error("Renderer boot assertion failed: Node globals are reachable.");
  }
}

assertNoNodeGlobals();

/**
 * TK-225: the TK-198 placeholder restyled onto the token/component system -
 * this is not new product surface, just proof the design system renders.
 * The real settings form is TK-200.
 */
function App() {
  return (
    <div className={`${surface.canvas} ${font.sans} ${ink.primary} min-h-screen p-8`}>
      <Panel className="max-w-md">
        <div className="flex items-center gap-3">
          <Icon icon={LayoutPanelLeft} label="Wombat" />
          <h1 className="text-lg font-semibold">Wombat</h1>
        </div>
        <p className={`${ink.muted} mt-2 text-sm`}>
          Electron shell scaffold (TK-198), restyled onto the TK-225 design system.
        </p>
        <div className="mt-4">
          <Indicator configured={false} label="Settings API not yet wired (TK-199)" />
        </div>
      </Panel>
    </div>
  );
}

const container = document.getElementById("root");
if (!container) {
  throw new Error("Renderer boot failed: #root element not found.");
}

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
