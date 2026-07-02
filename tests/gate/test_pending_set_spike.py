"""TK-24 SPIKE (RISK-5) — durable pending set is exactly-once across a mid-drain kill.

We model the crash by discarding the in-memory PendingSet and rebuilding from the durable
journal alone. The product invariant under test: rebuilt_pending + surfaced == the original
set, with no loss and no duplication, regardless of where the kill lands.
"""

from __future__ import annotations

import random

from wombat.gate.pending_set import PendingSet, WriteAheadJournal


def test_ac1_kill_after_two_surfaced_leaves_exactly_three() -> None:
    journal = WriteAheadJournal()
    pending = PendingSet(journal)
    for i in range(5):
        pending.add(f"i{i}")
    # drain begins; 2 items surfaced (journal writes happen)
    pending.remove("i0")
    pending.remove("i1")
    # process killed after the 2nd journal write -> drop in-memory state, rebuild from journal
    rebuilt = PendingSet.rebuild_from_journal(journal)
    assert len(rebuilt) == 3
    assert rebuilt.snapshot() == frozenset({"i2", "i3", "i4"})


def test_ac2_ten_random_kills_never_lose_or_duplicate() -> None:
    all_items = {f"i{i}" for i in range(5)}
    for run in range(10):
        rng = random.Random(run)  # seeded for reproducibility
        journal = WriteAheadJournal()
        pending = PendingSet(journal)
        for item in sorted(all_items):
            pending.add(item)
        order = sorted(all_items, key=lambda _: rng.random())
        kill_at = rng.randint(0, len(order))
        surfaced: set[str] = set()
        for k, item in enumerate(order):
            if k == kill_at:
                break  # crash here
            pending.remove(item)
            surfaced.add(item)
        rebuilt = PendingSet.rebuild_from_journal(journal)
        # exactly-once: no loss, no duplication
        assert len(rebuilt) + len(surfaced) == 5, f"run {run}: count drift"
        assert rebuilt.snapshot().isdisjoint(surfaced), f"run {run}: duplicated item"
        assert rebuilt.snapshot() | surfaced == all_items, f"run {run}: lost item"


def test_ac3_rebuild_matches_pre_kill_checkpoint() -> None:
    journal = WriteAheadJournal()
    pending = PendingSet(journal)
    for i in range(5):
        pending.add(f"i{i}")
    pending.remove("i0")
    checkpoint = len(pending)  # 4
    rebuilt = PendingSet.rebuild_from_journal(journal)
    assert len(rebuilt) == checkpoint
