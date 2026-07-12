import { useEffect, useState } from "react";

import { getCalendarEvents, type CalendarEventItem } from "../api";
import { border, brand, ink, interactive, status, surface } from "../tokens";
import { cn } from "./cn";
import { Panel } from "./Panel";

/**
 * TK-250: the Today "Upcoming" section. Loads `GET /external/calendar`
 * once on mount (load-on-view only, no polling) and renders the iteration-4
 * event cards (design brief section 11.3) - honest to the five-field gcal
 * payload verbatim (`event_id, title, start, end, all_day`): no
 * location/attendee/source field exists in the store, so the mock's
 * meta-line icons are omitted rather than invented (DEC-48(d)).
 */

type LoadState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "loaded"; items: CalendarEventItem[]; storageUnavailable: boolean };

type Bucket = "Today" | "Tomorrow" | "Later this week";

const BUCKET_ORDER: readonly Bucket[] = ["Today", "Tomorrow", "Later this week"];

interface BucketedEvent {
  event: CalendarEventItem;
  bucket: Bucket;
  isNext: boolean;
}

function startOfDay(date: Date): Date {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate());
}

function bucketFor(startIso: string, today: Date): Bucket {
  const diffDays = Math.round(
    (startOfDay(new Date(startIso)).getTime() - today.getTime()) / 86_400_000,
  );
  if (diffDays <= 0) return "Today";
  if (diffDays === 1) return "Tomorrow";
  return "Later this week";
}

/** Bucketing is derived client-side from `start` in LOCAL time (never UTC-displayed). */
function bucketEvents(items: readonly CalendarEventItem[]): BucketedEvent[] {
  const today = startOfDay(new Date());
  const sorted = [...items].sort(
    (a, b) => new Date(a.start).getTime() - new Date(b.start).getTime(),
  );
  let nextAssigned = false;
  return sorted.map((event) => {
    const bucket = bucketFor(event.start, today);
    const isNext = bucket === "Today" && !nextAssigned;
    if (isNext) nextAssigned = true;
    return { event, bucket, isNext };
  });
}

function formatTime(iso: string): string {
  const d = new Date(iso);
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

function formatWeekday(iso: string): string {
  return new Date(iso).toLocaleDateString(undefined, { weekday: "short" });
}

function EventCard({ event, bucket, isNext }: BucketedEvent) {
  const edgeClass = event.all_day
    ? cn("border-l-[3px] border-dashed", border.strong)
    : cn(
        "w-[3px] flex-none self-stretch rounded-full",
        isNext ? interactive.brand.bg : status.neutral,
      );

  return (
    <div
      className={cn(
        "flex items-center gap-3 rounded-md py-2 pr-3",
        surface.elevated,
        "border",
        isNext ? brand.border : border.default,
      )}
    >
      <span className={edgeClass} />
      <span className="flex w-16 flex-none flex-col leading-tight [font-variant-numeric:tabular-nums]">
        {event.all_day ? (
          <span
            className={cn(
              "inline-flex h-[18px] w-fit items-center rounded-full border px-2 text-[10px] font-semibold",
              border.strong,
              ink.muted,
            )}
          >
            All-day
          </span>
        ) : bucket === "Later this week" ? (
          <>
            <span className={cn("text-[10px] tracking-wide uppercase", ink.muted)}>
              {formatWeekday(event.start)}
            </span>
            <span className="text-[15px] font-semibold">{formatTime(event.start)}</span>
          </>
        ) : (
          <>
            <span className="text-[15px] font-semibold">{formatTime(event.start)}</span>
            <span className={cn("text-[11px]", ink.muted)}>– {formatTime(event.end)}</span>
          </>
        )}
      </span>
      <span className="min-w-0 flex-1 truncate text-[13px] font-semibold">{event.title}</span>
      {isNext && (
        <span
          className={cn(
            "ml-auto inline-flex h-[18px] flex-none items-center rounded-full border px-2 text-[9.5px] font-semibold tracking-wide uppercase",
            brand.border,
            brand.text,
          )}
        >
          next
        </span>
      )}
    </div>
  );
}

function SkeletonCard() {
  return (
    <div
      className={cn(
        "flex h-[46px] items-center rounded-md border border-dashed px-3 text-[11.5px]",
        border.strong,
        surface.elevated,
        ink.muted,
      )}
    >
      Loading events…
    </div>
  );
}

export function Upcoming() {
  const [state, setState] = useState<LoadState>({ status: "loading" });

  useEffect(() => {
    let cancelled = false;
    getCalendarEvents()
      .then((response) => {
        if (cancelled) return;
        setState({
          status: "loaded",
          items: response.items,
          storageUnavailable: response.storage_unavailable,
        });
      })
      .catch((error: unknown) => {
        if (cancelled) return;
        setState({
          status: "error",
          message: error instanceof Error ? error.message : String(error),
        });
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <Panel className="flex flex-col gap-3">
      <h2 className="text-sm font-semibold">Upcoming</h2>

      {state.status === "loading" && (
        <div className="flex flex-col gap-2">
          <SkeletonCard />
          <SkeletonCard />
        </div>
      )}

      {state.status === "error" && (
        <p className={ink.muted}>Unavailable — couldn't load calendar.</p>
      )}

      {state.status === "loaded" && state.storageUnavailable && (
        <p className={ink.muted}>Unavailable — storage offline.</p>
      )}

      {state.status === "loaded" && !state.storageUnavailable && state.items.length === 0 && (
        <p className={ink.muted}>No meetings in the next 7 days.</p>
      )}

      {state.status === "loaded" && !state.storageUnavailable && state.items.length > 0 && (
        <div className="flex flex-col gap-3">
          {BUCKET_ORDER.map((bucket) => {
            const inBucket = bucketEvents(state.items).filter((b) => b.bucket === bucket);
            if (inBucket.length === 0) return null;
            return (
              <div key={bucket} className="flex flex-col gap-2">
                <p className={cn("text-[10.5px] font-bold tracking-wide uppercase", ink.muted)}>
                  {bucket}
                </p>
                <div className="flex flex-col gap-2">
                  {inBucket.map((b) => (
                    <EventCard key={b.event.event_id} {...b} />
                  ))}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </Panel>
  );
}
