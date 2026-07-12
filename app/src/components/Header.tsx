import { useEffect, useState } from "react";

import { border, brand, ink, surface } from "../tokens";
import { cn } from "./cn";

/**
 * TK-249: the app shell's header - the wombat mark, wordmark, and an honest
 * running/offline status. The status rides the SAME `window.wombatChat`
 * bridge `ChatPane` already probes (TK-223) - no new api.ts function, no new
 * fetch, just the app's one existing signal for "is wombat's runtime up".
 * Restart itself stays exclusively in Settings > System (`RuntimeControls`,
 * byte-unchanged) - the header only reports state, it never triggers one.
 */
export function Header() {
  const [running, setRunning] = useState<boolean | null>(null);

  useEffect(() => {
    let cancelled = false;
    window.wombatChat
      .getInfo()
      .then((info) => {
        if (!cancelled) setRunning(info !== null);
      })
      .catch(() => {
        if (!cancelled) setRunning(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <header
      className={cn(
        surface.panel,
        border.default,
        "flex h-12 flex-none items-center gap-3 border-b px-4",
      )}
    >
      <span
        className={cn("size-6 flex-none rounded-full border-2", brand.border)}
        aria-hidden="true"
      />
      <span className={cn("text-sm font-semibold", ink.primary)}>wombat</span>
      <span className={cn("ml-auto text-sm", ink.muted)}>
        {running === null ? "Checking..." : running ? "Running" : "Offline"}
      </span>
    </header>
  );
}
