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

TK-176 (EP-12): two OPTIONAL keyword seams, both ``None`` by default so existing behavior/tests
are byte-identical when unset.

``absorb_feedback`` diverts a queue item whose ``payload["kind"] == "feedback"`` (the TK-51
``FeedbackSignal`` wire, Q-49) BEFORE scoring: when wired, every feedback-marked item in the
drained batch is awaited through ``absorb_feedback`` and EXCLUDED from ``gate_items``/
``decision``/``entries`` — it structurally cannot reach ``evaluate``, the pending set, or the
brief. An ``absorb_feedback`` exception is caught and logged LOUD (the drain keeps draining); the
item is deliberately left un-acked (absorb owns its own ack on success) so the at-least-once
queue redelivers it — that IS the retry.

``stamp_resolution`` is awaited once per surviving ``(decision, queue_item)`` entry AFTER the
batch decision is computed — the hot-path ``OUTCOME_PENDING`` stamp (Q-22: never a terminal
``OUTCOME_*`` here). A ``stamp_resolution`` exception is caught and logged LOUD; the item is
simply invisible to that night's outcome pass (acceptable, loud) — it never blocks the drain.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from cogworx.claims.provenance import Artifact, Provenance
from cogworx.loop.result import StageResult, Transition
from cogworx.loop.stage import StageContext

from wombat.gate.gate import gate_item_from_queue_item, stub_evaluate
from wombat.gate.models import GateAction, GateDecision, GateItem
from wombat.gate.pipeline import Gate
from wombat.gate.presence_hold import presence_hold
from wombat.queue import QueueItem
from wombat.sources.presence import PresenceSnapshot
from wombat.stages.artifacts import (
    GATE_DECISIONS,
    GateDecisionEntry,
    gate_decisions_to_artifact_data,
    queue_items_from_artifact_data,
)

logger = logging.getLogger(__name__)

# The Q-55 replacement seam: ONE async call scores the WHOLE batch and returns ONE decision.
EvaluateBatch = Callable[[list[GateItem], "PresenceSnapshot | None"], Awaitable[GateDecision]]

# TK-176: the TK-51 FeedbackSignal wire marker (Q-49) — a queue_item whose payload carries this
# kind is diverted BEFORE scoring when absorb_feedback is wired.
_FEEDBACK_KIND = "feedback"

# TK-176 seams (both OPTIONAL, None default — see module docstring).
AbsorbFeedback = Callable[[QueueItem], Awaitable[None]]
StampResolution = Callable[..., Awaitable[None]]


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
        *,
        absorb_feedback: AbsorbFeedback | None = None,
        stamp_resolution: StampResolution | None = None,
    ) -> None:
        self._evaluate = evaluate
        self._presence_provider = presence_provider
        self._absorb_feedback = absorb_feedback
        self._stamp_resolution = stamp_resolution

    async def run(self, ctx: StageContext) -> StageResult:
        art = await ctx.last_output("drain_queue")
        if art is None:
            msg = "gate: no drain_queue output available yet"
            raise RuntimeError(msg)
        queue_items = queue_items_from_artifact_data(art.data)

        # TK-176: divert feedback-marked items BEFORE scoring, ONLY when absorb_feedback is
        # wired — unset, this is a no-op and behavior stays byte-identical to pre-TK-176.
        if self._absorb_feedback is not None:
            feedback_items = [
                item for item in queue_items if item.payload.get("kind") == _FEEDBACK_KIND
            ]
            queue_items = [
                item for item in queue_items if item.payload.get("kind") != _FEEDBACK_KIND
            ]
            for feedback_item in feedback_items:
                try:
                    await self._absorb_feedback(feedback_item)
                except Exception:
                    logger.error(
                        "gate: absorb_feedback failed for item %r; leaving it un-acked so "
                        "the at-least-once queue redelivers it (that IS the retry)",
                        feedback_item.idempotency_key,
                        exc_info=True,
                    )

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

        if self._stamp_resolution is not None:
            for stamp_decision, stamp_queue_item in entries:
                try:
                    await self._stamp_resolution(stamp_decision, stamp_queue_item)
                except Exception:
                    logger.error(
                        "gate: stamp_resolution failed for item %r; skipping (invisible to "
                        "tonight's outcome pass, never blocks the drain)",
                        stamp_queue_item.idempotency_key,
                        exc_info=True,
                    )

        return Transition(
            to="review_or_speak",
            output=Artifact(
                kind=GATE_DECISIONS,
                produced_by=self.name,
                provenance=Provenance(source="system", confidence=1.0, recorded_at=ctx.clock()),
                data=gate_decisions_to_artifact_data(entries),
            ),
        )


__all__ = [
    "AbsorbFeedback",
    "EvaluateBatch",
    "GateStage",
    "StampResolution",
    "make_gate_evaluator",
    "make_stub_evaluator",
]
