"""UserModel.ratings_for — pure async read seam over the cog-worx user scope (TK-42, EP-10).

``UserModel`` reads a user's personalized rating parameters back out of the cog-worx entity KG.
It is a deterministic KEY LOOKUP (point-read of the claim about the resolved event class, scoped
to ``user:<user_id>``), not a ranked recall — see AC1. Frame: no model calls, no I/O beyond the
one injected ``EntityKG`` read.

Q-41 rulings honored here:
  1. ``resolve_event_class`` owns the GateItem -> EventClass resolution: the payload's
     'event_class' key (string or ``EventClass``) is the REQUIRED path for CALENDAR_CONFLICT;
     otherwise the TOTAL ``ItemKind`` fallback map applies. TK-21's ``ItemKind`` is not amended —
     ``payload`` is its designed extension point.
  2. ``user_id`` is an injected constructor arg; ``UserModel`` forms ``scope = f"user:{user_id}"``
     itself. ``params.py``'s ``OperatingParams`` is untouched.
  3. ``ratings_for`` is ``async def`` and awaits the injected ``EntityKG.claims_about`` directly —
     no ``asyncio.run()`` (that would ``RuntimeError`` inside cog-worx's already-running async
     Stage loop and be silently swallowed to defaults-forever).
  4. The claim wire shape (predicate + JSON payload codec) is homed in
     ``wombat.rating.params`` (``RATING_CLAIM_PREDICATE`` / ``to_claim_payload`` /
     ``params_from_claim_payload``) — the shared contract TK-44/TK-49 write through.
"""

from __future__ import annotations

import logging

from cogworx.substrate.entity_kg import EntityKG

from wombat.gate.models import GateItem, ItemKind
from wombat.rating.params import (
    RATING_CLAIM_PREDICATE,
    EventClass,
    RatingParams,
    default_params_for,
    params_from_claim_payload,
)

logger = logging.getLogger(__name__)

# Q-41 ruling 1: the TOTAL ItemKind -> EventClass fallback map. Every ItemKind member has an
# entry, so resolve_event_class never falls off the end of this map.
_ITEM_KIND_FALLBACK: dict[ItemKind, EventClass] = {
    ItemKind.BRIEF: EventClass.MORNING_BRIEF,
    ItemKind.REFLECTION: EventClass.REFLECTION,
    ItemKind.DRAFT: EventClass.DRAFT_REPLY,
    ItemKind.GENERIC: EventClass.GENERIC,
    # TK-222 (EP-32, Q-110(d)): chat rides the GENERIC rating vocabulary — deliberately NO new
    # EventClass for chat.
    ItemKind.CHAT: EventClass.GENERIC,
}


class UserModel:
    """Pure-read seam over one user's rating-parameter claims (Q-41).

    Keyword-injected deps only: ``entity_kg`` is the raw cog-worx ``EntityKG`` Protocol — NOT
    ``ScopedKG.user_model()``, which mints an S7 write token this read-only seam has no use for
    (Q-41 ruling 3). ``user_id`` is a plain string; the scope ``user:<user_id>`` is formed here.
    """

    def __init__(self, *, entity_kg: EntityKG, user_id: str) -> None:
        self._entity_kg = entity_kg
        self._user_id = user_id
        self._scope = f"user:{user_id}"

    def resolve_event_class(self, item: GateItem) -> EventClass:
        """Resolve the ``EventClass`` an item's rating params are keyed by (Q-41 ruling 1).

        The payload's ``'event_class'`` key is the REQUIRED path for CALENDAR_CONFLICT (GateItem
        carries no first-class EventClass field). Absent, or present but not a valid EventClass
        value, falls back to the TOTAL ``ItemKind`` map; an invalid value also logs a warning so
        a silently-wrong payload doesn't go unnoticed.
        """
        raw = item.payload.get("event_class")
        if raw is not None:
            try:
                return EventClass(raw)
            except ValueError:
                logger.warning(
                    "UserModel.resolve_event_class: invalid event_class payload value %r on "
                    "item %r; falling back to the ItemKind map",
                    raw,
                    item.item_id,
                )
        return _ITEM_KIND_FALLBACK[item.item_kind]

    async def ratings_for(self, item: GateItem) -> RatingParams:
        """Return this user's personalized ``RatingParams`` for ``item`` (AC1-AC3).

        Point-reads (deterministic key lookup, not ranked recall) the newest active
        ``RATING_CLAIM_PREDICATE`` claim about the resolved event class, scoped to
        ``user:<user_id>``. Any failure — the entity-KG read raising (store unreachable), no
        matching claim, or a malformed/unknown-version stored payload — falls back to
        ``default_params_for`` and (except the plain "no node" case) logs a warning. Never raises
        (AC3); store-agnostic (works the same against ``InMemoryEntityKG`` and the real Neo4j
        adapter).
        """
        event_class = self.resolve_event_class(item)

        try:
            claims = await self._entity_kg.claims_about(event_class.value, scope=self._scope)
        except Exception:
            logger.warning(
                "UserModel.ratings_for: entity-KG read failed (scope=%r, entity=%r); "
                "falling back to defaults",
                self._scope,
                event_class.value,
                exc_info=True,
            )
            return default_params_for(event_class)

        # claims_about returns newest-first; take the newest claim written under our predicate.
        rating_claims = [c for c in claims if c.claim.predicate == RATING_CLAIM_PREDICATE]
        if not rating_claims:
            return default_params_for(event_class)

        try:
            return params_from_claim_payload(rating_claims[0].claim.payload)
        except ValueError:
            logger.warning(
                "UserModel.ratings_for: malformed rating-claim payload (scope=%r, entity=%r); "
                "falling back to defaults",
                self._scope,
                event_class.value,
                exc_info=True,
            )
            return default_params_for(event_class)
