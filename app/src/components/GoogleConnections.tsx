import { useEffect, useRef, useState } from "react";

import {
  connectGoogleService,
  getGoogleStatus,
  type GoogleConnectionStatus,
  type GoogleServiceName,
  type GoogleStatusResponse,
} from "../api";
import { ink, radius, status } from "../tokens";
import { Button } from "./Button";
import { cn } from "./cn";
import { Panel } from "./Panel";

/**
 * TK-257 (DEC-50): the API Keys view's Google-connection rows - one per
 * service (Google Calendar, Gmail) over `GET /google/status` +
 * `POST /google/{service}/connect` (TK-256). Honest to the four backend
 * connection states (`not_configured`/`not_connected`/`expired`/`connected`)
 * and the three consent states (`idle`/`in_progress`/`error`) - no token or
 * secret is ever displayed, status words only.
 *
 * Polling: `GET /google/status` is re-fetched on an interval ONLY while some
 * service's `consent` is `"in_progress"` - it stops the moment every service
 * is back to a terminal consent state (`idle`/`error`). A service this
 * component itself put into `in_progress` that lands on `connected` triggers
 * the TK-224/`AudioPanel` restart-notice pattern (a plain notice line; the
 * restart action itself stays in `RuntimeControls` - out of scope here).
 */

const SERVICE_ORDER: readonly GoogleServiceName[] = ["gcal", "gmail"];

const SERVICE_LABELS: Record<GoogleServiceName, string> = {
  gcal: "Google Calendar",
  gmail: "Gmail",
};

const STATUS_LABELS: Record<GoogleConnectionStatus, string> = {
  not_configured: "Not configured",
  not_connected: "Not connected",
  expired: "Expired",
  connected: "Connected",
};

const POLL_INTERVAL_MS = 800;

type LoadState =
  | { kind: "loading" }
  | { kind: "error"; message: string }
  | { kind: "loaded"; data: GoogleStatusResponse };

function statusDotClass(value: GoogleConnectionStatus): string {
  return value === "connected" ? status.positive : status.neutral;
}

function StatusChip({ value }: { value: GoogleConnectionStatus }) {
  return (
    <span className="inline-flex items-center gap-2 text-sm">
      <span className={cn("size-2", radius.full, statusDotClass(value))} aria-hidden="true" />
      <span className={ink.muted}>{STATUS_LABELS[value]}</span>
    </span>
  );
}

function buttonLabel(value: GoogleConnectionStatus): string {
  return value === "expired" || value === "connected" ? "Reconnect" : "Connect";
}

export function GoogleConnections() {
  const [state, setState] = useState<LoadState>({ kind: "loading" });
  const [restartNotice, setRestartNotice] = useState(false);
  // Services THIS component put into consent "in_progress" via a connect
  // click - used only to decide whether a later "connected" status earns the
  // restart notice (an already-connected service on initial load must not).
  const watchedRef = useRef<Set<GoogleServiceName>>(new Set());

  useEffect(() => {
    let cancelled = false;
    getGoogleStatus()
      .then((data) => {
        if (cancelled) return;
        setState({ kind: "loaded", data });
      })
      .catch((error: unknown) => {
        if (cancelled) return;
        setState({
          kind: "error",
          message: error instanceof Error ? error.message : String(error),
        });
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const anyInProgress =
    state.kind === "loaded" && SERVICE_ORDER.some((s) => state.data[s].consent === "in_progress");

  useEffect(() => {
    if (!anyInProgress) return;
    let cancelled = false;
    const id = setInterval(() => {
      getGoogleStatus()
        .then((data) => {
          if (cancelled) return;
          for (const service of SERVICE_ORDER) {
            if (watchedRef.current.has(service) && data[service].consent !== "in_progress") {
              watchedRef.current.delete(service);
              if (data[service].status === "connected") {
                setRestartNotice(true);
              }
            }
          }
          setState({ kind: "loaded", data });
        })
        .catch(() => {
          /* transient poll failure - retry on the next tick */
        });
    }, POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [anyInProgress]);

  function handleConnect(service: GoogleServiceName): void {
    if (state.kind !== "loaded") return;
    watchedRef.current.add(service);
    const data = state.data;
    setState({
      kind: "loaded",
      data: {
        ...data,
        [service]: { ...data[service], consent: "in_progress", error: undefined },
      },
    });
    connectGoogleService(service).catch(() => {
      watchedRef.current.delete(service);
      setState((prev) =>
        prev.kind === "loaded"
          ? { kind: "loaded", data: { ...prev.data, [service]: data[service] } }
          : prev,
      );
    });
  }

  return (
    <Panel className="flex flex-col gap-4">
      <h2 className="text-sm font-semibold">Google connections</h2>

      {state.kind === "loading" && <p className={ink.muted}>Loading connection status…</p>}
      {state.kind === "error" && (
        <p className={ink.muted}>Unavailable — couldn't load Google connection status.</p>
      )}

      {state.kind === "loaded" &&
        SERVICE_ORDER.map((service) => {
          const info = state.data[service];
          const showButton = info.status !== "not_configured";
          const disabled = info.consent === "in_progress";
          return (
            <div key={service} className="flex flex-col gap-1">
              <div className="flex items-center justify-between gap-3">
                <div className="flex flex-col gap-1">
                  <span className="text-sm font-medium">{SERVICE_LABELS[service]}</span>
                  <StatusChip value={info.status} />
                </div>
                {showButton && (
                  <Button
                    type="button"
                    variant="secondary"
                    disabled={disabled}
                    onClick={() => handleConnect(service)}
                  >
                    {buttonLabel(info.status)}
                  </Button>
                )}
              </div>
              {info.consent === "in_progress" && (
                <p className={ink.muted}>Waiting for you to approve in the browser...</p>
              )}
              {info.consent === "error" && info.error && (
                <p role="alert" className={ink.primary}>
                  {info.error}
                </p>
              )}
            </div>
          );
        })}

      {restartNotice && (
        <p className={ink.muted}>Restart Wombat so it picks up the new connection - Settings &gt; System.</p>
      )}
    </Panel>
  );
}
