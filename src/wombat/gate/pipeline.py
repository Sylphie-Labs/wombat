"""Gate pipeline skeleton — accumulate -> score -> (trigger) -> decay (TK-21).

Pure and model-free (DEC-13, S9): scoring is done by INJECTED callables, never a model.
Trigger arms (TK-27/EP-9) and persistence (TK-25/EP-8) are out of scope here — with no
trigger arms wired, the pipeline always returns HOLD.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable

from .models import DecayEvent, GateAction, GateDecision, GateItem, ScoredItem

ScoreFn = Callable[[GateItem], float]
Clock = Callable[[], float]


class Gate:
    """The deterministic interruption gate's pipeline shape. Holds an in-memory pending set."""

    def __init__(
        self,
        *,
        urgency: ScoreFn,
        cognitive_load: ScoreFn,
        decay_ttl_seconds: float,
        clock: Clock = time.time,
    ) -> None:
        self._urgency = urgency
        self._cognitive_load = cognitive_load
        self._decay_ttl_seconds = decay_ttl_seconds
        self._clock = clock
        self._pending: dict[str, GateItem] = {}

    def accumulate(self, items: Iterable[GateItem]) -> None:
        """Append items to the pending set; duplicates (by item_id) are rejected idempotently."""
        for item in items:
            self._pending.setdefault(item.item_id, item)

    def score_pending(self) -> list[ScoredItem]:
        """Score every pending item via the injected callables. No model call occurs."""
        return [
            ScoredItem(
                item_id=item.item_id,
                item_kind=item.item_kind,
                urgency=self._urgency(item),
                load=self._cognitive_load(item),
            )
            for item in self._pending.values()
        ]

    def decay(self) -> list[DecayEvent]:
        """Remove items older than decay_ttl_seconds; emit a DecayEvent for each."""
        now = self._clock()
        events: list[DecayEvent] = []
        for item_id, item in list(self._pending.items()):
            age = now - item.created_at
            if age > self._decay_ttl_seconds:
                del self._pending[item_id]
                events.append(DecayEvent(item_id=item_id, age_seconds=age))
        return events

    def pipeline(self, items: Iterable[GateItem]) -> GateDecision:
        """End-to-end pass. No trigger arms here (TK-27), so the result is always HOLD."""
        self.accumulate(items)
        self.decay()
        self.score_pending()
        return GateDecision(action=GateAction.HOLD, items=())
