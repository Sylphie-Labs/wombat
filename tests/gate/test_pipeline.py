"""TK-27 — Gate rebuilt as the async orchestrator (EP-9, Q-55 convergence).

Ports TK-21's skeleton tests to the new async construction: ``Gate`` is now built from an
injected ``user_model`` + durable ``pending_set`` + ``ceiling`` (the TK-21 in-memory pending
dict is gone). The trigger-arm acceptance criteria themselves (AC1-AC4) live in
``test_trigger.py``; this file covers the general async pipeline shape, the
``surfacing_permitted=False`` suppression, and the held-then-pending path.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

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
