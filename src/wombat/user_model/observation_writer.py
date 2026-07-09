"""ObservationWriter — behavior/outcome write seam into the cog-worx user scope (TK-44, EP-11).

The ONE writer of ``wombat.user_model.claims.Claim`` objects into ``user:<user_id>`` (S7): every
call mints its ``ScopedKG`` via :func:`cogworx.knowledge.scoped_kg.user_model` under the fixed
owner ``'wombat.observation_writer'``, so a second writer attempting the same scope under a
different owner fails structurally (S7, enforced by ``ScopeRegistry.claim_write_token``) rather
than silently racing this one. TK-42's ``UserModel`` reads the RAW ``EntityKG`` (no token), so
there is no S7 conflict between the reader and this writer.

Three write paths:
  ``record``               generic behavior/outcome claims (``ClaimPredicate``-typed, TK-43's
                            closed vocabulary). The schema wall is re-checked here (defense in
                            depth beyond ``Claim.__post_init__``) so a duck-typed stand-in with a
                            hand-rolled string predicate cannot reach the entity KG (AC2).
  ``record_rating_params``  the BINDING Q-41 wire (ruling 4, standing obligation v0.30): writes
                            EXACTLY the predicate/subject/payload shape ``wombat.rating.params``
                            defines and ``UserModel.ratings_for`` reads, so the read/write wire
                            cannot drift (AC1b).
  ``record_superseding``   TK-45's supersede-capable widening: invalidates an existing claim THEN
                            writes the replacement via the exact ``record`` path. Invalidate-then-
                            assert IS the as-built supersede (Q-90) — the read side (TK-42's
                            ``UserModel``) is already newest-ACTIVE-wins, so there is no separate
                            Defeat/SUPERSEDES write needed here.

``claim.value`` (TK-43's already-JSON-native payload string, Q-49 convention) and ``event_id``
are carried together as one JSON envelope object (``{"value": ..., "event_id": ...}``) written as
the claim's ``obj`` — ``assert_fact`` accepts a single ``obj`` string, and treating ``value`` as an
opaque nested string (rather than re-parsing it) works regardless of what shape it holds.

Frame: no outcome-decision logic (TK-50) — this writer records what TK-45's ``OutcomeLabeler``
hands it, never decides an outcome itself. No CoherenceStore/Defeat call-site (Q-90, verified
against the installed cog-worx ``scoped_kg.py``: that surface is the reconciler's
``CoherenceStore.commit_reconciliation``, not this writer's — ``ScopedKG``'s write surface is
``assert_fact``/``add_evidence``/``invalidate`` only). Does not construct ``EntityKG``/
``ScopeRegistry`` (injected; TK-14's bundle owns them), no LLM, no pg.
"""

from __future__ import annotations

import json
import logging

from cogworx.knowledge.scoped_kg import user_model
from cogworx.knowledge.scopes import ScopeRegistry
from cogworx.knowledge.source_registry import SourceDeclaration
from cogworx.substrate.entity_kg import EntityKG

from wombat.rating.params import RATING_CLAIM_PREDICATE, EventClass, RatingParams, to_claim_payload
from wombat.user_model.claims import Claim, ClaimPredicate

logger = logging.getLogger(__name__)

# The single authorized writer to user:<user_id> (S7) — also the SourceDeclaration ref, so every
# claim this writer mints carries a source_ref that traces back to this module.
_OWNER = "wombat.observation_writer"


class ObservationWriter:
    """The ONE behavior/outcome write seam into one user's cog-worx user-model scope (S7).

    Keyword-injected deps only (TK-42 precedent): ``entity_kg`` is the raw cog-worx ``EntityKG``
    Protocol, ``scope_registry`` mints the S7 write token, ``user_id`` is a plain string. The
    ``ScopedKG`` view is built once, internally, at construction time — callers never see or mint
    a write token themselves.
    """

    def __init__(self, *, entity_kg: EntityKG, scope_registry: ScopeRegistry, user_id: str) -> None:
        self._scoped_kg = user_model(entity_kg, scope_registry, user_id, owner=_OWNER)
        self._source = SourceDeclaration(kind="system", ref=_OWNER)

    async def record(self, claim: Claim) -> str:
        """Write one behavior/outcome ``Claim`` (AC1). Returns the canonical claim id.

        Re-validates ``claim.predicate`` is a ``ClaimPredicate`` member BEFORE any I/O
        (``TypeError`` otherwise, AC2) — defense in depth beyond ``Claim.__post_init__``, since a
        duck-typed stand-in can carry a hand-rolled string predicate past a type-only check.
        On an entity-KG write failure the error is logged and RE-RAISED, never silently dropped
        (AC3).
        """
        if not isinstance(claim.predicate, ClaimPredicate):
            raise TypeError(
                f"ObservationWriter.record: claim.predicate must be a ClaimPredicate, got "
                f"{type(claim.predicate).__name__}: {claim.predicate!r}"
            )
        obj = json.dumps({"value": claim.value, "event_id": claim.event_id})
        try:
            return await self._scoped_kg.assert_fact(
                subject=claim.subject,
                predicate=claim.predicate.value,
                obj=obj,
                epistemic_type="observation",
                source=self._source,
                created_by=_OWNER,
            )
        except Exception:
            logger.error(
                "ObservationWriter.record: entity-KG write failed (subject=%r, predicate=%r)",
                claim.subject,
                claim.predicate.value,
                exc_info=True,
            )
            raise

    async def record_rating_params(
        self,
        event_class: EventClass,
        params: RatingParams,
        *,
        source: SourceDeclaration | None = None,
    ) -> str:
        """Write ``params`` under the BINDING Q-41 wire (ruling 4). Returns the canonical claim id.

        MUST write through ``wombat.rating.params``'s helpers —
        predicate=``RATING_CLAIM_PREDICATE``, subject=``event_class.value``,
        obj=``to_claim_payload(params)`` — so
        ``UserModel.ratings_for`` (the as-built read seam) reads back EXACTLY what was written
        (AC1b). On an entity-KG write failure the error is
        logged and RE-RAISED, never silently dropped (AC3).

        ``source`` (TK-49, additive/keyword-only) lets a caller other than this writer's own
        default owner (e.g. ``RatingTuner``) stamp its OWN provenance on the write — ``None`` (the
        default) preserves the exact prior behavior, writing under ``self._source``. The Q-41
        payload wire (``to_claim_payload``) stays CLOSED either way: provenance rides the claim's
        ``SourceDeclaration`` only, never the payload.
        """
        try:
            return await self._scoped_kg.assert_fact(
                subject=event_class.value,
                predicate=RATING_CLAIM_PREDICATE,
                obj=to_claim_payload(params),
                epistemic_type="observation",
                source=source if source is not None else self._source,
                created_by=_OWNER,
            )
        except Exception:
            logger.error(
                "ObservationWriter.record_rating_params: entity-KG write failed (event_class=%r)",
                event_class.value,
                exc_info=True,
            )
            raise

    async def record_superseding(self, claim: Claim, *, supersedes_claim_id: str) -> str:
        """Invalidate ``supersedes_claim_id`` THEN write ``claim`` via the exact ``record`` write
        path (TK-45 widening). Returns the canonical claim id of the newly-written claim.

        Invalidate-then-assert IS the as-built supersede (Q-90): the read side (TK-42's
        ``UserModel``) is already newest-ACTIVE-wins, so no separate Defeat/SUPERSEDES write is
        needed — ``ScopedKG``'s write surface is ``assert_fact``/``add_evidence``/``invalidate``
        only; the reconciler's ``CoherenceStore.commit_reconciliation`` is a different surface
        this writer does not touch.

        ``claim.predicate`` is re-validated as a ``ClaimPredicate`` BEFORE any I/O (``TypeError``
        otherwise) — same defense-in-depth as ``record``, checked here first so an invalid claim
        never reaches ``invalidate`` either. ``self._scoped_kg.invalidate`` raises ``ValueError``
        for an unknown or out-of-scope ``supersedes_claim_id`` — propagated, not caught (there is
        no compensating write to roll back). The subsequent write reuses ``record`` verbatim, so
        it carries the same schema wall and the same store-failure logged + RE-RAISED discipline
        (AC3).
        """
        if not isinstance(claim.predicate, ClaimPredicate):
            raise TypeError(
                f"ObservationWriter.record_superseding: claim.predicate must be a "
                f"ClaimPredicate, got {type(claim.predicate).__name__}: {claim.predicate!r}"
            )
        await self._scoped_kg.invalidate(supersedes_claim_id)
        return await self.record(claim)
