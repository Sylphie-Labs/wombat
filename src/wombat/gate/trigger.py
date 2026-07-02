"""Trigger arms' shared predicate + the ceiling seam (TK-27, EP-9, Q-55).

Two small, load-bearing pieces live here so the immediate arm and ``gate.select_items``
(Q-30) can never drift apart:

* ``is_surfacing_worthy`` — THE single shared per-item "is this worth an immediate voice"
  predicate. Both the pipeline's immediate arm and ``Gate.select_items`` call this SAME
  function; no second copy of the comparison exists anywhere.
* ``CeilingProtocol`` — the small runtime-checkable seam the per-class daily ceiling must
  satisfy. ``Gate`` depends on this Protocol (injected), never on a concrete ledger, so arm
  unit tests run un-gated against a fake and only ``CeilingLedger``'s own tests
  (``gate/ceiling.py``) need a real Postgres (Q-46).

``CeilingHit`` is defined here (not ``models.py``, TK-21's canonical decision vocabulary is
untouched) mirroring how ``pending_set.py`` homes ``CapacityEviction`` alongside the module
that raises it rather than in the shared model file.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from wombat.gate.models import ScoredItem
from wombat.rating.params import EventClass


def is_surfacing_worthy(scored: ScoredItem, urgency_threshold: float) -> bool:
    """THE single shared per-item worth predicate (Q-55): ``scored.urgency > urgency_threshold``.

    Used by BOTH the pipeline's immediate arm and ``Gate.select_items`` so the two never
    silently diverge into two different notions of "worth surfacing". Pure — no I/O, no
    ceiling read, no presence, no clock.
    """
    return scored.urgency > urgency_threshold


@runtime_checkable
class CeilingProtocol(Protocol):
    """The per-event-class daily surfacing-ceiling seam the gate depends on (injected).

    ``allow`` answers whether one more surfacing of ``event_class`` is permitted today;
    ``record`` books one. Keyed by ``EventClass`` (the DEC-13 personalization key), resolved
    by the injected ``user_model.resolve_event_class``. ``CeilingLedger`` (``gate/ceiling.py``)
    is the production implementation over the TK-152 ``DailyLedger``; tests inject a fake
    satisfying this same structural shape.
    """

    def allow(self, event_class: EventClass) -> bool: ...

    def record(self, event_class: EventClass) -> None: ...


@dataclass(frozen=True, slots=True)
class CeilingHit:
    """Emitted when an otherwise surfacing-worthy item is denied by the per-class ceiling.

    Routed through the gate's injected ``on_event`` callback (default: a loud log) so a
    ceiling denial is never silently swallowed — the item itself still falls through to the
    pending set (held), never dropped.
    """

    item_id: str
    event_class: EventClass


__all__ = ["CeilingHit", "CeilingProtocol", "is_surfacing_worthy"]
