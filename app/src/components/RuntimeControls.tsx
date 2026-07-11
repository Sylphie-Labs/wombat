import { useState } from "react";
import { RefreshCw } from "lucide-react";

import { ink } from "../tokens";
import { Button } from "./Button";
import { Icon } from "./Icon";
import { Panel } from "./Panel";

/**
 * TK-239 (DEC-42 second half, Q-116): the restart-server button. Rides
 * `window.wombatRuntime.restart()` (the preload bridge, `runtime-control.ts`
 * on the main-process side) - the renderer never spawns anything itself.
 * Disabled while a restart is pending (a double-click fires exactly one
 * invocation, since the button disables on the FIRST click's synchronous
 * state update); shows a visible success state on `restarted` and a loud,
 * detail-carrying error state on `failed`. No usage tracking of any kind
 * (DEC-29). Plain flat styling, minimal animation (DEC-39) - the base
 * `Button`/`Icon` seams only.
 */

export interface RuntimeRestartResult {
  readonly status: "restarted" | "failed" | "busy";
  readonly detail?: string;
}

declare global {
  interface Window {
    wombatRuntime: {
      restart(): Promise<RuntimeRestartResult>;
    };
  }
}

type RestartState =
  | { readonly kind: "idle" }
  | { readonly kind: "pending" }
  | { readonly kind: "success" }
  | { readonly kind: "error"; readonly detail: string };

export function RuntimeControls() {
  const [state, setState] = useState<RestartState>({ kind: "idle" });

  async function handleRestart(): Promise<void> {
    if (state.kind === "pending") return;
    setState({ kind: "pending" });
    try {
      const result = await window.wombatRuntime.restart();
      if (result.status === "restarted") {
        setState({ kind: "success" });
      } else if (result.status === "busy") {
        // A restart was already in flight elsewhere - leave its own outcome
        // to settle rather than fabricating one here.
        setState({ kind: "idle" });
      } else {
        setState({ kind: "error", detail: result.detail ?? "unknown error" });
      }
    } catch (error) {
      setState({
        kind: "error",
        detail: error instanceof Error ? error.message : String(error),
      });
    }
  }

  return (
    <Panel className="flex flex-col gap-4">
      <h2 className="text-sm font-semibold">Runtime</h2>
      <div className="flex items-center gap-2">
        <Button
          type="button"
          variant="secondary"
          onClick={() => void handleRestart()}
          disabled={state.kind === "pending"}
        >
          <Icon icon={RefreshCw} />
          {state.kind === "pending" ? "Restarting..." : "Restart wombat"}
        </Button>
      </div>
      {state.kind === "success" && <p className={ink.primary}>Wombat restarted.</p>}
      {state.kind === "error" && (
        <p role="alert" className={ink.primary}>
          Restart failed: {state.detail}
        </p>
      )}
    </Panel>
  );
}
