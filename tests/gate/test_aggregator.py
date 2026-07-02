"""Tests for the pure stateless load/urgency aggregator (TK-25 — AC1, AC5)."""

from __future__ import annotations

from wombat.gate.aggregator import cumulative_load, lowest_urgency
from wombat.gate.models import ItemKind, ScoredItem


def _item(item_id: str, urgency: float, load: float) -> ScoredItem:
    return ScoredItem(item_id=item_id, item_kind=ItemKind.GENERIC, urgency=urgency, load=load)


def test_cumulative_load_is_exact_float_sum() -> None:
    items = [_item(f"i{i}", urgency=0.5, load=0.15) for i in range(10)]
    assert cumulative_load(items) == 1.5


def test_cumulative_load_empty_is_zero() -> None:
    assert cumulative_load([]) == 0.0


def test_lowest_urgency_picks_the_minimum() -> None:
    items = [
        _item("a", urgency=0.8, load=0.1),
        _item("b", urgency=0.2, load=0.1),
        _item("c", urgency=0.5, load=0.1),
    ]
    lowest = lowest_urgency(items)
    assert lowest is not None
    assert lowest.item_id == "b"


def test_lowest_urgency_empty_is_none() -> None:
    assert lowest_urgency([]) is None
