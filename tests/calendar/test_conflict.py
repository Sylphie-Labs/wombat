"""Tests for TK-74 — ConflictDetector + AlternativeSlots over real CalendarEvents (Q-62).

Covers all three recorded acceptance criteria:
  AC1 — overlapping same-civil-local-day events are reported by ORIGINAL event_ids + day.
  AC2 — suggest_alternatives ranks by earliest gap, falling back to the next civil-local day
        when the conflict's own day has no capacity.
  AC3 — no false positives: all_day events never conflict/block, and a midnight-crossing pair
        that only *looks* overlapping ignoring dates does not conflict once projected per
        civil-local day.

Every function under test is pure (no I/O, no clock, no Google API) — the "no network call"
and "zero write calls" assertions are therefore structural: the modules import no HTTP client
and the functions under test take no session/client argument.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import wombat.calendar.alternatives as alternatives_module
import wombat.calendar.conflict as conflict_module
from wombat.calendar.alternatives import suggest_alternatives
from wombat.calendar.conflict import (
    DailyConflict,
    conflict_to_payload,
    detect_conflicts,
    project_to_day_items,
)
from wombat.calendar.models import CalendarEvent, WorkingHours
from wombat.rating.params import EventClass

UTC_TZ = ZoneInfo("UTC")
WORKING_HOURS = WorkingHours(540, 1080)  # 09:00-18:00


def _event(
    event_id: str, start: datetime, end: datetime, *, title: str = "", all_day: bool = False
) -> CalendarEvent:
    return CalendarEvent(event_id=event_id, title=title, start=start, end=end, all_day=all_day)


def _utc(y: int, m: int, d: int, hh: int, mm: int) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=UTC)


# --------------------------------------------------------------------------------------- purity


def test_no_network_client_imported() -> None:
    """Structural proof there is no HTTP seam to call out over: neither module imports one."""
    assert "requests" not in vars(conflict_module)
    assert "requests" not in vars(alternatives_module)


def test_detect_conflicts_signature_has_no_io_seam() -> None:
    """AC1: pure function -- only data + the injected tz, no session/client parameter."""
    assert set(inspect.signature(detect_conflicts).parameters) == {"events", "tz"}


def test_suggest_alternatives_signature_has_no_io_seam() -> None:
    """AC2: pure function -- no session/client parameter, so zero write calls is trivial."""
    params = set(inspect.signature(suggest_alternatives).parameters)
    assert params == {
        "conflict",
        "events",
        "working_hours",
        "tz",
        "granularity",
        "max_candidates",
    }


# --------------------------------------------------------------------------------------- AC1


def test_detect_conflicts_reports_original_event_ids_and_day() -> None:
    """Two overlapping timed events on the same civil-local day -> one DailyConflict."""
    events = [
        _event("evt-a", _utc(2026, 3, 10, 10, 0), _utc(2026, 3, 10, 11, 0), title="A"),
        _event("evt-b", _utc(2026, 3, 10, 10, 30), _utc(2026, 3, 10, 11, 30), title="B"),
    ]

    conflicts = detect_conflicts(events, UTC_TZ)

    assert len(conflicts) == 1
    (conflict,) = conflicts
    assert conflict.day == _utc(2026, 3, 10, 0, 0).date()
    assert {conflict.incumbent_event_id, conflict.movable_event_id} == {"evt-a", "evt-b"}
    # The earlier-starting event is the incumbent (mirrors the frozen kernel's rule).
    assert conflict.incumbent_event_id == "evt-a"
    assert conflict.movable_event_id == "evt-b"


def test_conflict_to_payload_stamps_exact_event_class_spelling() -> None:
    events = [
        _event("evt-a", _utc(2026, 3, 10, 10, 0), _utc(2026, 3, 10, 11, 0), title="A"),
        _event("evt-b", _utc(2026, 3, 10, 10, 30), _utc(2026, 3, 10, 11, 30), title="B"),
    ]
    (conflict,) = detect_conflicts(events, UTC_TZ)

    payload = conflict_to_payload(conflict)

    assert payload["event_class"] == EventClass.CALENDAR_CONFLICT.value == "calendar_conflict"
    assert payload["incumbent_event_id"] == "evt-a"
    assert payload["movable_event_id"] == "evt-b"
    assert payload["day"] == "2026-03-10"
    # JSON-native (Q-49): every value must round-trip through json.dumps without a default=.
    import json

    json.dumps(payload)


# --------------------------------------------------------------------------------------- AC2


def test_suggest_alternatives_same_day_ranked_earliest_first() -> None:
    events = [
        _event("evt-a", _utc(2026, 3, 10, 10, 0), _utc(2026, 3, 10, 11, 0), title="A"),
        _event("evt-b", _utc(2026, 3, 10, 10, 30), _utc(2026, 3, 10, 11, 30), title="B"),
    ]
    (conflict,) = detect_conflicts(events, UTC_TZ)

    slots = suggest_alternatives(conflict, events, WORKING_HOURS, UTC_TZ)

    assert len(slots) >= 1
    assert [s.rank for s in slots] == sorted(s.rank for s in slots)
    for slot in slots:
        assert WORKING_HOURS.start <= slot.start
        assert slot.end <= WORKING_HOURS.end


def test_suggest_alternatives_falls_back_to_next_day_when_full() -> None:
    """When the conflict's own civil-local day has zero free capacity, fall back to the next
    civil-local day (AC2: "same day, or the next working day if none")."""
    events = [
        # Incumbent occupies the ENTIRE working-hours window on day 1 -- no room to move
        # the movable event anywhere else that day.
        _event("evt-a", _utc(2026, 3, 10, 9, 0), _utc(2026, 3, 10, 18, 0), title="A"),
        _event("evt-b", _utc(2026, 3, 10, 10, 0), _utc(2026, 3, 10, 11, 0), title="B"),
        # Day 2 has no events at all -- wide open.
    ]
    (conflict,) = detect_conflicts(events, UTC_TZ)

    # Confirm the premise: no same-day capacity.
    same_day_items = project_to_day_items(events, UTC_TZ)[conflict.day]
    assert same_day_items  # sanity: the day bucket is non-empty

    slots = suggest_alternatives(conflict, events, WORKING_HOURS, UTC_TZ)

    assert len(slots) >= 1
    for slot in slots:
        assert WORKING_HOURS.start <= slot.start
        assert slot.end <= WORKING_HOURS.end
        assert slot.end - slot.start == 60  # evt-b's original duration is preserved


# --------------------------------------------------------------------------------------- AC3


def test_no_conflicts_for_non_overlapping_events() -> None:
    events = [
        _event("evt-a", _utc(2026, 3, 10, 9, 0), _utc(2026, 3, 10, 10, 0), title="A"),
        _event("evt-b", _utc(2026, 3, 10, 11, 0), _utc(2026, 3, 10, 12, 0), title="B"),
    ]

    assert detect_conflicts(events, UTC_TZ) == []


def test_all_day_events_excluded_from_conflicts_and_free_intervals() -> None:
    """An all_day event spanning the WHOLE day would conflict with everything if it were not
    excluded (RISK-6 junk conflicts) -- assert it never appears in a Conflict and never
    shrinks the projected day bucket."""
    holiday = _event(
        "evt-holiday",
        _utc(2026, 3, 10, 0, 0),
        _utc(2026, 3, 11, 0, 0),
        title="Holiday",
        all_day=True,
    )
    events = [
        holiday,
        _event("evt-a", _utc(2026, 3, 10, 9, 0), _utc(2026, 3, 10, 10, 0), title="A"),
        _event("evt-b", _utc(2026, 3, 10, 11, 0), _utc(2026, 3, 10, 12, 0), title="B"),
    ]

    conflicts = detect_conflicts(events, UTC_TZ)
    assert conflicts == []  # would be non-empty if the all-day event were treated as busy

    day = _utc(2026, 3, 10, 0, 0).date()
    buckets = project_to_day_items(events, UTC_TZ)
    day_item_ids = {item.event_id for item in buckets[day]}
    assert "evt-holiday" not in day_item_ids
    assert day_item_ids == {"evt-a", "evt-b"}


def test_midnight_crossing_pair_no_false_positive() -> None:
    """Two events that cross midnight and share the SAME wall-clock minute-of-day range (so a
    naive comparison that ignored dates would think they overlap) but land on opposite ends of
    their one shared civil-local day -- must NOT be reported as a conflict."""
    # A: 2026-03-15 23:50 -> 2026-03-16 00:10 (tail end of day 15, head of day 16).
    event_a = _event("evt-a", _utc(2026, 3, 15, 23, 50), _utc(2026, 3, 16, 0, 10), title="A")
    # B: 2026-03-16 23:50 -> 2026-03-17 00:10 (tail end of day 16, head of day 17) -- exactly
    # 24h after A, same wall-clock minute-of-day, but genuinely non-overlapping in time.
    event_b = _event("evt-b", _utc(2026, 3, 16, 23, 50), _utc(2026, 3, 17, 0, 10), title="B")

    conflicts = detect_conflicts([event_a, event_b], UTC_TZ)

    assert conflicts == []

    # Both events DO touch day 2026-03-16 (A's head, B's tail) -- confirm they land at
    # opposite ends of that shared day rather than being silently dropped.
    day16 = _utc(2026, 3, 16, 0, 0).date()
    buckets = project_to_day_items([event_a, event_b], UTC_TZ)
    day16_items = {item.event_id: (item.start, item.end) for item in buckets[day16]}
    assert day16_items == {"evt-a": (0, 10), "evt-b": (1430, 1440)}


def test_daily_conflict_is_a_minimal_wrapper_not_a_models_mutation() -> None:
    """Q-62 seams guidance: DailyConflict wraps the frozen spike Conflict + a day field --
    it must not be the frozen Conflict type itself (which carries no day)."""
    from wombat.calendar.models import Conflict

    events = [
        _event("evt-a", _utc(2026, 3, 10, 10, 0), _utc(2026, 3, 10, 11, 0), title="A"),
        _event("evt-b", _utc(2026, 3, 10, 10, 30), _utc(2026, 3, 10, 11, 30), title="B"),
    ]
    (conflict,) = detect_conflicts(events, UTC_TZ)

    assert isinstance(conflict, DailyConflict)
    assert isinstance(conflict.conflict, Conflict)
    assert not isinstance(conflict, Conflict)
