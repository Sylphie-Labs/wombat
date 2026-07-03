"""wombat.calendar.alternatives — suggest_alternatives over real CalendarEvents (TK-74, Q-62).

Another projection ADAPTER: it reprojects the day (or, when that day is full, the following
civil-local day) implicated by a ``DailyConflict`` into the frozen TK-73 kernel's
``CalendarEventItem`` domain and REUSES ``slots.propose_alternatives`` for the actual
earliest-gap algorithm (Q-43 — never reimplemented here). All-day events never enter the
projection (Q-62 ruling 2), so they never occupy a candidate slot.

Pure: no I/O, no clock, no Google API. ``tz`` and ``working_hours`` are both injected.
"""

from __future__ import annotations

from datetime import timedelta
from zoneinfo import ZoneInfo

from wombat.calendar.conflict import DailyConflict, project_to_day_items
from wombat.calendar.models import (
    AlternativeSlot,
    CalendarEvent,
    CalendarEventItem,
    Conflict,
    WorkingHours,
)
from wombat.calendar.slots import propose_alternatives as propose_minute_alternatives


def suggest_alternatives(
    conflict: DailyConflict,
    events: list[CalendarEvent],
    working_hours: WorkingHours,
    tz: ZoneInfo,
    *,
    granularity: int = 15,
    max_candidates: int = 5,
) -> list[AlternativeSlot]:
    """Propose ranked alternative slots for ``conflict``'s movable event (earliest gap first).

    Tries ``conflict.day`` first, delegating straight to the frozen kernel's
    ``propose_alternatives`` over that day's projected events. If that day has no capacity
    (an empty result), falls back to the following civil-local day, carrying over the movable
    event's original duration (AC2: "same day, or the next working day if none").
    """
    buckets = project_to_day_items(events, tz)

    same_day_items = buckets.get(conflict.day, [])
    slots = propose_minute_alternatives(
        conflict.conflict,
        same_day_items,
        working_hours,
        granularity=granularity,
        max_candidates=max_candidates,
    )
    if slots:
        return slots

    next_day = conflict.day + timedelta(days=1)
    next_day_items = buckets.get(next_day, [])
    movable = conflict.conflict.movable
    duration = movable.end - movable.start
    # Only the event_id (exclude_id) and duration matter to propose_alternatives -- the
    # start/end below are a throwaway anchor at the working day's start, never surfaced.
    next_day_movable = CalendarEventItem(
        event_id=movable.event_id,
        title=movable.title,
        start=working_hours.start,
        end=working_hours.start + duration,
    )
    next_day_conflict = Conflict(incumbent=conflict.conflict.incumbent, movable=next_day_movable)
    return propose_minute_alternatives(
        next_day_conflict,
        next_day_items,
        working_hours,
        granularity=granularity,
        max_candidates=max_candidates,
    )


__all__ = ["suggest_alternatives"]
