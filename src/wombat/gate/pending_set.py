"""Production durable bounded pending set (TK-25, RISK-5) — hardened in place from TK-24's spike.

PERSISTENCE SEAM (Q-44): the pending set is wombat-DOMAIN gate state, not a cog-worx Engine
seam, so it does NOT join ``SubstrateBundle`` (see ``wombat.substrate`` — untouched here).
Instead this module defines its own ``PendingJournal`` Protocol — ``append(record)`` +
``replay()`` returning ordered, oldest-first records — so the write-ahead/replay semantics are
proven substrate-independently. ``InMemoryPendingJournal`` is the v1 default + test double; the
real Postgres adapter is split out to TK-29.

Every mutation is write-ahead-logged: the durable record lands in the journal BEFORE the
in-memory effect, so a kill can never lose a COMMITTED mutation, and replay (``dict`` semantics)
is idempotent so it can never double-count. ``rebuild_from_journal`` is the exactly-once
recovery path proven by the TK-24 kill scenarios (ported to ``tests/gate/test_pending_set.py``).

ORDERING under capacity eviction (Q-45): a capacity-forced ``add`` is a COMPOUND op — it emits
BOTH a ``PendingSetRemove`` (the evicted item) and a ``PendingSetAdd`` (the new item). These are
journaled Remove-BEFORE-Add so the only durable intermediate state a kill can freeze is
Remove-committed / Add-absent = ``max_pending - 1`` items, never ``max_pending + 1``. The size
<= ``max_pending`` invariant therefore holds at EVERY durable point. A kill in that two-append
window aborts an UNCOMMITTED ``add`` — the call never returned, so this is clean write-ahead
abort semantics, not loss: the TK-2 at-least-once queue redelivers the unacked source item on
restart and ``add`` re-runs (now a plain insert, since the evicted slot is already free) and the
system converges. So the precise guarantee is: no COMMITTED (returned) mutation is ever lost or
double-counted, and ``max_pending`` is never exceeded at any durable point.

``CapacityEviction`` is defined here (not ``models.py``) mirroring how ``Gate.decay()`` returns
``DecayEvent`` — TK-21's canonical decision vocabulary is not touched.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from wombat.gate.aggregator import cumulative_load as _cumulative_load
from wombat.gate.aggregator import lowest_urgency as _lowest_urgency
from wombat.gate.models import ItemKind, ScoredItem


@dataclass(frozen=True, slots=True)
class PendingSetAdd:
    """Write-ahead record for an add. Carries the FULL ScoredItem so replay restores
    ``cumulative_load()`` exactly, not just set membership.

    ``added_at`` is a SANCTIONED rider (TK-27, Q-55) on this journal record only — the
    canonical ``ScoredItem`` stays untouched. It carries the epoch-seconds instant the item
    entered the pending set, so the flush arm's min-age guard can read "how long has the
    oldest pending item been waiting" without the gate needing its own side-table. Defaults to
    ``0.0`` so a legacy record reconstructed without it (no persisted prod data exists yet;
    TK-29's pg adapter is unbuilt so the wire shape inherits this field for free) replays as
    the oldest possible item rather than raising.
    """

    item_id: str
    item_kind: ItemKind
    urgency: float
    load: float
    added_at: float = 0.0


@dataclass(frozen=True, slots=True)
class PendingSetRemove:
    """Write-ahead record for a single removal (drain-one or capacity eviction)."""

    item_id: str


@dataclass(frozen=True, slots=True)
class PendingSetClear:
    """Write-ahead record for the atomic bulk drain-all (no payload)."""


JournalRecord = PendingSetAdd | PendingSetRemove | PendingSetClear


@runtime_checkable
class PendingJournal(Protocol):
    """The wombat-owned durable log seam for pending-set mutations (Q-44).

    Deliberately NOT the cog-worx ``Journal`` (TK-24 seam-miss: that's a positional step/run
    WAL with no PendingSetAdd/Remove concept). Implementations must make ``append`` durable
    before returning, and ``replay`` must return records ordered oldest-first.
    """

    def append(self, record: JournalRecord) -> None: ...

    def replay(self) -> Sequence[JournalRecord]: ...


@dataclass(slots=True)
class InMemoryPendingJournal:
    """The v1 default + test substrate implementing ``PendingJournal``. Real pg is TK-29."""

    _records: list[JournalRecord] = field(default_factory=list)

    def append(self, record: JournalRecord) -> None:
        self._records.append(record)

    def replay(self) -> Sequence[JournalRecord]:
        return tuple(self._records)


@dataclass(frozen=True, slots=True)
class CapacityEviction:
    """Emitted by ``PendingSet.add`` when adding at capacity evicts the lowest-urgency item.

    Mirrors ``DecayEvent``'s shape (item_id + one scalar) rather than joining models.py.
    """

    item_id: str
    urgency: float


class PendingSet:
    """A durable, capacity-bounded set of ``ScoredItem``, journaled write-ahead (RISK-5)."""

    def __init__(self, *, journal: PendingJournal, max_pending: int) -> None:
        self._journal = journal
        self._max_pending = max_pending
        self._items: dict[str, ScoredItem] = {}
        self._added_at: dict[str, float] = {}

    def add(self, item: ScoredItem, *, added_at: float) -> CapacityEviction | None:
        """Write-ahead the add; if at capacity, journal the eviction Remove FIRST (Q-45).

        ``added_at`` (epoch seconds, TK-27 rider) is journaled on the ``PendingSetAdd`` record
        alongside the canonical ``ScoredItem`` fields, and tracked in memory so
        ``oldest_added_at()`` can answer the flush arm's min-age guard.
        """
        if len(self._items) < self._max_pending:
            self._journal.append(
                PendingSetAdd(
                    item_id=item.item_id,
                    item_kind=item.item_kind,
                    urgency=item.urgency,
                    load=item.load,
                    added_at=added_at,
                )
            )
            self._items[item.item_id] = item
            self._added_at[item.item_id] = added_at
            return None

        # At capacity: evict the lowest-urgency item FIRST (Remove-before-Add, Q-45). This
        # keeps the only durable intermediate state at max_pending-1 (Remove committed, Add
        # absent) rather than max_pending+1, so the size <= max_pending invariant holds at every
        # durable point. A kill in the two-append window aborts this UNCOMMITTED add (it never
        # returned); the TK-2 at-least-once queue redelivers the unacked item and add() re-runs.
        evicted = _lowest_urgency(self._items.values())
        assert evicted is not None  # capacity >= 1 implies non-empty here
        self._journal.append(PendingSetRemove(item_id=evicted.item_id))
        self._journal.append(
            PendingSetAdd(
                item_id=item.item_id,
                item_kind=item.item_kind,
                urgency=item.urgency,
                load=item.load,
                added_at=added_at,
            )
        )
        del self._items[evicted.item_id]
        self._added_at.pop(evicted.item_id, None)
        self._items[item.item_id] = item
        self._added_at[item.item_id] = added_at
        return CapacityEviction(item_id=evicted.item_id, urgency=evicted.urgency)

    def remove(self, item_id: str) -> None:
        """Write-ahead the removal, then discard from memory (tolerant of a missing id)."""
        self._journal.append(PendingSetRemove(item_id=item_id))
        self._items.pop(item_id, None)
        self._added_at.pop(item_id, None)

    def clear(self) -> tuple[ScoredItem, ...]:
        """Journaled atomic bulk drain-all; returns the drained items (TK-27's flush arm)."""
        self._journal.append(PendingSetClear())
        drained = tuple(self._items.values())
        self._items.clear()
        self._added_at.clear()
        return drained

    def cumulative_load(self) -> float:
        """Exact float sum of the current snapshot's ``.load`` (delegates to the aggregator)."""
        return _cumulative_load(self._items.values())

    def list(self) -> list[ScoredItem]:
        return list(self._items.values())

    def list_with_added_at(self) -> Sequence[tuple[ScoredItem, float]]:
        """Snapshot of current items paired with their journaled ``added_at`` instant.

        TK-28's ``decay_stale`` reads this (rather than the bare ``ScoredItem``) since
        ``added_at`` is the only durably journaled instant an age comparison can be based on
        (Q-73) — the canonical ``ScoredItem`` itself never carries a timestamp.

        Typed ``Sequence`` (not the bare ``list`` builtin) because this class already defines a
        method named ``list`` — inside a class body a same-named method binding shadows the
        builtin for any LATER annotation resolved in this scope (a real mypy/Python name-
        resolution gotcha, not a style choice).
        """
        return [(item, self._added_at[item_id]) for item_id, item in self._items.items()]

    def snapshot(self) -> tuple[ScoredItem, ...]:
        return tuple(self._items.values())

    def oldest_added_at(self) -> float | None:
        """The smallest ``added_at`` among current pending items, or ``None`` if empty.

        The flush arm (TK-27) reads this to compute the oldest pending item's age against
        ``flush_min_age_seconds``.
        """
        if not self._added_at:
            return None
        return min(self._added_at.values())

    def __len__(self) -> int:
        return len(self._items)

    @classmethod
    def rebuild_from_journal(cls, journal: PendingJournal, *, max_pending: int) -> PendingSet:
        """Replay the durable log into a fresh set — the exactly-once recovery path."""
        rebuilt = cls(journal=journal, max_pending=max_pending)
        items: dict[str, ScoredItem] = {}
        added_at: dict[str, float] = {}
        for record in journal.replay():
            if isinstance(record, PendingSetAdd):
                items[record.item_id] = ScoredItem(
                    item_id=record.item_id,
                    item_kind=record.item_kind,
                    urgency=record.urgency,
                    load=record.load,
                )
                # A legacy record with no persisted added_at replays at the field's own
                # default (0.0) — never a KeyError, never a raise (see PendingSetAdd docstring).
                added_at[record.item_id] = record.added_at
            elif isinstance(record, PendingSetRemove):
                items.pop(record.item_id, None)
                added_at.pop(record.item_id, None)
            else:
                items.clear()
                added_at.clear()
        rebuilt._items = items
        rebuilt._added_at = added_at
        return rebuilt
