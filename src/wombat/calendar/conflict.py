"""wombat.calendar.conflict — ConflictDetector over real CalendarEvents (TK-74, EP-16, Q-62).

A projection ADAPTER, not a second overlap algorithm: it projects the Q-60 ``CalendarEvent``
wire type (absolute-time, aware-UTC) into the frozen TK-73 day-minutes kernel's
``CalendarEventItem`` (minutes-since-local-midnight, one civil-local day at a time) and REUSES
``slots.detect_conflicts`` for the actual overlap math (Q-43 — never reimplemented here).

Two events only "conflict" if they overlap on the same civil-local day in the injected ``tz``
(Q-62 ruling 3), so detection buckets the projected segments by day and runs the kernel
independently per bucket. All-day events are excluded entirely from projection (Q-62 ruling 2)
-- they are not busy time, so they never appear in a ``Conflict`` and never block a free
interval.

Pure: no I/O, no clock, no Google API. ``tz`` is always an injected ``ZoneInfo`` -- this module
never reads config or wall-clock time itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from wombat.calendar.models import (
    MINUTES_PER_DAY,
    CalendarEvent,
    CalendarEventItem,
    Conflict,
)
from wombat.calendar.slots import detect_conflicts as detect_minute_conflicts
from wombat.rating.params import EventClass


def _split_by_local_day(
    event: CalendarEvent, tz: ZoneInfo
) -> list[tuple[date, CalendarEventItem]]:
    """Split one timed ``CalendarEvent`` into per-civil-local-day segments (Q-62 ruling 1).

    A midnight-crossing event becomes ``[start_min, 1440)`` on its first day and
    ``[0, end_min)`` on the next; a multi-day event gets full-day ``[0, 1440)`` segments for
    every intermediate day. Zero-length segments (an event ending exactly at local midnight)
    are dropped -- the kernel type requires ``start < end``. Each segment carries the
    ORIGINAL event's ``event_id``/``title`` so a conflict can always be traced back to the
    real event, never a projected fragment.
    """
    local_start = event.start.astimezone(tz)
    local_end = event.end.astimezone(tz)
    start_day = local_start.date()
    end_day = local_end.date()

    segments: list[tuple[date, CalendarEventItem]] = []
    day = start_day
    while day <= end_day:
        seg_start = local_start.hour * 60 + local_start.minute if day == start_day else 0
        seg_end = (
            local_end.hour * 60 + local_end.minute if day == end_day else MINUTES_PER_DAY
        )
        if seg_start < seg_end:
            segments.append(
                (
                    day,
                    CalendarEventItem(
                        event_id=event.event_id, title=event.title, start=seg_start, end=seg_end
                    ),
                )
            )
        day += timedelta(days=1)
    return segments


def project_to_day_items(
    events: list[CalendarEvent], tz: ZoneInfo
) -> dict[date, list[CalendarEventItem]]:
    """Project timed ``CalendarEvent``s into per-civil-local-day ``CalendarEventItem`` buckets.

    All-day events are dropped entirely (Q-62 ruling 2) before projection -- a holiday is not
    busy time and must never appear in a bucket a caller then feeds to the kernel.
    """
    buckets: dict[date, list[CalendarEventItem]] = {}
    for event in events:
        if event.all_day:
            continue
        for day, item in _split_by_local_day(event, tz):
            buckets.setdefault(day, []).append(item)
    return buckets


@dataclass(frozen=True, slots=True)
class DailyConflict:
    """Two ORIGINAL ``CalendarEvent``s that overlap on one civil-local day.

    A minimal wrapper (Q-62 seams guidance) around the frozen spike ``Conflict`` -- whose
    ``CalendarEventItem``s are per-day PROJECTED segments, not the originals -- plus the day
    they collided on. The spike type stays untouched; this just adds the day dimension AC1
    needs without mutating ``models.py``.
    """

    day: date
    conflict: Conflict  # projected-segment pair (CalendarEventItem) for this civil-local day

    @property
    def incumbent_event_id(self) -> str:
        return self.conflict.incumbent.event_id

    @property
    def movable_event_id(self) -> str:
        return self.conflict.movable.event_id


def detect_conflicts(events: list[CalendarEvent], tz: ZoneInfo) -> list[DailyConflict]:
    """Detect scheduling conflicts among real ``CalendarEvent``s, per civil-local day.

    Projects ``events`` into day buckets (all-day events excluded), then reuses the frozen
    kernel's ``detect_conflicts`` independently within each bucket -- an overlap only counts if
    it lands on the same civil-local day in ``tz``. Pure: no I/O, no clock, no Google API.
    """
    buckets = project_to_day_items(events, tz)
    conflicts: list[DailyConflict] = []
    for day in sorted(buckets):
        for minute_conflict in detect_minute_conflicts(buckets[day]):
            conflicts.append(DailyConflict(day=day, conflict=minute_conflict))
    return conflicts


def conflict_to_payload(conflict: DailyConflict) -> dict[str, Any]:
    """JSON-native wire form (Q-49) for one ``DailyConflict``.

    Stamps ``event_class = EventClass.CALENDAR_CONFLICT.value`` (``"calendar_conflict"``,
    matching ``rating/params.py`` exactly) -- the single stamping site later tickets
    (TK-98/TK-99) reuse rather than re-deriving the spelling. TK-74 itself does not enqueue
    this payload or call this function in a pipeline; it just provides it.
    """
    return {
        "event_class": EventClass.CALENDAR_CONFLICT.value,
        "day": conflict.day.isoformat(),
        "incumbent_event_id": conflict.incumbent_event_id,
        "incumbent_title": conflict.conflict.incumbent.title,
        "movable_event_id": conflict.movable_event_id,
        "movable_title": conflict.conflict.movable.title,
    }


__all__ = [
    "DailyConflict",
    "conflict_to_payload",
    "detect_conflicts",
    "project_to_day_items",
]
