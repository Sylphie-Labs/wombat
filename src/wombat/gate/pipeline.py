"""Gate pipeline — the ASYNC production orchestrator (TK-27, EP-9, Q-55 convergence).

Rebuilds TK-21's synchronous skeleton IN PLACE (Q-39 pattern) as the thin async composition
of the arms + ceiling + durable pending set. Pure and model-free (DEC-13, S9): scoring is
personalized via the injected ``user_model`` seam (TK-42/EP-10) and never a model call.

Composition (all values keyword-injected — NO inline literals; composition passes
``OperatingParams`` fields, tests pass literals directly):

* ``user_model``   — resolves an item's ``EventClass`` and awaits its personalized
  ``RatingParams`` (TK-42). A structural seam (``UserModelProtocol``), not a concrete import,
  so this module never depends on cog-worx's entity-KG plumbing.
* ``pending_set``  — the TK-25 durable, journaled ``PendingSet`` (REPLACES TK-21's in-memory
  ``dict`` entirely). Held items are custody-durable the instant ``add`` returns (write-ahead
  BEFORE any queue ack happens upstream), so a crash between add and ack just re-delivers and
  re-adds idempotently (Q-51/52).
* ``ceiling``      — the ``trigger.CeilingProtocol`` seam (``CeilingLedger`` in production,
  ``gate/ceiling.py``; a fake in arm unit tests).
* ``urgency_threshold`` / ``load_flush_threshold`` / ``flush_min_age_seconds`` — the TK-13
  ``OperatingParams`` values the two arms compare against.
* ``clock``        — injected epoch-seconds callable; no wall-clock read happens here.
* ``on_event``     — routes ``CeilingHit`` (trigger.py) and ``CapacityEviction``
  (pending_set.py) events out of the pipeline; defaults to a loud log so neither is ever
  silently swallowed. Tests inject a recorder.

Decay stays OUT of this ticket (TK-28) — there is no TTL/eviction-by-age logic here at all.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from typing import Protocol, runtime_checkable

from wombat.gate.models import GateAction, GateDecision, GateItem, ScoredItem
from wombat.gate.pending_set import PendingSet
from wombat.gate.scoring import cognitive_load, urgency
from wombat.gate.trigger import CeilingHit, CeilingProtocol, is_surfacing_worthy
from wombat.rating.params import EventClass, RatingParams

_log = logging.getLogger(__name__)

Clock = Callable[[], float]


@runtime_checkable
class UserModelProtocol(Protocol):
    """The structural shape ``Gate`` needs from a user-model read seam (TK-42).

    Deliberately a Protocol, not an import of the concrete ``wombat.user_model.UserModel`` —
    the gate stays decoupled from cog-worx's entity-KG plumbing; tests inject a plain fake.
    """

    def resolve_event_class(self, item: GateItem) -> EventClass: ...

    async def ratings_for(self, item: GateItem) -> RatingParams: ...


def _log_event_loudly(event: object) -> None:
    """Default ``on_event``: log every gate event loudly (never silently swallowed)."""
    _log.warning("gate event: %r", event)


class Gate:
    """The production async interruption gate (Q-55): arms + ceiling + durable pending set."""

    def __init__(
        self,
        *,
        user_model: UserModelProtocol,
        pending_set: PendingSet,
        ceiling: CeilingProtocol,
        urgency_threshold: float,
        load_flush_threshold: float,
        flush_min_age_seconds: float,
        clock: Clock,
        on_event: Callable[[object], None] = _log_event_loudly,
    ) -> None:
        self._user_model = user_model
        self._pending_set = pending_set
        self._ceiling = ceiling
        self._urgency_threshold = urgency_threshold
        self._load_flush_threshold = load_flush_threshold
        self._flush_min_age_seconds = flush_min_age_seconds
        self._clock = clock
        self._on_event = on_event

    async def _score(self, item: GateItem) -> tuple[EventClass, ScoredItem]:
        """Resolve the event class and score one item via the injected user-model seam."""
        event_class = self._user_model.resolve_event_class(item)
        rating_params = await self._user_model.ratings_for(item)
        scored = ScoredItem(
            item_id=item.item_id,
            item_kind=item.item_kind,
            urgency=urgency(item, rating_params),
            load=cognitive_load(item, rating_params),
        )
        return event_class, scored

    def _try_flush(self) -> GateDecision | None:
        """Evaluate the flush arm; ``None`` if it does not fire (caller then holds)."""
        if self._pending_set.cumulative_load() <= self._load_flush_threshold:
            return None
        oldest_added_at = self._pending_set.oldest_added_at()
        if oldest_added_at is None:
            return None
        if (self._clock() - oldest_added_at) < self._flush_min_age_seconds:
            return None

        flushed = tuple(
            sorted(self._pending_set.list(), key=lambda scored: scored.urgency, reverse=True)
        )
        self._pending_set.clear()  # TK-25's durable, journaled bulk drain-all
        return GateDecision(action=GateAction.SURFACE_FLUSH, items=flushed)

    async def pipeline(
        self, items: Iterable[GateItem], *, surfacing_permitted: bool = True
    ) -> GateDecision:
        """End-to-end pass: score, apply the two arms, then the flush arm. One decision/call.

        ``surfacing_permitted`` is computed by the CALLER (presence, Q-12/Q-54) — this method
        never imports or reads a presence signal. ``False`` suppresses BOTH arms (nothing
        surfaces this call) but items still score and accumulate into the pending set
        (presence suppresses surfacing, never accumulation).
        """
        for item in items:
            event_class, scored = await self._score(item)
            worthy = surfacing_permitted and is_surfacing_worthy(scored, self._urgency_threshold)

            if worthy and self._ceiling.allow(event_class):
                self._ceiling.record(event_class)
                return GateDecision(action=GateAction.SURFACE_IMMEDIATE, items=(scored,))

            if worthy:
                # Ceiling denies an otherwise-worthy item: emit, then fall through to hold.
                self._on_event(CeilingHit(item_id=scored.item_id, event_class=event_class))

            eviction = self._pending_set.add(scored, added_at=self._clock())
            if eviction is not None:
                self._on_event(eviction)

        if surfacing_permitted:
            flush_decision = self._try_flush()
            if flush_decision is not None:
                return flush_decision

        return GateDecision(action=GateAction.HOLD, items=())

    async def select_items(self, items: Iterable[GateItem]) -> list[ScoredItem]:
        """The Q-30 threshold-free, pending-set-PRESERVING selection seam (used by TK-99).

        Scores each item via the SAME awaited ``ratings_for`` seam, filters by the ONE shared
        ``is_surfacing_worthy`` predicate (no load/min-age guard, no ceiling, no presence),
        sorts urgency descending, and touches NOTHING: no pending mutation, no ceiling
        read/record, no events. The live pending set's contents and ``cumulative_load()`` are
        identical before and after any call to this method.
        """
        scored_items: list[ScoredItem] = []
        for item in items:
            _, scored = await self._score(item)
            scored_items.append(scored)

        worthy = [s for s in scored_items if is_surfacing_worthy(s, self._urgency_threshold)]
        worthy.sort(key=lambda scored: scored.urgency, reverse=True)
        return worthy


__all__ = ["Gate", "UserModelProtocol"]
