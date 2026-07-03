"""BriefDecisionArtifact — the sealed, immutable output of ``BriefForceFlushStage`` (TK-99, Q-75).

Mirrors ``wombat.domain.brief_payload`` (TK-98): frozen (+slots) dataclasses with JSON-native
``to_payload``/``from_payload`` wire helpers an Artifact's ``data`` round-trips through exactly
(Q-49). ``BriefForceFlushStage`` is the only producer; ``brief_compose`` (TK-100) is the intended
consumer.

``BriefBucket`` groups the SELECTED brief items (the ones ``Gate.select_items`` judged worth
surfacing) by family: ``recap`` (Gmail items), ``conflict`` (derived calendar conflicts), ``prep``
(calendar events). ``conflict`` entries are stored as the ALREADY-serialized
``wombat.calendar.conflict.conflict_to_payload`` dict — the ONE stamping helper for a conflict's
wire shape (Q-62) — rather than a domain object, because that helper's dict (event_id/title only,
no start/end minutes) is not losslessly invertible back into a ``DailyConflict``; storing the wire
dict directly makes the round-trip trivially exact instead of inventing a second, richer
serialization of the same fact.

CANONICAL WIRE FORMS ONLY (Q-50 mouth-facing boundary): every entry crosses through its own
canonical ``to_payload`` (``GmailBriefItem``/``CalendarEvent``) or the one conflict stamping
helper — none of these ever carry a gate ``ScoredItem``'s ``urgency``/``load`` scoring keys, since
selection discards the ``ScoredItem`` the moment it has decided which artifact-local item_ids made
the cut.

``item_kind`` is always stamped ``ItemKind.BRIEF.value`` ("brief") — the whole artifact is one
brief's worth of items, never a mix of kinds.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from wombat.calendar.models import CalendarEvent
from wombat.domain.brief_payload import GmailBriefItem
from wombat.gate.models import ItemKind


@dataclass(frozen=True, slots=True)
class BriefBucket:
    """The selected brief items, grouped by family (Q-75 ruling 3-4).

    ``recap`` = selected Gmail items, ``conflict`` = selected derived-conflict wire dicts,
    ``prep`` = selected calendar events. Each tuple preserves the urgency-descending order
    ``Gate.select_items`` returned, filtered down to that one family.
    """

    recap: tuple[GmailBriefItem, ...] = ()
    conflict: tuple[dict[str, Any], ...] = ()
    prep: tuple[CalendarEvent, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        """JSON-native wire form (Q-49): each family through its own canonical serialization."""
        return {
            "recap": [item.to_payload() for item in self.recap],
            "conflict": [dict(entry) for entry in self.conflict],
            "prep": [event.to_payload() for event in self.prep],
        }

    @staticmethod
    def from_payload(d: dict[str, Any]) -> BriefBucket:
        """Inverse of ``to_payload`` — exact round-trip: ``from_payload(b.to_payload()) == b``."""
        return BriefBucket(
            recap=tuple(GmailBriefItem.from_payload(raw) for raw in d["recap"]),
            conflict=tuple(dict(raw) for raw in d["conflict"]),
            prep=tuple(CalendarEvent.from_payload(raw) for raw in d["prep"]),
        )


@dataclass(frozen=True, slots=True)
class BriefDecisionArtifact:
    """The sealed, immutable output of ``BriefForceFlushStage`` (Q-75).

    ``item_kind`` is always ``ItemKind.BRIEF.value`` — a structural stamp, not a per-call choice.
    ``calendar_unavailable``/``gmail_unavailable`` carry the upstream ``BriefPayload`` degrade
    flags through unchanged, so a downstream consumer can tell a genuinely-empty bucket apart
    from a source outage. Frozen + slots: any post-construction mutation attempt raises
    ``dataclasses.FrozenInstanceError`` — sealed the instant the stage builds it.
    """

    bucket: BriefBucket
    calendar_unavailable: bool
    gmail_unavailable: bool
    item_kind: str = field(default=ItemKind.BRIEF.value)

    def to_payload(self) -> dict[str, Any]:
        """JSON-native wire form (Q-49): the one shape ``from_payload`` round-trips exactly."""
        return {
            "item_kind": self.item_kind,
            "bucket": self.bucket.to_payload(),
            "calendar_unavailable": self.calendar_unavailable,
            "gmail_unavailable": self.gmail_unavailable,
        }

    @staticmethod
    def from_payload(d: dict[str, Any]) -> BriefDecisionArtifact:
        """Inverse of ``to_payload`` — exact round-trip: ``from_payload(a.to_payload()) == a``."""
        return BriefDecisionArtifact(
            bucket=BriefBucket.from_payload(d["bucket"]),
            calendar_unavailable=d["calendar_unavailable"],
            gmail_unavailable=d["gmail_unavailable"],
            item_kind=d["item_kind"],
        )


__all__ = ["BriefBucket", "BriefDecisionArtifact"]
