"""Self-eval for the RISK-6 earliest-gap slot spike (TK-73).

These tests are the *automated* half of the spike's exit criteria. They assert:
  * every fixture scenario contains a detectable conflict,
  * the algorithm proposes >=3 candidate alternative slots per conflict, and
  * every proposed slot is VALID — inside working hours, the right duration, and
    non-overlapping with any busy block (the events kept after moving the movable).

The remaining exit criterion (">=80% of slots rated reasonable") is HUMAN-GATED:
it needs Jim's qualitative ratings and cannot be asserted here (see the finding
stub at tests/calendar/fixtures/FINDING_TK73.md).
"""

from __future__ import annotations

from itertools import pairwise
from pathlib import Path

import pytest

from wombat.calendar.fixture_loader import Fixture, Scenario, load_fixture
from wombat.calendar.models import AlternativeSlot, WorkingHours
from wombat.calendar.slots import (
    detect_conflicts,
    project_busy,
    propose_alternatives,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "conflicts.yaml"
MIN_CANDIDATES = 3


@pytest.fixture(scope="module")
def fixture() -> Fixture:
    return load_fixture(FIXTURE_PATH)


def _slot_is_valid(
    slot: AlternativeSlot,
    busy: list[tuple[int, int]],
    working_hours: WorkingHours,
) -> bool:
    if slot.start < working_hours.start or slot.end > working_hours.end:
        return False
    if slot.start >= slot.end:
        return False
    return all(not (slot.start < b_end and b_start < slot.end) for b_start, b_end in busy)


def test_fixture_has_at_least_five_scenarios(fixture: Fixture) -> None:
    """Contract requires >=5 distinct conflicting scenarios."""
    assert len(fixture.scenarios) >= 5


def test_every_scenario_has_a_conflict(fixture: Fixture) -> None:
    for scenario in fixture.scenarios:
        conflicts = detect_conflicts(list(scenario.events))
        assert conflicts, f"scenario {scenario.name!r} has no detectable conflict"


@pytest.mark.parametrize("scenario_index", range(6))
def test_proposes_at_least_three_valid_slots(
    fixture: Fixture, scenario_index: int
) -> None:
    """Self-eval: >=3 valid candidate slots per conflict for each fixture case."""
    scenario: Scenario = fixture.scenarios[scenario_index]
    events = list(scenario.events)
    conflicts = detect_conflicts(events)
    assert conflicts, f"{scenario.name!r}: expected a conflict"

    conflict = conflicts[0]
    slots = propose_alternatives(conflict, events, fixture.working_hours)

    assert len(slots) >= MIN_CANDIDATES, (
        f"{scenario.name!r}: expected >={MIN_CANDIDATES} slots, got {len(slots)}"
    )

    busy = project_busy(
        events, fixture.working_hours, exclude_id=conflict.movable.event_id
    )
    for slot in slots:
        assert _slot_is_valid(slot, busy, fixture.working_hours), (
            f"{scenario.name!r}: invalid slot {slot}"
        )
        assert slot.duration == conflict.movable.end - conflict.movable.start


def test_slots_are_ranked_earliest_first(fixture: Fixture) -> None:
    for scenario in fixture.scenarios:
        events = list(scenario.events)
        conflict = detect_conflicts(events)[0]
        slots = propose_alternatives(conflict, events, fixture.working_hours)
        starts = [s.start for s in slots]
        ranks = [s.rank for s in slots]
        assert ranks == list(range(len(slots)))
        assert starts == sorted(starts), f"{scenario.name!r}: slots not earliest-first"


def test_slots_never_overlap_each_other(fixture: Fixture) -> None:
    for scenario in fixture.scenarios:
        events = list(scenario.events)
        conflict = detect_conflicts(events)[0]
        slots = sorted(
            propose_alternatives(conflict, events, fixture.working_hours),
            key=lambda s: s.start,
        )
        for earlier, later in pairwise(slots):
            assert earlier.end <= later.start, (
                f"{scenario.name!r}: overlapping candidates {earlier} / {later}"
            )


def test_no_conflict_yields_no_conflicts() -> None:
    """Sanity: disjoint events produce zero conflicts (mirrors TK-74 AC3)."""
    from wombat.calendar.models import CalendarEventItem

    events = [
        CalendarEventItem("a", "A", 540, 600),
        CalendarEventItem("b", "B", 600, 660),
        CalendarEventItem("c", "C", 700, 760),
    ]
    assert detect_conflicts(events) == []
