"""Pure stateless load/urgency math over pending-set items (TK-25, RISK-5).

No I/O, no model calls, no mutable state. ``pending_set.PendingSet.cumulative_load()``
delegates here over its current snapshot; eviction uses ``lowest_urgency`` the same way.
Kept separate from ``pending_set.py`` per the Q-44 module split: this file is pure math over
``Iterable[ScoredItem]``, the other is the durable stateful set.
"""

from __future__ import annotations

from collections.abc import Iterable

from wombat.gate.models import ScoredItem


def cumulative_load(items: Iterable[ScoredItem]) -> float:
    """Exact float sum of ``.load`` over ``items``; ``0.0`` for an empty iterable."""
    return sum((item.load for item in items), 0.0)


def lowest_urgency(items: Iterable[ScoredItem]) -> ScoredItem | None:
    """The item with the smallest ``.urgency``, or ``None`` if ``items`` is empty."""
    items = list(items)
    if not items:
        return None
    return min(items, key=lambda item: item.urgency)
