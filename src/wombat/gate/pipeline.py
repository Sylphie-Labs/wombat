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
* ``threshold_fn`` (TK-215, DEC-37(a)/Q-107(a)) — OPTIONAL keyword, default ``None``. When
  ``None`` (every existing caller/test), both worth checks compare against the constant
  ``urgency_threshold`` above — today's gate, byte-unaffected. When provided, it is a zero-arg
  callable evaluated PER ITEM inside the scoring loop (``pipeline()``) and per item in
  ``select_items()`` — never once per call — so a live proactivity change lands on the very
  next scored item, no restart. Composition wires a closure over ``LivePersona.matrix
  .proactivity`` and ``trigger.effective_urgency_threshold``; the one-predicate invariant (Q-55)
  stays intact because BOTH call sites resolve the SAME effective threshold for a given item.
* ``clock``        — injected epoch-seconds callable; no wall-clock read happens here.
* ``on_event``     — routes ``CeilingHit`` (trigger.py), ``CapacityEviction``
  (pending_set.py), ``DecayEvent`` (models.py) and ``LedgerReset`` (decay.py) events out of
  the pipeline; defaults to a loud log so none is ever silently swallowed. Tests inject a
  recorder.
* ``decay_ttl_seconds`` / ``day_rollover`` — TK-28 (Q-73): the stale-item decay ttl and the
  exactly-once wombat-day-boundary observation seam. Both are REQUIRED constructor kwargs,
  never optional-defaulted (the Q-69 orphaned-budget lesson: an optional decay would ship
  dead in the runnable path). Tests not exercising decay pass ``decay_ttl_seconds=float("inf")``
  plus a no-op ``day_rollover`` fake whose ``check()`` always returns ``None``.

TK-28 (Q-73) prepends two hygiene steps to every ``pipeline()`` call, in order:
``day_rollover.check()`` (routed via ``on_event``) then ``self.decay()`` — BEFORE the
byte-untouched TK-27 arms below. Both run regardless of ``surfacing_permitted`` and on empty
batches: presence suppresses surfacing, never hygiene. No new arm logic.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from typing import Protocol, runtime_checkable

from wombat.gate.decay import DayRolloverProtocol, decay_stale
from wombat.gate.models import DecayEvent, GateAction, GateDecision, GateItem, ItemKind, ScoredItem
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
        decay_ttl_seconds: float,
        day_rollover: DayRolloverProtocol,
        clock: Clock,
        on_event: Callable[[object], None] = _log_event_loudly,
        threshold_fn: Callable[[], float] | None = None,
    ) -> None:
        self._user_model = user_model
        self._pending_set = pending_set
        self._ceiling = ceiling
        self._urgency_threshold = urgency_threshold
        self._load_flush_threshold = load_flush_threshold
        self._flush_min_age_seconds = flush_min_age_seconds
        self._decay_ttl_seconds = decay_ttl_seconds
        self._day_rollover = day_rollover
        self._clock = clock
        self._on_event = on_event
        self._threshold_fn = threshold_fn

    def _current_urgency_threshold(self) -> float:
        """TK-215: the threshold this item scores against — the injected ``threshold_fn`` if
        wired (evaluated fresh, per item), else the constant ``urgency_threshold`` (today's
        gate, byte-unaffected when ``threshold_fn`` is ``None``)."""
        return self._threshold_fn() if self._threshold_fn is not None else self._urgency_threshold

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

    def decay(self) -> tuple[DecayEvent, ...]:
        """Public decay pass (TK-28): remove journaled-stale pending items via ``decay_stale``.

        Emits each ``DecayEvent`` through ``on_event`` AND returns them, so both ``pipeline()``'s
        internal wiring and an external caller observe the same events.
        """
        events = decay_stale(
            self._pending_set, now=self._clock(), decay_ttl_seconds=self._decay_ttl_seconds
        )
        for event in events:
            self._on_event(event)
        return events

    async def pipeline(
        self, items: Iterable[GateItem], *, surfacing_permitted: bool = True
    ) -> GateDecision:
        """End-to-end pass: two hygiene steps, then score + the two arms, then the flush arm.

        TK-28 (Q-73) prepends ``day_rollover.check()`` (routed via ``on_event``) then
        ``self.decay()`` — BOTH run regardless of ``surfacing_permitted`` and on empty batches
        (presence suppresses surfacing, never hygiene), before the byte-untouched TK-27 arms.

        ``surfacing_permitted`` is computed by the CALLER (presence, Q-12/Q-54) — this method
        never imports or reads a presence signal. ``False`` suppresses BOTH arms (nothing
        surfaces this call) but items still score and accumulate into the pending set
        (presence suppresses surfacing, never accumulation).
        """
        reset = self._day_rollover.check()
        if reset is not None:
            self._on_event(reset)
        self.decay()

        for item in items:
            event_class, scored = await self._score(item)
            worthy = surfacing_permitted and is_surfacing_worthy(
                scored, self._current_urgency_threshold()
            )

            if worthy and self._ceiling.allow(event_class):
                self._ceiling.record(event_class)
                return GateDecision(action=GateAction.SURFACE_IMMEDIATE, items=(scored,))

            if worthy:
                # Ceiling denies an otherwise-worthy item: emit, then fall through to hold.
                self._on_event(CeilingHit(item_id=scored.item_id, event_class=event_class))

            # DEC-57/TK-272: chat NEVER absorbs into the durable pending set. A held chat item
            # holds for VOICE purposes only (compose_dispatch still runs it text-only) — it must
            # carry its REAL score forward, so return immediately with the scored item attached
            # rather than adding it and returning the (score-discarding) empty-items HOLD below.
            # Q-51's batch_size=1 means at most one item reaches this point per pipeline() call.
            if scored.item_kind is ItemKind.CHAT:
                return GateDecision(action=GateAction.HOLD, items=(scored,))

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
        worthy: list[ScoredItem] = []
        for item in items:
            _, scored = await self._score(item)
            if is_surfacing_worthy(scored, self._current_urgency_threshold()):
                worthy.append(scored)

        worthy.sort(key=lambda scored: scored.urgency, reverse=True)
        return worthy


__all__ = ["Gate", "UserModelProtocol"]
