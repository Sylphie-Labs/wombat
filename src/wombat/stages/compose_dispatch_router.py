"""ComposeDispatchRouter — route ONE surfaced item to its composer by item_kind (TK-10, EP-4, Q-51).

Dispatch is an ENGINE-DRIVEN ``Transition`` (Q-51 option (a)), never a direct call: ``run()``
returns ``Transition(to=<composer stage name for the item's kind>, output=compose_request)`` and
the cog-worx ENGINE drives the chosen composer next — the router never invokes a composer inside
``run()``. "Invoked exactly once" is therefore tested as ``Transition.to == <expected composer
stage name>``, mirroring how ``ComposeStage`` (TK-8) is itself a terminal Stage reading
``ctx.last_output("compose_dispatch")``.

The router is constructed with an INJECTED ``composer_by_kind: dict[ItemKind, str]`` map (kind ->
composer STAGE NAME); ``transitions`` is derived from the map's values PLUS ``_FALLBACK_COMPOSER``
(TK-172, CR-12) so the graph's declared edges cover every registered target AND the fallback edge
``run()`` can always return — even when a future map omits ``"compose"`` from its values, which
today's sole construction (``bootstrap.py``'s ``{ItemKind.GENERIC: "compose"}``) happens not to.
An unknown/unregistered kind falls back to ``"compose"`` (the generic TK-8 stage) and logs a
warning — it never raises and never silently drops the item (AC4).

Input is the NEW single-item ``wombat.surfaced_item`` wire (TK-7 ``review_or_speak``'s
single-ized forward of one gate-decisions entry), read via ``ctx.last_output("review_or_speak")``
— NOT the raw ``wombat.gate_decisions`` batch and NOT ``last_output("gate")`` (Q-51).

PAYLOAD BOUNDARY (Q-50 rider, CON-1, load-bearing): the router builds ``compose_request.payload``
from ``queue_item.payload`` ONLY — the user-facing dict. It never merges ``scored_item`` or
``action`` into the payload, so ``ComposeStage``'s "renders payload verbatim" guarantee holds
structurally: the model never sees urgency/load/GateAction/queue internals, because this wire
construction is the one place those fields could leak in and it deliberately excludes them.
"""

from __future__ import annotations

import logging

from cogworx.claims.provenance import Artifact, Provenance
from cogworx.loop.result import StageResult, Transition
from cogworx.loop.stage import StageContext

from wombat.gate.models import ItemKind
from wombat.stages.artifacts import (
    COMPOSE_REQUEST,
    compose_request_to_artifact_data,
    surfaced_item_from_artifact_data,
    surfaced_item_held_chat_from_artifact_data,
)

logger = logging.getLogger(__name__)

# The generic TK-8 composer stage — the fallback for any kind not in the injected map (AC4).
_FALLBACK_COMPOSER = "compose"


class ComposeDispatchRouter:
    """Dispatches ONE surfaced item to the composer stage registered for its item_kind."""

    name: str = "compose_dispatch"

    def __init__(self, *, composer_by_kind: dict[ItemKind, str]) -> None:
        self._composer_by_kind = composer_by_kind
        # The declared graph edges must cover every registered target (Q-51) PLUS the fallback
        # edge (TK-172, CR-12) -- run() can return Transition(to=_FALLBACK_COMPOSER) on an unknown
        # kind regardless of whether the injected map's values happen to include it.
        self.transitions: tuple[str, ...] = tuple(
            sorted(set(composer_by_kind.values()) | {_FALLBACK_COMPOSER})
        )

    async def run(self, ctx: StageContext) -> StageResult:
        art = await ctx.last_output("review_or_speak")
        if art is None:
            msg = "compose_dispatch: no review_or_speak output available yet"
            raise RuntimeError(msg)
        _action, scored_item, queue_item = surfaced_item_from_artifact_data(art.data)
        held_chat = surfaced_item_held_chat_from_artifact_data(art.data)

        composer_name = self._composer_by_kind.get(scored_item.item_kind)
        if composer_name is None:
            logger.warning(
                "compose_dispatch: item_kind %r not in composer_by_kind map; "
                "falling back to %r (item_id=%r)",
                scored_item.item_kind,
                _FALLBACK_COMPOSER,
                queue_item.idempotency_key,
            )
            composer_name = _FALLBACK_COMPOSER

        # PAYLOAD BOUNDARY (CON-1): payload is queue_item.payload ONLY — never scored_item/action.
        item_id = queue_item.idempotency_key
        data = compose_request_to_artifact_data(
            item_id, scored_item.item_kind, queue_item.payload, held_chat=held_chat
        )

        return Transition(
            to=composer_name,
            output=Artifact(
                kind=COMPOSE_REQUEST,
                produced_by=self.name,
                provenance=Provenance(source="system", confidence=1.0, recorded_at=ctx.clock()),
                data=data,
            ),
        )


__all__ = ["ComposeDispatchRouter"]
