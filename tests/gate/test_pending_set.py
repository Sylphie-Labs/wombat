"""Production durable pending set (TK-25, RISK-5) — hardened from the TK-24 spike.

Ports the TK-24 kill scenarios (originally tests/gate/test_pending_set_spike.py, now deleted
— its runnable proof is subsumed here) against the PRODUCTION PendingSet, plus AC1/AC2/AC3/AC5
from the TK-25 ticket. The "kill" mechanism throughout: the journal OBJECT survives while the
PendingSet instance holding it is discarded and a fresh one is rebuilt via
``rebuild_from_journal``. ``_KillSwitchJournal`` additionally lets a test inject a kill at (or
just after) a specific append, to prove the write-ahead ordering itself rather than just the
end-to-end replay.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

import pytest

from wombat.gate.aggregator import cumulative_load
from wombat.gate.models import ItemKind, ScoredItem
from wombat.gate.pending_set import (
    CapacityEviction,
    InMemoryPendingJournal,
    JournalRecord,
    PendingJournal,
    PendingSet,
)


def _item(item_id: str, urgency: float = 0.5, load: float = 0.1) -> ScoredItem:
    return ScoredItem(item_id=item_id, item_kind=ItemKind.GENERIC, urgency=urgency, load=load)


@dataclass(slots=True)
class _KillSwitchJournal:
    """Wraps a real journal to inject a kill at a specific append.

    If ``raise_before_append`` is True, the very next ``append`` raises WITHOUT forwarding to
    the wrapped journal (models a kill before the write commits — nothing durable happens).
    Otherwise, once ``raise_after`` appends have landed in the wrapped journal, the append that
    reaches that count raises immediately after forwarding (models a kill right after a write
    commits, before the caller's remaining code runs).
    """

    inner: InMemoryPendingJournal
    raise_after: int | None = None
    raise_before_append: bool = False
    _count: int = field(default=0, init=False)

    def append(self, record: JournalRecord) -> None:
        if self.raise_before_append:
            raise RuntimeError("simulated kill before append lands")
        self.inner.append(record)
        self._count += 1
        if self.raise_after is not None and self._count == self.raise_after:
            raise RuntimeError("simulated kill right after append lands")

    def replay(self) -> tuple[JournalRecord, ...]:
        return tuple(self.inner.replay())


# --- AC1: exact float sum, no model call -----------------------------------------------------


def test_ac1_cumulative_load_ten_items_exact_sum() -> None:
    journal = InMemoryPendingJournal()
    pending = PendingSet(journal=journal, max_pending=20)
    for i in range(10):
        pending.add(_item(f"i{i}", urgency=0.5, load=0.15))
    assert pending.cumulative_load() == 1.5


# --- AC2: clear() is a journaled, exactly-once bulk drain-all --------------------------------


def test_ac2_clear_kill_after_append_rebuilds_empty() -> None:
    journal = InMemoryPendingJournal()
    killer = _KillSwitchJournal(inner=journal, raise_after=6)  # 6th append = the clear's
    pending = PendingSet(journal=killer, max_pending=20)
    for i in range(5):
        pending.add(_item(f"i{i}"))
    with pytest.raises(RuntimeError):
        pending.clear()
    rebuilt = PendingSet.rebuild_from_journal(journal, max_pending=20)
    assert rebuilt.list() == []
    assert rebuilt.cumulative_load() == 0.0


def test_ac2_clear_kill_before_append_leaves_set_intact() -> None:
    journal = InMemoryPendingJournal()
    pending = PendingSet(journal=journal, max_pending=20)
    for i in range(5):
        pending.add(_item(f"i{i}"))
    killer = _KillSwitchJournal(inner=journal, raise_before_append=True)
    doomed = PendingSet(journal=killer, max_pending=20)
    with pytest.raises(RuntimeError):
        doomed.clear()
    rebuilt = PendingSet.rebuild_from_journal(journal, max_pending=20)
    assert len(rebuilt) == 5  # the Clear record never landed: nothing cleared


def test_ac2_clear_returns_drained_items() -> None:
    journal = InMemoryPendingJournal()
    pending = PendingSet(journal=journal, max_pending=20)
    pending.add(_item("a"))
    pending.add(_item("b"))
    drained = pending.clear()
    assert {item.item_id for item in drained} == {"a", "b"}
    assert len(pending) == 0


# --- AC3: capacity eviction ------------------------------------------------------------------


def test_ac3_capacity_eviction_evicts_lowest_urgency() -> None:
    journal = InMemoryPendingJournal()
    pending = PendingSet(journal=journal, max_pending=3)
    pending.add(_item("a", urgency=0.9))
    pending.add(_item("b", urgency=0.2))
    pending.add(_item("c", urgency=0.5))
    assert len(pending) == 3

    eviction = pending.add(_item("d", urgency=0.7))

    assert isinstance(eviction, CapacityEviction)
    assert eviction.item_id == "b"
    assert len(pending) == 3
    assert {item.item_id for item in pending.list()} == {"a", "c", "d"}


def test_ac3_add_below_capacity_returns_none() -> None:
    journal = InMemoryPendingJournal()
    pending = PendingSet(journal=journal, max_pending=5)
    assert pending.add(_item("a")) is None


def _at_capacity_set(seed: InMemoryPendingJournal) -> PendingSet:
    """A max_pending=3 set holding a,b,c with 'b' the lowest-urgency eviction target."""
    setup = PendingSet(journal=seed, max_pending=3)
    setup.add(_item("a", urgency=0.9))
    setup.add(_item("b", urgency=0.2))  # lowest urgency -> the eviction target
    setup.add(_item("c", urgency=0.5))
    return setup


def test_ac3_kill_between_evicting_appends_never_exceeds_capacity() -> None:
    """Kill AFTER the Remove append, BEFORE the Add append of an evicting add() (Q-45).

    An evicting add() at capacity emits Remove(evicted) THEN Add(new). ``raise_after=1`` fires
    right after the Remove lands (the killer counts only appends made through it), before the
    Add. The journal freezes at Remove-committed / Add-absent = max_pending-1: the uncommitted
    add() never returned, so its new item is absent, the evicted item is gone, and size stays
    <= max_pending (the pre-Q-45 bug produced max_pending+1 from Add-before-Remove).
    """
    seed = InMemoryPendingJournal()
    _at_capacity_set(seed)

    killer = _KillSwitchJournal(inner=seed, raise_after=1)  # raise just after the Remove append
    pending = PendingSet.rebuild_from_journal(seed, max_pending=3)
    pending._journal = killer  # swap in the kill-switch wrapper
    with pytest.raises(RuntimeError):
        pending.add(_item("d", urgency=0.7))

    rebuilt = PendingSet.rebuild_from_journal(seed, max_pending=3)
    rebuilt_ids = {item.item_id for item in rebuilt.list()}
    assert len(rebuilt) <= 3
    assert "b" not in rebuilt_ids  # evicted item stays evicted
    assert "d" not in rebuilt_ids  # uncommitted new item never resurrects
    assert rebuilt.cumulative_load() == cumulative_load(rebuilt.list())


def test_ac3_kill_before_remove_append_leaves_set_intact() -> None:
    """Kill BEFORE the Remove append of an evicting add(): nothing durable happened, set intact."""
    seed = InMemoryPendingJournal()
    _at_capacity_set(seed)

    killer = _KillSwitchJournal(inner=seed, raise_before_append=True)
    pending = PendingSet.rebuild_from_journal(seed, max_pending=3)
    pending._journal = killer  # swap in the kill-switch wrapper
    with pytest.raises(RuntimeError):
        pending.add(_item("d", urgency=0.7))

    rebuilt = PendingSet.rebuild_from_journal(seed, max_pending=3)
    assert {item.item_id for item in rebuilt.list()} == {"a", "b", "c"}
    assert len(rebuilt) == 3


def test_ac3_kill_after_both_evicting_appends_commits_swap() -> None:
    """Kill AFTER both appends of an evicting add(): the swap is durable and committed (Q-45).

    Both Remove(b) and Add(d) land, add() returns the CapacityEviction; the instance is then
    discarded and rebuilt. The committed swap survives: d present, b absent, size == max_pending.
    """
    seed = InMemoryPendingJournal()
    pending = _at_capacity_set(seed)

    eviction = pending.add(_item("d", urgency=0.7))  # both appends land; add() commits & returns
    assert isinstance(eviction, CapacityEviction)
    assert eviction.item_id == "b"

    rebuilt = PendingSet.rebuild_from_journal(seed, max_pending=3)
    rebuilt_ids = {item.item_id for item in rebuilt.list()}
    assert rebuilt_ids == {"a", "c", "d"}  # new item present, evicted item absent
    assert len(rebuilt) == 3


# --- AC4: mid-drain kill-and-restart — the 10 ported TK-24 kill scenarios --------------------


def test_ac4_kill_after_two_removes_leaves_exactly_three() -> None:
    journal = InMemoryPendingJournal()
    pending = PendingSet(journal=journal, max_pending=10)
    for i in range(5):
        pending.add(_item(f"i{i}", urgency=0.5, load=0.1))
    pending.remove("i0")
    pending.remove("i1")
    rebuilt = PendingSet.rebuild_from_journal(journal, max_pending=10)
    assert len(rebuilt) == 3
    assert {item.item_id for item in rebuilt.list()} == {"i2", "i3", "i4"}


def test_ac4_ten_seeded_kills_never_lose_or_duplicate() -> None:
    all_ids = {f"i{i}" for i in range(5)}
    for run in range(10):
        rng = random.Random(run)  # seeded for reproducibility
        journal = InMemoryPendingJournal()
        pending = PendingSet(journal=journal, max_pending=10)
        for item_id in sorted(all_ids):
            pending.add(_item(item_id, urgency=0.5, load=0.1))
        order = sorted(all_ids, key=lambda _: rng.random())
        kill_at = rng.randint(0, len(order))
        surfaced: set[str] = set()
        for k, item_id in enumerate(order):
            if k == kill_at:
                break  # crash here
            pending.remove(item_id)
            surfaced.add(item_id)
        checkpoint_load = pending.cumulative_load()
        rebuilt = PendingSet.rebuild_from_journal(journal, max_pending=10)
        rebuilt_ids = {item.item_id for item in rebuilt.list()}
        # exactly-once: no loss, no duplication
        assert len(rebuilt) + len(surfaced) == 5, f"run {run}: count drift"
        assert rebuilt_ids.isdisjoint(surfaced), f"run {run}: duplicated item"
        assert rebuilt_ids | surfaced == all_ids, f"run {run}: lost item"
        assert rebuilt.cumulative_load() == checkpoint_load, f"run {run}: load drift"


def test_ac4_rebuild_matches_pre_kill_checkpoint() -> None:
    journal = InMemoryPendingJournal()
    pending = PendingSet(journal=journal, max_pending=10)
    for i in range(5):
        pending.add(_item(f"i{i}", urgency=0.5, load=0.1))
    pending.remove("i0")
    checkpoint_len = len(pending)  # 4
    checkpoint_load = pending.cumulative_load()
    rebuilt = PendingSet.rebuild_from_journal(journal, max_pending=10)
    assert len(rebuilt) == checkpoint_len
    assert rebuilt.cumulative_load() == checkpoint_load


# --- AC5: empty set ----------------------------------------------------------------------------


def test_ac5_list_and_cumulative_load_on_empty_set() -> None:
    journal = InMemoryPendingJournal()
    pending = PendingSet(journal=journal, max_pending=5)
    assert pending.list() == []
    assert pending.cumulative_load() == 0.0


# --- Protocol sanity ---------------------------------------------------------------------------


def test_in_memory_journal_satisfies_pending_journal_protocol() -> None:
    assert isinstance(InMemoryPendingJournal(), PendingJournal)
