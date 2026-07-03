"""Calendar data model for the conflict-with-alternatives spike (TK-73, EP-16).

Minimal, model-free types. Times are wall-clock *minutes since local midnight*
for a single day so the spike stays free of timezone/I/O concerns (NG-4). All
types are frozen dataclasses matching the existing ``src/wombat/gate`` style.

``CalendarEventItem`` is the minimal seed of the type TK-72 will own; the spike
needs only ``event_id``, the [start, end) interval, and a human-readable title.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

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


# --------------------------------------------------------------------------------------- TK-72

# ``CalendarEvent`` (Q-60): a NEW SIBLING domain type, not a promotion of ``CalendarEventItem``
# above. The two model DIFFERENT domains — ``CalendarEventItem`` is the day-projection the
# proven slot math (``slots.py``) lives in; ``CalendarEvent`` is the absolute-time wire fact a
# real Google Calendar event carries (an instant can span arbitrary days, unlike the spike's
# 0..1440 minutes-since-midnight). The spike types above stay untouched (frozen, Q-43).
#
# ``CalendarPoller`` (TK-72) is the only producer; ``to_payload``/``from_payload`` are the
# JSON-native wire helpers (Q-49) a ``SourceEvent.payload`` round-trips through — datetimes
# serialize as ISO-8601 strings. TK-74 (conflict detection) and TK-98 (gather) are future
# consumers of this type, not this ticket's concern.


@dataclass(frozen=True, slots=True)
class CalendarEvent:
    """One real Google Calendar event, as an absolute-time wire fact.

    ``start``/``end`` are timezone-AWARE instants (normalized to UTC by the producer —
    ``CalendarPoller``); ``all_day`` distinguishes a date-only Google event (midnight-to-midnight
    in the configured wombat timezone, normalized to UTC) from a timed one. ``title`` is Google's
    ``summary`` field, or ``""`` if absent.
    """

    event_id: str
    title: str
    start: datetime
    end: datetime
    all_day: bool

    def __post_init__(self) -> None:
        if self.start.tzinfo is None:
            raise ValueError(f"CalendarEvent {self.event_id!r}: start is naive (must be aware)")
        if self.end.tzinfo is None:
            raise ValueError(f"CalendarEvent {self.event_id!r}: end is naive (must be aware)")
        if not self.start < self.end:
            msg = (
                f"CalendarEvent {self.event_id!r}: require start < end, "
                f"got start={self.start.isoformat()}, end={self.end.isoformat()}"
            )
            raise ValueError(msg)

    def to_payload(self) -> dict[str, Any]:
        """JSON-native wire form (Q-49): datetimes as ISO-8601 strings. The one shape
        ``SourceEvent.payload`` carries and ``from_payload`` round-trips exactly."""
        return {
            "event_id": self.event_id,
            "title": self.title,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "all_day": self.all_day,
        }

    @staticmethod
    def from_payload(d: dict[str, Any]) -> CalendarEvent:
        """Inverse of ``to_payload`` — exact round-trip: ``from_payload(e.to_payload()) == e``."""
        return CalendarEvent(
            event_id=d["event_id"],
            title=d["title"],
            start=datetime.fromisoformat(d["start"]),
            end=datetime.fromisoformat(d["end"]),
            all_day=d["all_day"],
        )
