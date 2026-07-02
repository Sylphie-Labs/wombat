"""SPIKE (TK-24, RISK-5) — durable pending set, exactly-once across a mid-drain crash.

THROWAWAY prototype. It uses its OWN in-process write-ahead journal stub, NOT the cog-worx
Journal: the audit (seam-miss TK-24) confirmed cog-worx's Journal is a positional step/run WAL
("not a generic store that flattens engines") with no PendingSetAdd/Remove concept — so per
this spike's non_goal we emulate write-ahead here to prove the *semantics* before TK-25 picks a
production substrate.

Hypothesis under test: appending a PendingSetAdd/PendingSetRemove record to the journal BEFORE
mutating in-memory state, then replaying the log on restart, yields exactly-once add/remove
across a kill at any point — no locking required. Write-ahead is the whole trick: the record is
durable before the in-memory effect, so a crash can never lose a committed mutation, and replay
is idempotent (set semantics), so it can never double-count.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class PendingSetAdd:
    item_id: str


@dataclass(frozen=True, slots=True)
class PendingSetRemove:
    item_id: str


JournalRecord = PendingSetAdd | PendingSetRemove


@dataclass(slots=True)
class WriteAheadJournal:
    """An append-only durable log. Survives the simulated kill; the in-memory set does not."""

    records: list[JournalRecord] = field(default_factory=list)

    def append(self, record: JournalRecord) -> None:
        self.records.append(record)


class PendingSet:
    """An in-memory set whose every mutation is write-ahead-logged to a durable journal."""

    def __init__(self, journal: WriteAheadJournal) -> None:
        self._journal = journal
        self._items: set[str] = set()

    def add(self, item_id: str) -> None:
        self._journal.append(PendingSetAdd(item_id))  # write-ahead: durable BEFORE in-memory
        self._items.add(item_id)

    def remove(self, item_id: str) -> None:
        self._journal.append(PendingSetRemove(item_id))  # write-ahead: durable BEFORE in-memory
        self._items.discard(item_id)

    def snapshot(self) -> frozenset[str]:
        return frozenset(self._items)

    def __len__(self) -> int:
        return len(self._items)

    @classmethod
    def rebuild_from_journal(cls, journal: WriteAheadJournal) -> PendingSet:
        """Replay the durable log into a fresh set — the exactly-once recovery path."""
        rebuilt = cls(journal)
        items: set[str] = set()
        for record in journal.records:
            if isinstance(record, PendingSetAdd):
                items.add(record.item_id)
            else:
                items.discard(record.item_id)
        rebuilt._items = items
        return rebuilt
