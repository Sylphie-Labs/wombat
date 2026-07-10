"""Gate data model — the ONE canonical decision vocabulary (TK-21, ISS-4).

``GateAction`` is the single closed set of decision actions in all of wombat; no other
module may define a competing surface/hold/flush string set. The surfaced artifact also
carries ``item_kind`` so consumers route by kind instead of re-deriving a private enum.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class GateAction(Enum):
    """The single closed decision-action vocabulary (ISS-4). Nothing else defines these."""

    HOLD = "hold"
    SURFACE_IMMEDIATE = "surface_immediate"
    SURFACE_FLUSH = "surface_flush"


class ItemKind(Enum):
    """How a surfaced item is composed downstream; speak-vs-text is a sink concern."""

    BRIEF = "brief"
    REFLECTION = "reflection"
    DRAFT = "draft"
    GENERIC = "generic"
    # TK-222 (EP-32, Q-110(d)): chat is a REAL input source, never a side-channel to the model
    # (CON-1) — the runtime chat surface stamps this kind on every message it pushes.
    CHAT = "chat"


@dataclass(frozen=True, slots=True)
class GateItem:
    """An item entering the gate. ``payload`` holds opaque fields the scoring callables read."""

    item_id: str
    item_kind: ItemKind
    # epoch seconds. NOTE (TK-28, Q-73): decay compares the journaled PendingSetAdd.added_at
    # rider (pending_set.py) against decay_ttl_seconds, NOT this field — added_at is the only
    # durably journaled instant, so it is the only restart-consistent decay basis.
    created_at: float
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ScoredItem:
    """A scored item. Carries ``item_kind`` so the surfaced artifact routes by kind (AC1)."""

    item_id: str
    item_kind: ItemKind
    urgency: float
    load: float


@dataclass(frozen=True, slots=True)
class DecayEvent:
    """Emitted when a stale item is removed from the pending set."""

    item_id: str
    age_seconds: float


@dataclass(frozen=True, slots=True)
class GateDecision:
    """The gate's verdict. ``items`` is empty on HOLD."""

    action: GateAction
    items: tuple[ScoredItem, ...] = ()
