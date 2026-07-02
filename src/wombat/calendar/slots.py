"""Earliest-gap alternative-slot algorithm for the conflict spike (TK-73, RISK-6).

Deterministic and model-free (NG-4). Pure functions only — no I/O, no clock, no
Google API. Given a day's events, the busy projection, and working hours, propose
a ranked list of alternative slots for a *movable* event displaced by a conflict.

Algorithm (naive earliest-gap):
  1. Project busy time = union of all events EXCEPT the movable one, clipped to
     working hours.
  2. Compute the free intervals (the complement of busy within working hours).
  3. Walk free intervals earliest-first; in each, emit DISTINCT, non-overlapping
     candidate slots of the movable event's duration, packed from the interval
     start and stepping by the slot duration (snapped to a grid). Earlier slots
     get a lower (better) rank.

This is intentionally simple: the spike exists to learn whether "earliest gap" is
good enough for a human before TK-74 hardens it.
"""

from __future__ import annotations

from .models import (
    AlternativeSlot,
    CalendarEventItem,
    Conflict,
    FreeInterval,
    WorkingHours,
)

DEFAULT_GRANULARITY = 15  # minutes; candidate starts snap to this grid


def detect_conflicts(events: list[CalendarEventItem]) -> list[Conflict]:
    """Return every overlapping pair as a Conflict.

    The earlier-starting event (ties broken by ``event_id``) is the incumbent;
    the later one is treated as movable. Pure: no ordering side effects on input.
    """
    ordered = sorted(events, key=lambda e: (e.start, e.event_id))
    conflicts: list[Conflict] = []
    for i, first in enumerate(ordered):
        for second in ordered[i + 1 :]:
            if first.overlaps(second):
                conflicts.append(Conflict(incumbent=first, movable=second))
    return conflicts


def project_busy(
    events: list[CalendarEventItem],
    working_hours: WorkingHours,
    *,
    exclude_id: str | None = None,
) -> list[tuple[int, int]]:
    """Merge events into disjoint busy intervals clipped to working hours.

    ``exclude_id`` drops the movable event so its own time counts as free. Returns
    sorted, non-overlapping ``(start, end)`` tuples.
    """
    clipped: list[tuple[int, int]] = []
    for ev in events:
        if exclude_id is not None and ev.event_id == exclude_id:
            continue
        start = max(ev.start, working_hours.start)
        end = min(ev.end, working_hours.end)
        if start < end:
            clipped.append((start, end))

    clipped.sort()
    merged: list[tuple[int, int]] = []
    for start, end in clipped:
        if merged and start <= merged[-1][1]:
            prev_start, prev_end = merged[-1]
            merged[-1] = (prev_start, max(prev_end, end))
        else:
            merged.append((start, end))
    return merged


def free_intervals(
    events: list[CalendarEventItem],
    working_hours: WorkingHours,
    *,
    exclude_id: str | None = None,
) -> list[FreeInterval]:
    """Complement of the busy projection within working hours, earliest-first."""
    busy = project_busy(events, working_hours, exclude_id=exclude_id)
    frees: list[FreeInterval] = []
    cursor = working_hours.start
    for start, end in busy:
        if start > cursor:
            frees.append(FreeInterval(cursor, start))
        cursor = max(cursor, end)
    if cursor < working_hours.end:
        frees.append(FreeInterval(cursor, working_hours.end))
    return frees


def propose_alternatives(
    conflict: Conflict,
    events: list[CalendarEventItem],
    working_hours: WorkingHours,
    *,
    granularity: int = DEFAULT_GRANULARITY,
    max_candidates: int = 5,
) -> list[AlternativeSlot]:
    """Propose ranked alternative slots for ``conflict.movable`` (earliest gap first).

    The movable event is excluded from the busy projection (it is the thing being
    moved). Candidate slots have the movable event's exact duration, are packed
    from each free interval's start on a ``granularity`` grid, and never overlap a
    busy block (they live wholly inside a free interval). Returns up to
    ``max_candidates`` slots, rank 0 = earliest.
    """
    if granularity <= 0:
        msg = f"granularity must be positive, got {granularity}"
        raise ValueError(msg)

    duration = conflict.movable.end - conflict.movable.start
    frees = free_intervals(events, working_hours, exclude_id=conflict.movable.event_id)

    # Step by the slot duration so proposed candidates are DISTINCT and never
    # overlap each other — a human wants genuinely different options, not a
    # sliding window of near-duplicate times. Starts still snap to the grid.
    step = max(granularity, duration)

    candidates: list[AlternativeSlot] = []
    for interval in frees:
        # First grid-aligned start at or after the interval start.
        first = interval.start
        if first % granularity != 0:
            first += granularity - (first % granularity)
        slot_start = first
        while slot_start + duration <= interval.end:
            candidates.append(
                AlternativeSlot(start=slot_start, end=slot_start + duration, rank=0)
            )
            if len(candidates) >= max_candidates:
                break
            slot_start += step
        if len(candidates) >= max_candidates:
            break

    # Re-rank earliest-first (frees are already earliest-first, so order holds).
    return [
        AlternativeSlot(start=c.start, end=c.end, rank=i)
        for i, c in enumerate(candidates)
    ]
