"""TK-27 — trigger arms, the shared predicate, and the ceiling seam (EP-9, Q-55).

Un-gated (AC1-AC4): fakes only, no Postgres. Exercised through ``Gate.pipeline``/
``Gate.select_items`` (``pipeline.py``) since the arms are orchestrated there, sharing
``trigger.is_surfacing_worthy`` and depending on ``trigger.CeilingProtocol`` (injected).

Gated (Q-46): ``CeilingLedger`` (``gate/ceiling.py``) against a REAL throwaway Postgres, behind
``WOMBAT_TEST_PG_DSN``. Spin one up with:

    docker run --rm -d -p 5521:5432 -e POSTGRES_PASSWORD=wombat postgres:16
    WOMBAT_TEST_PG_DSN=postgresql://postgres:wombat@localhost:5521/postgres
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import psycopg
import pytest

from wombat.domain.daily_ledger import DailyLedger, ensure_schema
from wombat.gate.ceiling import CeilingLedger
from wombat.gate.decay import LedgerReset
from wombat.gate.models import GateAction, GateItem, ItemKind, ScoredItem
from wombat.gate.pending_set import CapacityEviction, InMemoryPendingJournal, PendingSet
from wombat.gate.pipeline import Gate
from wombat.gate.trigger import CeilingHit, CeilingProtocol, is_surfacing_worthy
from wombat.rating.params import EventClass, RatingParams


class _NoOpRollover:
    """A ``DayRolloverProtocol`` double that never fires (TK-28, Q-73): these tests exercise
    the TK-27 arms only, not decay/rollover — ``decay_ttl_seconds=float("inf")`` pairs with
    this so hygiene is a structural no-op here."""

    def check(self) -> LedgerReset | None:
        return None


def _item(item_id: str, *, sender_class: str = "automated", **payload_extra: object) -> GateItem:
    payload: dict[str, object] = {"is_timed": False, "sender_class": sender_class}
    payload.update(payload_extra)
    return GateItem(item_id=item_id, item_kind=ItemKind.GENERIC, created_at=0.0, payload=payload)


@dataclass
class _FakeUserModel:
    rating_params: RatingParams
    event_class: EventClass = EventClass.CALENDAR_CONFLICT

    def resolve_event_class(self, item: GateItem) -> EventClass:
        return self.event_class

    async def ratings_for(self, item: GateItem) -> RatingParams:
        return self.rating_params


@dataclass
class _FakeCeiling:
    allowed: bool = True
    recorded: list[EventClass] = field(default_factory=list)

    def allow(self, event_class: EventClass) -> bool:
        return self.allowed

    def record(self, event_class: EventClass) -> None:
        self.recorded.append(event_class)


@dataclass
class _EventRecorder:
    events: list[object] = field(default_factory=list)

    def __call__(self, event: object) -> None:
        self.events.append(event)


# --- is_surfacing_worthy / CeilingProtocol: the shared seam themselves -----------------------


def test_is_surfacing_worthy_is_strictly_greater_than_threshold() -> None:
    below = ScoredItem(item_id="a", item_kind=ItemKind.GENERIC, urgency=0.5, load=0.0)
    at = ScoredItem(item_id="b", item_kind=ItemKind.GENERIC, urgency=0.75, load=0.0)
    above = ScoredItem(item_id="c", item_kind=ItemKind.GENERIC, urgency=0.9, load=0.0)

    assert is_surfacing_worthy(below, 0.75) is False
    assert is_surfacing_worthy(at, 0.75) is False  # strictly greater, not >=
    assert is_surfacing_worthy(above, 0.75) is True


def test_fake_ceiling_satisfies_the_runtime_checkable_protocol() -> None:
    assert isinstance(_FakeCeiling(), CeilingProtocol)


# --- AC1: worthy + ceiling allows -> SURFACE_IMMEDIATE, ceiling.record called, no model call --


async def test_ac1_worthy_item_under_ceiling_surfaces_immediate_and_records_ceiling() -> None:
    rating_params = RatingParams(urgency_base=0.5, urgency_gain=1.0, load_base=0.0, load_gain=0.0)
    ceiling = _FakeCeiling(allowed=True)
    user_model = _FakeUserModel(rating_params=rating_params, event_class=EventClass.GENERIC)
    gate = Gate(
        user_model=user_model,
        pending_set=PendingSet(journal=InMemoryPendingJournal(), max_pending=10),
        ceiling=ceiling,
        urgency_threshold=0.1,
        load_flush_threshold=10.0,
        flush_min_age_seconds=100.0,
        decay_ttl_seconds=float("inf"),
        day_rollover=_NoOpRollover(),
        clock=lambda: 1000.0,
    )

    decision = await gate.pipeline([_item("a", sender_class="vip")])

    assert decision.action is GateAction.SURFACE_IMMEDIATE
    assert [scored.item_id for scored in decision.items] == ["a"]
    assert ceiling.recorded == [EventClass.GENERIC]
    # There is no mouth/LLM seam anywhere in this module — nothing to assert beyond its absence.


# --- AC2: ceiling denies -> HOLD, a CeilingHit is emitted ------------------------------------


async def test_ac2_ceiling_denies_worthy_item_holds_and_emits_ceiling_hit() -> None:
    rating_params = RatingParams(urgency_base=0.5, urgency_gain=1.0, load_base=0.0, load_gain=0.0)
    ceiling = _FakeCeiling(allowed=False)
    recorder = _EventRecorder()
    gate = Gate(
        user_model=_FakeUserModel(rating_params=rating_params, event_class=EventClass.GENERIC),
        pending_set=PendingSet(journal=InMemoryPendingJournal(), max_pending=10),
        ceiling=ceiling,
        urgency_threshold=0.1,
        load_flush_threshold=10.0,
        flush_min_age_seconds=100.0,
        decay_ttl_seconds=float("inf"),
        day_rollover=_NoOpRollover(),
        clock=lambda: 1000.0,
        on_event=recorder,
    )

    decision = await gate.pipeline([_item("a", sender_class="vip")])

    assert decision.action is GateAction.HOLD
    assert decision.items == ()  # regardless of urgency_score, per AC2
    assert ceiling.recorded == []  # never booked — it was denied, not granted
    ceiling_hits = [e for e in recorder.events if isinstance(e, CeilingHit)]
    assert len(ceiling_hits) == 1
    assert ceiling_hits[0].item_id == "a"
    assert ceiling_hits[0].event_class is EventClass.GENERIC


# --- AC3: load over threshold + oldest pending old enough -> SURFACE_FLUSH, urgency desc,
#          pending_set.clear() called (a durable journaled clear) --------------------------


async def test_ac3_flush_arm_fires_all_pending_urgency_desc_and_clears() -> None:
    rating_params = RatingParams(urgency_base=0.0, urgency_gain=1.0, load_base=1.0, load_gain=0.0)
    pending_set = PendingSet(journal=InMemoryPendingJournal(), max_pending=10)
    clock_time = [1000.0]
    gate = Gate(
        user_model=_FakeUserModel(rating_params=rating_params, event_class=EventClass.GENERIC),
        pending_set=pending_set,
        ceiling=_FakeCeiling(allowed=True),
        urgency_threshold=0.5,  # both items below this -> neither is "worthy" (no immediate)
        load_flush_threshold=0.5,
        flush_min_age_seconds=100.0,
        decay_ttl_seconds=float("inf"),
        day_rollover=_NoOpRollover(),
        clock=lambda: clock_time[0],
    )

    # Two items accumulate as HOLD (not worthy): "vip" (u=0.45) then "automated" (u=0.045).
    hold_1 = await gate.pipeline([_item("low_urgency", sender_class="automated")])
    hold_2 = await gate.pipeline([_item("high_urgency", sender_class="vip")])
    assert hold_1.action is GateAction.HOLD
    assert hold_2.action is GateAction.HOLD
    assert len(pending_set) == 2

    # Not enough time has passed yet -> still HOLD (min-age guard not satisfied).
    still_held = await gate.pipeline([])
    assert still_held.action is GateAction.HOLD
    assert len(pending_set) == 2

    # Advance the clock past flush_min_age_seconds -> the heartbeat tick fires the flush.
    clock_time[0] = 1200.0
    decision = await gate.pipeline([])

    assert decision.action is GateAction.SURFACE_FLUSH
    assert [scored.item_id for scored in decision.items] == ["high_urgency", "low_urgency"]
    assert [scored.urgency for scored in decision.items] == sorted(
        (scored.urgency for scored in decision.items), reverse=True
    )
    assert len(pending_set) == 0  # the durable journaled clear ran


# --- AC4: select_items is threshold-free (Q-30 shares is_surfacing_worthy only) and touches
#          NOTHING — pending contents + cumulative_load identical before/after -----------------


async def test_ac4_select_items_returns_worthy_sorted_and_preserves_pending_untouched() -> None:
    rating_params = RatingParams(urgency_base=0.0, urgency_gain=1.0, load_base=0.3, load_gain=0.0)
    pending_set = PendingSet(journal=InMemoryPendingJournal(), max_pending=10)
    ceiling = _FakeCeiling(allowed=True)
    gate = Gate(
        user_model=_FakeUserModel(rating_params=rating_params, event_class=EventClass.GENERIC),
        pending_set=pending_set,
        ceiling=ceiling,
        urgency_threshold=0.2,
        load_flush_threshold=10.0,
        flush_min_age_seconds=100.0,
        decay_ttl_seconds=float("inf"),
        day_rollover=_NoOpRollover(),
        clock=lambda: 1000.0,
    )
    # Seed the LIVE pending set with an item unrelated to the brief-items list below.
    already_pending = ScoredItem(
        item_id="already-pending", item_kind=ItemKind.GENERIC, urgency=0.3, load=0.1
    )
    pending_set.add(already_pending, added_at=1000.0)
    before_contents = pending_set.list()
    before_load = pending_set.cumulative_load()

    # Brief-specific candidates, NOT in the live pending set (Q-30).
    candidates = [
        _item("weak", sender_class="automated"),  # u=0.045 -> below 0.2, not worthy
        _item("strong", sender_class="vip"),  # u=0.45 -> worthy
        _item("medium", sender_class="known_human"),  # u=0.315 -> worthy
    ]

    selected = await gate.select_items(candidates)

    assert [scored.item_id for scored in selected] == ["strong", "medium"]
    assert [scored.urgency for scored in selected] == sorted(
        (scored.urgency for scored in selected), reverse=True
    )
    # The live pending set is left UNCHANGED — not accumulated, not cleared.
    assert pending_set.list() == before_contents
    assert pending_set.cumulative_load() == before_load
    assert ceiling.recorded == []  # no ceiling read/record


# --- CapacityEviction still routes through on_event when a held add() evicts -----------------


async def test_capacity_eviction_from_a_held_add_routes_through_on_event() -> None:
    rating_params = RatingParams(urgency_base=0.0, urgency_gain=1.0, load_base=0.0, load_gain=0.0)
    pending_set = PendingSet(journal=InMemoryPendingJournal(), max_pending=1)
    recorder = _EventRecorder()
    gate = Gate(
        user_model=_FakeUserModel(rating_params=rating_params, event_class=EventClass.GENERIC),
        pending_set=pending_set,
        ceiling=_FakeCeiling(allowed=True),
        urgency_threshold=0.9,  # nothing here is worthy -> both items fall through to add()
        load_flush_threshold=10.0,
        flush_min_age_seconds=100.0,
        decay_ttl_seconds=float("inf"),
        day_rollover=_NoOpRollover(),
        clock=lambda: 1000.0,
        on_event=recorder,
    )

    await gate.pipeline([_item("first", sender_class="automated")])
    await gate.pipeline([_item("second", sender_class="known_human")])  # evicts "first" at cap=1

    evictions = [e for e in recorder.events if isinstance(e, CapacityEviction)]
    assert len(evictions) == 1
    assert evictions[0].item_id == "first"


# ================================================================================================
# GATED: CeilingLedger against a REAL throwaway Postgres (Q-46)
# ================================================================================================

_DSN = os.environ.get("WOMBAT_TEST_PG_DSN")

_requires_pg = pytest.mark.skipif(
    not _DSN,
    reason=(
        "WOMBAT_TEST_PG_DSN is not set — skipping CeilingLedger DB tests that require a real "
        "throwaway Postgres. Start one with:\n"
        "  docker run --rm -d -p 5521:5432 -e POSTGRES_PASSWORD=wombat postgres:16\n"
        "then export WOMBAT_TEST_PG_DSN=postgresql://postgres:wombat@localhost:5521/postgres"
    ),
)


@pytest.fixture
def clean_table() -> None:
    assert _DSN is not None
    with psycopg.connect(_DSN) as conn:
        ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE daily_ledger")
        conn.commit()


@_requires_pg
def test_ceiling_ledger_allows_below_ceiling_and_denies_at_ceiling(clean_table: None) -> None:
    assert _DSN is not None
    tz = ZoneInfo("UTC")
    fixed_instant = datetime(2026, 7, 2, 12, 0, tzinfo=UTC)
    daily_ledger = DailyLedger(_DSN, tz=tz, clock=lambda: fixed_instant)
    try:
        ceiling = CeilingLedger(daily_ledger=daily_ledger, per_class_daily_ceiling=2)
        event_class = EventClass.CALENDAR_CONFLICT

        assert ceiling.allow(event_class) is True  # 0 < 2

        ceiling.record(event_class)
        assert ceiling.allow(event_class) is True  # 1 < 2

        ceiling.record(event_class)
        assert ceiling.allow(event_class) is False  # 2 < 2 is False -- at ceiling
    finally:
        daily_ledger.close()


@_requires_pg
def test_ceiling_ledger_record_increments_the_underlying_daily_ledger_row(
    clean_table: None,
) -> None:
    assert _DSN is not None
    tz = ZoneInfo("UTC")
    fixed_instant = datetime(2026, 7, 2, 12, 0, tzinfo=UTC)
    daily_ledger = DailyLedger(_DSN, tz=tz, clock=lambda: fixed_instant)
    try:
        ceiling = CeilingLedger(daily_ledger=daily_ledger, per_class_daily_ceiling=5)
        event_class = EventClass.MORNING_BRIEF

        ceiling.record(event_class)
        ceiling.record(event_class)
        row = daily_ledger.current_row(f"ceiling:{event_class.value}")

        assert row.value == 2
    finally:
        daily_ledger.close()


@_requires_pg
def test_ceiling_ledger_is_keyed_independently_per_event_class(clean_table: None) -> None:
    assert _DSN is not None
    tz = ZoneInfo("UTC")
    fixed_instant = datetime(2026, 7, 2, 12, 0, tzinfo=UTC)
    daily_ledger = DailyLedger(_DSN, tz=tz, clock=lambda: fixed_instant)
    try:
        ceiling = CeilingLedger(daily_ledger=daily_ledger, per_class_daily_ceiling=1)

        ceiling.record(EventClass.CALENDAR_CONFLICT)

        # A DIFFERENT event class's ceiling is untouched (DEC-13 personalization key).
        assert ceiling.allow(EventClass.CALENDAR_CONFLICT) is False
        assert ceiling.allow(EventClass.DRAFT_REPLY) is True
    finally:
        daily_ledger.close()
