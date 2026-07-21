"""TK-27 — Gate rebuilt as the async orchestrator (EP-9, Q-55 convergence).

Ports TK-21's skeleton tests to the new async construction: ``Gate`` is now built from an
injected ``user_model`` + durable ``pending_set`` + ``ceiling`` (the TK-21 in-memory pending
dict is gone). The trigger-arm acceptance criteria themselves (AC1-AC4) live in
``test_trigger.py``; this file covers the general async pipeline shape, the
``surfacing_permitted=False`` suppression, and the held-then-pending path.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from wombat.domain.daily_ledger import DailyLedger
from wombat.gate.ceiling import FlushDayLatch
from wombat.gate.decay import LedgerReset
from wombat.gate.models import GateAction, GateDecision, GateItem, ItemKind
from wombat.gate.pending_set import InMemoryPendingJournal, PendingSet
from wombat.gate.pipeline import Gate
from wombat.rating.params import EventClass, RatingParams


class _NoOpRollover:
    """A ``DayRolloverProtocol`` double that never fires (TK-28, Q-73) — this module covers the
    general async pipeline shape, not decay/rollover."""

    def check(self) -> LedgerReset | None:
        return None


def _item(item_id: str, *, sender_class: str = "automated", **payload_extra: object) -> GateItem:
    payload: dict[str, object] = {"is_timed": False, "sender_class": sender_class}
    payload.update(payload_extra)
    return GateItem(item_id=item_id, item_kind=ItemKind.GENERIC, created_at=0.0, payload=payload)


@dataclass
class _FakeUserModel:
    """Returns a fixed RatingParams + EventClass regardless of the item (tests control the
    resulting score entirely through the item's payload, exercised by the REAL scoring fns)."""

    rating_params: RatingParams
    event_class: EventClass = EventClass.GENERIC

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
class _FakeFlushLatch:
    """A ``FlushLatchProtocol`` double (TK-287) mirroring ``FlushDayLatch``'s once-per-day
    shape: ``record()`` closes the latch for the rest of "today" (``allowed`` flips ``False``),
    exactly like a real once-per-wombat-day ledger row. A test simulates the next wombat day by
    setting ``.allowed = True`` directly (there is no in-fake day boundary to cross)."""

    allowed: bool = True
    recorded: int = 0
    denied_count: int = 0

    def allow(self) -> bool:
        return self.allowed

    def record(self) -> None:
        self.recorded += 1
        self.allowed = False

    def note_denied(self) -> None:
        self.denied_count += 1


def _noop_on_event(event: object) -> None:
    pass


def _gate(
    *,
    rating_params: RatingParams,
    ceiling: _FakeCeiling | None = None,
    urgency_threshold: float = 0.5,
    load_flush_threshold: float = 10.0,
    flush_min_age_seconds: float = 100.0,
    clock: Callable[[], float] = lambda: 1000.0,
    pending_set: PendingSet | None = None,
    on_event: Callable[[object], None] = _noop_on_event,
    flush_latch: _FakeFlushLatch | None = None,
) -> Gate:
    if pending_set is None:
        pending_set = PendingSet(journal=InMemoryPendingJournal(), max_pending=50)
    return Gate(
        user_model=_FakeUserModel(rating_params=rating_params),
        pending_set=pending_set,
        ceiling=ceiling or _FakeCeiling(),
        urgency_threshold=urgency_threshold,
        load_flush_threshold=load_flush_threshold,
        flush_min_age_seconds=flush_min_age_seconds,
        decay_ttl_seconds=float("inf"),
        day_rollover=_NoOpRollover(),
        clock=clock,
        on_event=on_event,
        flush_latch=flush_latch or _FakeFlushLatch(),
    )


# --- Ported TK-21 skeleton behavior, to the new async construction --------------------------


async def test_pipeline_holds_when_no_thresholds_crossed() -> None:
    # Not worthy (urgency stays low) and load stays under the flush threshold -> HOLD.
    rating_params = RatingParams(urgency_base=0.0, urgency_gain=0.1, load_base=0.0, load_gain=0.1)
    gate = _gate(rating_params=rating_params)

    decision = await gate.pipeline([_item("a")])

    assert isinstance(decision, GateDecision)
    assert decision.action is GateAction.HOLD
    assert decision.items == ()


async def test_pipeline_returns_surface_immediate_for_a_worthy_item_under_ceiling() -> None:
    rating_params = RatingParams(urgency_base=0.5, urgency_gain=1.0, load_base=0.0, load_gain=0.0)
    gate = _gate(rating_params=rating_params, urgency_threshold=0.1)

    decision = await gate.pipeline([_item("a", sender_class="vip")])

    assert decision.action is GateAction.SURFACE_IMMEDIATE
    assert [scored.item_id for scored in decision.items] == ["a"]


# --- Held -> pending path ---------------------------------------------------------------------


async def test_held_item_accumulates_into_the_injected_pending_set() -> None:
    rating_params = RatingParams(urgency_base=0.0, urgency_gain=0.0, load_base=0.1, load_gain=0.0)
    pending_set = PendingSet(journal=InMemoryPendingJournal(), max_pending=50)
    gate = _gate(rating_params=rating_params, pending_set=pending_set, load_flush_threshold=10.0)

    decision = await gate.pipeline([_item("a")])

    assert decision.action is GateAction.HOLD
    assert {scored.item_id for scored in pending_set.list()} == {"a"}


async def test_held_chat_item_never_absorbs_into_the_pending_set_and_carries_its_score() -> None:
    """DEC-57/TK-272 (R1): a CHAT item that would otherwise HOLD never reaches
    ``pending_set.add`` — it returns HOLD immediately, carrying the REAL scored item."""
    rating_params = RatingParams(urgency_base=0.0, urgency_gain=0.0, load_base=0.1, load_gain=0.0)
    pending_set = PendingSet(journal=InMemoryPendingJournal(), max_pending=50)
    gate = _gate(rating_params=rating_params, pending_set=pending_set, load_flush_threshold=10.0)
    chat_item = GateItem(
        item_id="chat-1",
        item_kind=ItemKind.CHAT,
        created_at=0.0,
        payload={"is_timed": False, "sender_class": "automated"},
    )

    decision = await gate.pipeline([chat_item])

    assert decision.action is GateAction.HOLD
    assert [scored.item_id for scored in decision.items] == ["chat-1"]
    assert decision.items[0].item_kind is ItemKind.CHAT
    assert pending_set.list() == []  # never absorbed


async def test_pipeline_of_empty_items_is_a_valid_heartbeat_tick() -> None:
    rating_params = RatingParams(urgency_base=0.0, urgency_gain=0.0, load_base=0.0, load_gain=0.0)
    gate = _gate(rating_params=rating_params)

    decision = await gate.pipeline([])

    assert decision.action is GateAction.HOLD
    assert decision.items == ()


# --- surfacing_permitted=False suppresses BOTH arms but items still accumulate --------------


async def test_surfacing_permitted_false_suppresses_the_immediate_arm() -> None:
    # This item WOULD be worthy + under ceiling if permitted — assert it is suppressed instead.
    rating_params = RatingParams(urgency_base=0.9, urgency_gain=0.0, load_base=0.0, load_gain=0.0)
    ceiling = _FakeCeiling(allowed=True)
    pending_set = PendingSet(journal=InMemoryPendingJournal(), max_pending=50)
    gate = _gate(
        rating_params=rating_params,
        ceiling=ceiling,
        urgency_threshold=0.1,
        pending_set=pending_set,
    )

    decision = await gate.pipeline([_item("a")], surfacing_permitted=False)

    assert decision.action is GateAction.HOLD
    assert ceiling.recorded == []  # the immediate arm never even consulted the ceiling
    # The item still scored + accumulated (presence suppresses surfacing, never accumulation).
    assert {scored.item_id for scored in pending_set.list()} == {"a"}


async def test_surfacing_permitted_false_suppresses_the_flush_arm() -> None:
    rating_params = RatingParams(urgency_base=0.0, urgency_gain=0.0, load_base=1.0, load_gain=0.0)
    pending_set = PendingSet(journal=InMemoryPendingJournal(), max_pending=50)
    clock_time = [1000.0]
    gate = _gate(
        rating_params=rating_params,
        pending_set=pending_set,
        load_flush_threshold=0.5,
        flush_min_age_seconds=10.0,
        clock=lambda: clock_time[0],
    )

    await gate.pipeline([_item("a")], surfacing_permitted=False)
    clock_time[0] = 2000.0  # well past flush_min_age_seconds
    decision = await gate.pipeline([], surfacing_permitted=False)

    assert decision.action is GateAction.HOLD
    assert len(pending_set) == 1  # flush never fired, nothing cleared


# --- TK-287 (DEC-63b): the once-per-wombat-day FlushDayLatch on the flush arm ----------------


async def test_ac1_flush_latch_denies_a_second_flush_the_same_wombat_day() -> None:
    """AC1: flush fired once this day -> the next over-threshold pipeline() run HOLDs and the
    pending set is NOT cleared."""
    rating_params = RatingParams(urgency_base=0.0, urgency_gain=0.0, load_base=1.0, load_gain=0.0)
    pending_set = PendingSet(journal=InMemoryPendingJournal(), max_pending=50)
    clock_time = [1000.0]
    flush_latch = _FakeFlushLatch()
    gate = _gate(
        rating_params=rating_params,
        pending_set=pending_set,
        load_flush_threshold=0.5,
        flush_min_age_seconds=10.0,
        clock=lambda: clock_time[0],
        flush_latch=flush_latch,
    )

    # First flush: the item ages past min-age and crosses the load threshold -> fires.
    await gate.pipeline([_item("a")])
    clock_time[0] = 2000.0
    first_decision = await gate.pipeline([])
    assert first_decision.action is GateAction.SURFACE_FLUSH
    assert flush_latch.recorded == 1
    assert len(pending_set) == 0

    # A new item accumulates and crosses the threshold again...
    clock_time[0] = 2100.0
    await gate.pipeline([_item("b")])
    clock_time[0] = 3000.0
    second_decision = await gate.pipeline([])

    # ...but the latch is closed for today -> HOLD, pending set untouched (not cleared).
    assert second_decision.action is GateAction.HOLD
    assert flush_latch.recorded == 1  # no second record()
    assert {scored.item_id for scored in pending_set.list()} == {"b"}


async def test_ac2_flush_latch_fires_again_on_the_next_wombat_day() -> None:
    """AC2: once the latch reopens (a new wombat day), the flush arm fires again exactly once."""
    rating_params = RatingParams(urgency_base=0.0, urgency_gain=0.0, load_base=1.0, load_gain=0.0)
    pending_set = PendingSet(journal=InMemoryPendingJournal(), max_pending=50)
    clock_time = [1000.0]
    flush_latch = _FakeFlushLatch()
    gate = _gate(
        rating_params=rating_params,
        pending_set=pending_set,
        load_flush_threshold=0.5,
        flush_min_age_seconds=10.0,
        clock=lambda: clock_time[0],
        flush_latch=flush_latch,
    )

    await gate.pipeline([_item("a")])
    clock_time[0] = 2000.0
    assert (await gate.pipeline([])).action is GateAction.SURFACE_FLUSH

    clock_time[0] = 2100.0
    await gate.pipeline([_item("b")])
    clock_time[0] = 3000.0
    assert (await gate.pipeline([])).action is GateAction.HOLD  # still today, latch closed

    # Simulate the wombat-day rollover reopening the latch: the still-pending item flushes.
    flush_latch.allowed = True
    third_decision = await gate.pipeline([])

    assert third_decision.action is GateAction.SURFACE_FLUSH
    assert flush_latch.recorded == 2
    assert len(pending_set) == 0


async def test_ac3_closed_flush_latch_never_blocks_the_immediate_arm() -> None:
    """AC3: a closed flush_latch never blocks the immediate-surfacing arm — only the flush arm
    consults it (ceiling permitting, the item still surfaces via SURFACE_IMMEDIATE)."""
    rating_params = RatingParams(urgency_base=0.9, urgency_gain=0.0, load_base=0.0, load_gain=0.0)
    flush_latch = _FakeFlushLatch(allowed=False)
    gate = _gate(rating_params=rating_params, urgency_threshold=0.1, flush_latch=flush_latch)

    decision = await gate.pipeline([_item("a", sender_class="vip")])

    assert decision.action is GateAction.SURFACE_IMMEDIATE
    assert flush_latch.recorded == 0
    assert flush_latch.denied_count == 0  # the immediate arm never even consults the flush latch


async def test_ac3_select_items_touches_no_flush_latch_state() -> None:
    """AC3: ``select_items`` (Q-30) reads/records NOTHING on the flush latch."""
    rating_params = RatingParams(urgency_base=0.9, urgency_gain=0.0, load_base=0.0, load_gain=0.0)
    flush_latch = _FakeFlushLatch(allowed=False)
    gate = _gate(rating_params=rating_params, urgency_threshold=0.1, flush_latch=flush_latch)

    worthy = await gate.select_items([_item("a")])

    assert [scored.item_id for scored in worthy] == ["a"]
    assert flush_latch.recorded == 0
    assert flush_latch.denied_count == 0


def test_ac5_gate_construction_without_flush_latch_raises_type_error() -> None:
    """AC5 (Q-69 lesson): ``flush_latch`` has no default — omitting it is a TypeError, not a
    silently-dead optional."""
    with pytest.raises(TypeError):
        Gate(  # type: ignore[call-arg]
            user_model=_FakeUserModel(
                rating_params=RatingParams(
                    urgency_base=0.0, urgency_gain=0.0, load_base=0.0, load_gain=0.0
                )
            ),
            pending_set=PendingSet(journal=InMemoryPendingJournal(), max_pending=50),
            ceiling=_FakeCeiling(),
            urgency_threshold=0.5,
            load_flush_threshold=10.0,
            flush_min_age_seconds=100.0,
            decay_ttl_seconds=float("inf"),
            day_rollover=_NoOpRollover(),
            clock=lambda: 1000.0,
        )


def test_ac5_flush_day_latch_note_denied_logs_at_most_one_info_line_per_wombat_day(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC5: repeated denied-flush attempts within one wombat day log AT MOST ONE INFO line.

    Exercises the REAL ``FlushDayLatch.note_denied()`` directly (never ``allow()``/``record()``,
    which touch Postgres via ``DailyLedger.current_row()``/``increment()``) — ``note_denied()``
    only calls ``DailyLedger.today()``, a pure clock computation with no I/O, so this is a safe
    non-pg unit test of the dedup logic.
    """
    clock_instant = [datetime(2026, 7, 21, 12, 0, tzinfo=UTC)]
    daily_ledger = DailyLedger(
        "postgresql://unused/db", tz=ZoneInfo("UTC"), clock=lambda: clock_instant[0]
    )
    latch = FlushDayLatch(daily_ledger=daily_ledger)

    with caplog.at_level(logging.INFO):
        latch.note_denied()
        latch.note_denied()
        latch.note_denied()

    assert len([r for r in caplog.records if r.levelno == logging.INFO]) == 1

    # A new wombat day logs again (exactly one more line).
    clock_instant[0] = datetime(2026, 7, 22, 0, 5, tzinfo=UTC)
    caplog.clear()
    with caplog.at_level(logging.INFO):
        latch.note_denied()

    assert len([r for r in caplog.records if r.levelno == logging.INFO]) == 1
