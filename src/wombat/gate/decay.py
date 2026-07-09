"""Gate stale-item decay + the midnight-boundary observation (TK-28, EP-9, Q-13 split pass b).

Two small, load-bearing pieces (Q-73):

* ``decay_stale`` — the pure decay pass over the pending set. A pending item is stale iff its
  journaled ``added_at`` (NOT ``GateItem.created_at`` — see the corrected comment on
  ``gate/models.py``) is more than ``decay_ttl_seconds`` old. ``added_at`` is the ONLY durably
  journaled instant, so it is the only restart-consistent decay basis. Removal goes through the
  JOURNALED ``PendingSet.remove()`` path (appends a ``PendingSetRemove``) so a decayed item can
  never resurrect on ``rebuild_from_journal`` (RISK-5).
* ``DayRollover`` — the exactly-once wombat-day-boundary OBSERVATION. The per-class ceiling
  ALREADY resets structurally as-built (``CeilingLedger`` keys one ``DailyLedger`` row per
  ``ceiling:<EventClass.value>`` per wombat-date; a new civil day simply resolves a fresh row at
  0 — no timer, sleep-safe by construction, DEC-21/TK-152). So this ticket does not build a
  reset MUTATION; it builds the exactly-once EVENT that observes a day change happened, via a
  durable atomic upsert on ``DailyLedger`` — the ONLY call whose ``increment`` returns
  ``value == 1`` emits ``LedgerReset``, giving exactly-once across both a slept-through boundary
  (fires once at the first new-day tick) and a mid-day restart (lands ``value >= 2`` — no
  double-emit).

``LedgerReset`` is defined HERE (not ``models.py``) mirroring the as-built event-homing
convention: ``CeilingHit`` lives in ``trigger.py``, ``CapacityEviction`` in ``pending_set.py`` —
TK-21's canonical decision vocabulary in ``models.py`` is untouched.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Protocol, runtime_checkable

from wombat.domain.daily_ledger import DailyLedger
from wombat.gate.models import DecayEvent
from wombat.gate.pending_set import PendingSet

_ROLLOVER_LEDGER_NAME = "rollover:gate"


def decay_stale(
    pending_set: PendingSet, *, now: float, decay_ttl_seconds: float
) -> tuple[DecayEvent, ...]:
    """Remove every pending item whose journaled ``added_at`` age is strictly over the ttl.

    Pure aside from the ``pending_set`` mutation: reads ``pending_set.list_with_added_at()`` (a
    snapshot), then removes each stale item through the JOURNALED ``PendingSet.remove()`` (never
    a bare dict pop) so the removal is write-ahead-logged and cannot resurrect on
    ``rebuild_from_journal`` (RISK-5). Returns one ``DecayEvent`` per removed item, in the same
    order the snapshot was taken.
    """
    events: list[DecayEvent] = []
    for scored, added_at in pending_set.list_with_added_at():
        age_seconds = now - added_at
        if age_seconds > decay_ttl_seconds:
            pending_set.remove(scored.item_id)
            events.append(DecayEvent(item_id=scored.item_id, age_seconds=age_seconds))
    return tuple(events)


@dataclass(frozen=True, slots=True)
class LedgerReset:
    """Emitted exactly once per wombat-day the FIRST time any process observes that day.

    Not a reset MUTATION — the per-class ceiling already resets structurally (a new wombat-date
    simply resolves a fresh ``DailyLedger`` row). This event is the exactly-once OBSERVATION of
    that day change, routed through the gate's injected ``on_event`` (Q-73).
    """

    wombat_date: date


@runtime_checkable
class DayRolloverProtocol(Protocol):
    """The small day-boundary-observation seam the gate depends on (injected, Q-73).

    ``Gate`` depends on this Protocol, never on the concrete ``DayRollover`` — tests inject a
    no-op fake (``check()`` always returns ``None``) when they are not exercising rollover.
    """

    def check(self) -> LedgerReset | None: ...


class DayRollover:
    """The production ``DayRolloverProtocol`` over a real ``DailyLedger`` (ceiling.py precedent).

    Holds an in-memory last-seen wombat-day (cheap: ``DailyLedger.today()`` is a pure clock
    read, no I/O) plus a durable pg marker. On a detected day change it calls
    ``DailyLedger.increment("rollover:gate")`` — a durable atomic upsert — and emits
    ``LedgerReset`` ONLY when that upsert returns ``value == 1``. This is exactly-once across
    BOTH a slept-through boundary (fires once at the first new-day tick) and a restart (a
    fresh in-memory instance re-detects "change" from ``None``, but the durable row is already
    at >=1 for today, so the upsert lands at >=2 and never re-emits).
    """

    def __init__(self, *, daily_ledger: DailyLedger) -> None:
        self._daily_ledger = daily_ledger
        self._last_seen: date | None = None

    def check(self) -> LedgerReset | None:
        today = self._daily_ledger.today()
        if today == self._last_seen:
            return None
        # TK-169 (CR-4): stamp ``_last_seen`` only AFTER the durable increment succeeds. If
        # increment() raises (transient pg error), the exception propagates (fail-loud, by
        # design) and ``_last_seen`` stays untouched — the next check() on this same wombat-day
        # is a real retry, not a short-circuited no-op that would silently swallow the day's
        # LedgerReset.
        row = self._daily_ledger.increment(_ROLLOVER_LEDGER_NAME)
        self._last_seen = today
        if row.value == 1:
            return LedgerReset(wombat_date=today)
        return None


__all__ = ["DayRollover", "DayRolloverProtocol", "LedgerReset", "decay_stale"]
