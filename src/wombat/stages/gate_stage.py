"""GateStage — the deterministic Hold vs Surface gate (TK-6, EP-4, Q-48).

Pulls the upstream drained batch (TK-5, ``ctx.last_output("drain_queue")``, deserialized through
the shared ``queue_items_from_artifact_data`` helper — never hand-parsed), takes ONE presence
snapshot for the whole batch, evaluates every item through an injected ``evaluate`` callable (the
TK-27 replacement seam: composition binds ``stub_evaluate``'s ``urgency_threshold`` via
``functools.partial``; this stage itself never changes when the production evaluator lands), and
emits ONE batch Artifact downstream. Never acks (TK-7's job on hold/completion) and never calls
the mouth/model — the LLM call count stays 0 (NG-4).

``GateStage`` touches ``ctx.last_output`` for the upstream batch and ``ctx.clock`` for the
outgoing ``Provenance.recorded_at`` timestamp only (Q-48 explicitly permits widening the ctx
surface this one step beyond ``last_output`` — the brief was clear no wall-clock may be invented,
and ``Provenance`` requires a timestamp, so this mirrors TK-5's exact ``ctx.clock()`` pattern
rather than reading a wall clock).
"""

from __future__ import annotations

from collections.abc import Callable

from cogworx.claims.provenance import Artifact, Provenance
from cogworx.loop.result import StageResult, Transition
from cogworx.loop.stage import StageContext

from wombat.gate.gate import gate_item_from_queue_item
from wombat.gate.models import GateDecision, GateItem
from wombat.presence.probe import PresenceSnapshot
from wombat.stages.artifacts import (
    GATE_DECISIONS,
    GateDecisionEntry,
    gate_decisions_to_artifact_data,
    queue_items_from_artifact_data,
)


class GateStage:
    """Evaluates each item in the upstream drained batch through the injected gate evaluator."""

    name: str = "gate"
    transitions: tuple[str, ...] = ("review_or_speak",)

    def __init__(
        self,
        evaluate: Callable[[GateItem, PresenceSnapshot | None], GateDecision],
        presence_provider: Callable[[], PresenceSnapshot | None],
    ) -> None:
        self._evaluate = evaluate
        self._presence_provider = presence_provider

    async def run(self, ctx: StageContext) -> StageResult:
        art = await ctx.last_output("drain_queue")
        if art is None:
            msg = "gate: no drain_queue output available yet"
            raise RuntimeError(msg)
        queue_items = queue_items_from_artifact_data(art.data)

        # ONE presence snapshot for the whole batch — never re-read per item.
        presence = self._presence_provider()

        entries: list[GateDecisionEntry] = []
        for queue_item in queue_items:
            gate_item = gate_item_from_queue_item(queue_item)
            decision = self._evaluate(gate_item, presence)
            entries.append((decision, queue_item))

        return Transition(
            to="review_or_speak",
            output=Artifact(
                kind=GATE_DECISIONS,
                produced_by=self.name,
                provenance=Provenance(source="system", confidence=1.0, recorded_at=ctx.clock()),
                data=gate_decisions_to_artifact_data(entries),
            ),
        )


__all__ = ["GateStage"]
