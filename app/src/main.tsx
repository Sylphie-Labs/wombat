import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import "./styles.css";

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

function App() {
  return (
    <div style={{ fontFamily: "sans-serif", padding: "2rem" }}>
      <h1>Wombat</h1>
      <p>Electron shell scaffold (TK-198).</p>
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
