"""GateStage — the deterministic Hold vs Surface gate (TK-6, EP-4, Q-48; async-batch, Q-55).

Pulls the upstream drained batch (TK-5, ``ctx.last_output("drain_queue")``, deserialized through
the shared ``queue_items_from_artifact_data`` helper — never hand-parsed), takes ONE presence
snapshot for the whole batch, evaluates the WHOLE batch through ONE injected async ``evaluate``
callable (the Q-55 replacement seam: ``evaluate(items, presence) -> GateDecision`` scores every
item and returns exactly ONE ``GateDecision`` for the call — never one-decision-per-item; this
stage itself never changes when the production evaluator lands), and emits ONE batch Artifact
downstream. Never acks (TK-7's job on hold/completion) and never calls the mouth/model — the LLM
call count stays 0 (NG-4).

Two adapters satisfy the ``evaluate`` seam, both defined here (module-level factories):

* ``make_stub_evaluator`` — wraps the TK-6 per-item ``stub_evaluate`` (``gate/gate.py``) into the
  async-batch shape, behavior-preserving: at the Q-51 mvp ``batch_size=1`` composition rule there
  is exactly one item per call, so this reduces to "apply presence_hold then stub_urgency to that
  one item" exactly as before.
* ``make_gate_evaluator`` — wraps the production async ``Gate`` (TK-27, ``gate/pipeline.py``):
  computes ``surfacing_permitted`` ONCE per batch from the SAME canonical ``presence_hold``
  predicate (presence ``None`` degrades to held, same as every other presence-first call site),
  then awaits ``gate.pipeline(items, surfacing_permitted=...)``.

``GateStage`` touches ``ctx.last_output`` for the upstream batch and ``ctx.clock`` for the
outgoing ``Provenance.recorded_at`` timestamp only (Q-48 explicitly permits widening the ctx
surface this one step beyond ``last_output`` — the brief was clear no wall-clock may be invented,
and ``Provenance`` requires a timestamp, so this mirrors TK-5's exact ``ctx.clock()`` pattern
rather than reading a wall clock).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from cogworx.claims.provenance import Artifact, Provenance
from cogworx.loop.result import StageResult, Transition
from cogworx.loop.stage import StageContext

from wombat.gate.gate import gate_item_from_queue_item, stub_evaluate
from wombat.gate.models import GateAction, GateDecision, GateItem
from wombat.gate.pipeline import Gate
from wombat.gate.presence_hold import presence_hold
from wombat.sources.presence import PresenceSnapshot
from wombat.stages.artifacts import (
    GATE_DECISIONS,
    GateDecisionEntry,
    gate_decisions_to_artifact_data,
    queue_items_from_artifact_data,
)

# The Q-55 replacement seam: ONE async call scores the WHOLE batch and returns ONE decision.
EvaluateBatch = Callable[[list[GateItem], "PresenceSnapshot | None"], Awaitable[GateDecision]]


def make_stub_evaluator(
    *, urgency_threshold: float, staleness_ceiling_s: float, confidence_floor: float
) -> EvaluateBatch:
    """Adapt the TK-6 per-item ``stub_evaluate`` to the async-batch seam (behavior-preserving).

    At the Q-51 mvp ``batch_size=1`` composition rule ``items`` has exactly one element, so this
    is exactly the old per-item call: presence is applied first (Q-12), then the stub urgency
    lookup, for that one item — one item in, one ``GateDecision`` out. A multi-item call (not
    exercised at batch_size=1) evaluates every item independently through the same stub and
    returns the LAST item's decision — a documented degenerate case, never reached in practice.
    """

    async def evaluate(items: list[GateItem], presence: PresenceSnapshot | None) -> GateDecision:
        decision = GateDecision(action=GateAction.HOLD, items=())
        for item in items:
            decision = stub_evaluate(
                item,
                presence,
                urgency_threshold=urgency_threshold,
                staleness_ceiling_s=staleness_ceiling_s,
                confidence_floor=confidence_floor,
            )
        return decision

    return evaluate


def make_gate_evaluator(
    *,
    gate: Gate,
    staleness_ceiling_s: float,
    confidence_floor: float,
    clock: Callable[[], float],
) -> EvaluateBatch:
    """Adapt the production async ``Gate`` (TK-27) to the async-batch seam (Q-55).

    ``surfacing_permitted`` is computed ONCE per batch by the SAME canonical ``presence_hold``
    predicate every other presence-first call site uses (``presence is None`` degrades to held,
    same as ``stub_evaluate``) — this stage never re-derives its own presence policy. The scoring/
    ceiling/pending-set mechanics all live in ``Gate.pipeline`` itself; this adapter is pure
    wiring.
    """

    async def evaluate(items: list[GateItem], presence: PresenceSnapshot | None) -> GateDecision:
        surfacing_permitted = not presence_hold(
            presence,
            clock(),
            staleness_ceiling_s=staleness_ceiling_s,
            confidence_floor=confidence_floor,
        )
        return await gate.pipeline(items, surfacing_permitted=surfacing_permitted)

    return evaluate


class GateStage:
    """Evaluates the upstream drained batch through ONE injected async batch evaluator."""

    name: str = "gate"
    transitions: tuple[str, ...] = ("review_or_speak",)

    def __init__(
        self,
        evaluate: EvaluateBatch,
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

        gate_items = [gate_item_from_queue_item(queue_item) for queue_item in queue_items]
        decision = await self._evaluate(gate_items, presence)

        # At the Q-51 mvp batch_size=1 composition rule there is exactly one queue_item, so this
        # pairs the ONE decision with the ONE item that produced it — the exact shape every
        # downstream consumer (review_or_speak) is built around.
        entries: list[GateDecisionEntry] = [
            (decision, queue_item) for queue_item in queue_items
        ]

        return Transition(
            to="review_or_speak",
            output=Artifact(
                kind=GATE_DECISIONS,
                produced_by=self.name,
                provenance=Provenance(source="system", confidence=1.0, recorded_at=ctx.clock()),
                data=gate_decisions_to_artifact_data(entries),
            ),
        )


__all__ = ["EvaluateBatch", "GateStage", "make_gate_evaluator", "make_stub_evaluator"]
