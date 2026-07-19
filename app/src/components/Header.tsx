import { useEffect, useState } from "react";

import { probeChat } from "../chat";
import { border, brand, ink, surface } from "../tokens";
import { cn } from "./cn";

/** TK-263 (ISS-16): interval between liveness probes. */
const PROBE_INTERVAL_MS = 15_000;

/**
 * TK-249: the app shell's header - the wombat mark, wordmark, and an honest
 * running/offline status. TK-263: handshake-file presence alone proved
 * stale (the file survives a dead runtime), so the status now comes from
 * `probeChat()` - a fresh `getInfo()` read plus a real round-trip to the
 * chat port - polled at mount and on a ~15s interval so a runtime death
 * between polls flips the chip without a reload. Restart itself stays
 * exclusively in Settings > System (`RuntimeControls`, byte-unchanged) -
 * the header only reports state, it never triggers one.
 */
export function Header() {
  const [running, setRunning] = useState<boolean | null>(null);

  useEffect(() => {
    let cancelled = false;

    const probe = () => {
      probeChat()
        .then((isRunning) => {
          if (!cancelled) setRunning(isRunning);
        })
        .catch(() => {
          if (!cancelled) setRunning(false);
        });
    };

    probe();
    const intervalId = setInterval(probe, PROBE_INTERVAL_MS);
    return () => {
      cancelled = true;
      clearInterval(intervalId);
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
