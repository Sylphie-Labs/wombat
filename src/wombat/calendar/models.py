"""Calendar data model for the conflict-with-alternatives spike (TK-73, EP-16).

Minimal, model-free types. Times are wall-clock *minutes since local midnight*
for a single day so the spike stays free of timezone/I/O concerns (NG-4). All
types are frozen dataclasses matching the existing ``src/wombat/gate`` style.

``CalendarEventItem`` is the minimal seed of the type TK-72 will own; the spike
needs only ``event_id``, the [start, end) interval, and a human-readable title.
"""

from __future__ import annotations

from dataclasses import dataclass

MINUTES_PER_DAY = 24 * 60


@dataclass(frozen=True, slots=True)
class CalendarEventItem:
    """A single calendar event on one day, as a half-open [start, end) interval.

    ``start`` and ``end`` are minutes since local midnight (0..1440). The interval
    is half-open: an event ``[540, 600)`` occupies 09:00 up to but not including
    10:00, so a back-to-back event starting at 600 does NOT overlap it.
    """

    event_id: str
    title: str
    start: int  # minutes since local midnight, inclusive
    end: int  # minutes since local midnight, exclusive

    def __post_init__(self) -> None:
        if not (0 <= self.start < self.end <= MINUTES_PER_DAY):
            msg = (
                f"invalid interval for {self.event_id!r}: "
                f"require 0 <= start < end <= {MINUTES_PER_DAY}, "
                f"got start={self.start}, end={self.end}"
            )
            raise ValueError(msg)

    def overlaps(self, other: CalendarEventItem) -> bool:
        """True iff the two half-open intervals share any minute."""
        return self.start < other.end and other.start < self.end


@dataclass(frozen=True, slots=True)
class WorkingHours:
    """The bookable window of a day, as a half-open [start, end) interval in minutes."""

    start: int  # e.g. 540 == 09:00
    end: int  # e.g. 1080 == 18:00

    def __post_init__(self) -> None:
        if not (0 <= self.start < self.end <= MINUTES_PER_DAY):
            msg = (
                "invalid working hours: require "
                f"0 <= start < end <= {MINUTES_PER_DAY}, "
                f"got start={self.start}, end={self.end}"
            )
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class FreeInterval:
    """A maximal free (non-busy) stretch inside working hours, half-open in minutes."""

    start: int
    end: int

    @property
    def length(self) -> int:
        return self.end - self.start


@dataclass(frozen=True, slots=True)
class Conflict:
    """Two events that overlap. ``incumbent`` is the kept event, ``movable`` needs a new slot."""

    incumbent: CalendarEventItem
    movable: CalendarEventItem


@dataclass(frozen=True, slots=True)
class AlternativeSlot:
    """A proposed replacement slot for a movable event, half-open in minutes.

    ``rank`` is 0-based: 0 is the earliest-gap (most preferred) candidate.
    """

    start: int
    end: int
    rank: int

    @property
    def duration(self) -> int:
        return self.end - self.start
