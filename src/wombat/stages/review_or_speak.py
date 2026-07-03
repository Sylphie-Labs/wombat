"""ReviewOrSpeakStage — branch on the canonical GateAction; ack, then Transition/Done (TK-7, Q-52).

Reads the upstream gate-decisions batch (``ctx.last_output("gate")``, deserialized ONLY through
``gate_decisions_from_artifact_data`` — the shared Q-48 helper, never hand-parsed) and, per entry,
acks the item off the injected ``WombatQueue`` (Q-52: ack happens inside ``run()`` — decide, ack,
return — BEFORE the engine journals the result; the ack-before-journal crash window is ACCEPTED at
stub custody because both failure modes degrade toward silence, never a double-surface).

At the Q-51 mvp ``batch_size=1`` composition rule there is at most ONE entry, so the batch is
EITHER one hold -> terminal ``Done(wombat.hold_report)`` (the engine journals this StepResult
before advancing, so AC2's "hold reason in the journal" holds with NO direct journal write, Q-47)
OR one surface -> ``Transition(to="compose_dispatch", output=wombat.surfaced_item)``. A MIXED
batch (>1 entries) is structurally unreachable at batch_size=1; the defensive branch below acks
every entry, forwards the FIRST surfaced item, and logs LOUD warnings naming every non-forwarded
item_id (surplus surfaced entries AND holds alike) — never silent, never lease-stranded.

SURFACE_FLUSH DIGEST (Q-55 rider, demo-harness assembly): the production ``Gate`` (TK-27) can
return a ``SURFACE_FLUSH`` decision whose ``items`` carries the WHOLE flushed pending set — many
``ScoredItem``s, not one. This stage single-izes that batch down to ONE synthesized
``surfaced_item`` (a terse digest: count + item-kind mix) so exactly ONE ``compose_request``
reaches the mouth (one consolidated line) rather than fan-out per held item — the real
brief-composition path is TK-99/TK-100; this is the demo-grade digest. ``SURFACE_IMMEDIATE``
still forwards its single item unchanged.

EMPTY-ITEMS HOLD (Q-55 rider): the production ``Gate``'s ``HOLD`` carries an EMPTY ``items``
tuple (the score is discarded once the item lands in the durable pending set — TK-27) whereas the
TK-6 stub's ``HOLD`` always carries the one scored item. A hold record is built from
``decision.items[0]`` when present, or a documented placeholder (scored fields unknown, urgency/
load reported as ``None``) reconstructed from the queue item alone when not — never an
``IndexError``.

This stage NEVER branches on ``item_kind`` and NEVER references a concrete composer — routing to
the right composer by kind is entirely TK-10's job (ISS-5); ``ctx`` surface is exactly
``last_output("gate")`` + ``clock`` (provenance only).
"""

from __future__ import annotations

import logging
from typing import Protocol

from cogworx.claims.provenance import Artifact, Provenance
from cogworx.loop.result import Done, StageResult, Transition
from cogworx.loop.stage import StageContext

from wombat.gate.gate import gate_item_from_queue_item
from wombat.gate.models import GateAction, ItemKind, ScoredItem
from wombat.queue import QueueItem
from wombat.stages.artifacts import (
    HOLD_REPORT,
    SURFACED_ITEM,
    gate_decisions_from_artifact_data,
    hold_report_to_artifact_data,
    surfaced_item_to_artifact_data,
)

logger = logging.getLogger(__name__)

# The canonical closed set of "this item is going out" actions (TK-21/ISS-4) — everything else
# (currently only GateAction.HOLD) is a hold.
_SURFACE_ACTIONS = (GateAction.SURFACE_IMMEDIATE, GateAction.SURFACE_FLUSH)

# The digest id/kind prefix for a synthesized SURFACE_FLUSH surfaced_item (Q-55 rider).
_DIGEST_PREFIX = "digest"


class _AckableQueue(Protocol):
    """The one queue method ReviewOrSpeakStage needs — a structural seam so tests can inject a
    bare fake instead of a real ``WombatQueue`` (which the composition root passes per ASMP-2,
    the SAME instance injected into ``DrainQueueStage``)."""

    def ack(self, item_id: int) -> None: ...


def _hold_record(scored_item: ScoredItem | None, queue_item: QueueItem) -> dict[str, object]:
    """Build one JSON-native hold record.

    When ``scored_item`` is present (the TK-6 stub's HOLD always carries one) the reason derives
    from it, as before. When it is ``None`` (the production ``Gate``'s HOLD, Q-55 rider — the
    score was discarded once the item entered the durable pending set) the record still names the
    item and its kind (recovered from the queue item's own payload) with an honest placeholder
    reason instead of fabricating a score.
    """
    if scored_item is None:
        item_kind = gate_item_from_queue_item(queue_item).item_kind
        return {
            "item_id": queue_item.idempotency_key,
            "item_kind": item_kind.value,
            "reason": "held — accumulating in the durable pending set (score not carried on hold)",
            "urgency": None,
            "load": None,
        }
    reason = f"urgency={scored_item.urgency:.2f} load={scored_item.load:.2f} below surface bar"
    return {
        "item_id": queue_item.idempotency_key,
        "item_kind": scored_item.item_kind.value,
        "reason": reason,
        "urgency": scored_item.urgency,
        "load": scored_item.load,
    }


def _digest_scored_item(items: tuple[ScoredItem, ...]) -> ScoredItem:
    """Synthesize ONE aggregate ``ScoredItem`` representing a SURFACE_FLUSH batch (Q-55 rider).

    ``item_kind`` is always ``GENERIC`` (a consolidated digest belongs to no single kind);
    ``urgency`` is the max across the flushed items (the loudest reason the flush fired);
    ``load`` is the sum (the cumulative load that tripped the flush arm).
    """
    return ScoredItem(
        item_id=f"{_DIGEST_PREFIX}-{len(items)}",
        item_kind=ItemKind.GENERIC,
        urgency=max((item.urgency for item in items), default=0.0),
        load=sum(item.load for item in items),
    )


def _digest_queue_item(items: tuple[ScoredItem, ...], carrier: QueueItem) -> QueueItem:
    """Synthesize the digest's carrier ``QueueItem`` whose ``payload`` IS the terse digest.

    Downstream, the compose request is built from ``payload`` alone (Q-50 payload boundary), so
    this is the ONE place the digest content (count + item-kind mix) can reach the mouth as one
    consolidated line. ``item_id`` is carried from ``carrier`` (the queue item that tipped the
    flush arm this cycle) purely for the ack path upstream of this call — it is never re-acked
    here.
    """
    kinds = sorted({item.item_kind.value for item in items})
    kinds_text = ", ".join(kinds) if kinds else "no items"
    summary = f"{len(items)} held item{'s' if len(items) != 1 else ''} ({kinds_text})"
    payload = {"digest_count": len(items), "summary": summary, "item_kinds": kinds}
    return QueueItem(
        idempotency_key=f"{_DIGEST_PREFIX}-{carrier.idempotency_key}",
        payload=payload,
        item_id=carrier.item_id,
    )


class ReviewOrSpeakStage:
    """Branches on ``GateAction``: ack + forward a surface, ack + journal-via-Done a hold."""

    name: str = "review_or_speak"
    transitions: tuple[str, ...] = ("compose_dispatch",)

    def __init__(self, *, queue: _AckableQueue) -> None:
        self._queue = queue

    async def run(self, ctx: StageContext) -> StageResult:
        art = await ctx.last_output("gate")
        if art is None:
            msg = "review_or_speak: no gate output available yet"
            raise RuntimeError(msg)
        entries = gate_decisions_from_artifact_data(art.data)

        surfaced: list[tuple[GateAction, ScoredItem, QueueItem]] = []
        holds: list[dict[str, object]] = []
        for decision, queue_item in entries:
            assert queue_item.item_id is not None, (
                "review_or_speak: a drained queue_item must carry a server-assigned item_id"
            )
            # Decide -> ack -> (eventually) return, per entry (Q-52).
            self._queue.ack(queue_item.item_id)
            if decision.action is GateAction.SURFACE_FLUSH:
                # Q-55: decision.items may be MANY (the whole flushed pending set) — single-ize
                # to ONE synthesized digest surfaced_item, never one compose_request per item.
                digest_scored = _digest_scored_item(decision.items)
                digest_queue_item = _digest_queue_item(decision.items, queue_item)
                surfaced.append((decision.action, digest_scored, digest_queue_item))
            elif decision.action in _SURFACE_ACTIONS:
                # SURFACE_IMMEDIATE: exactly one item, both under the stub and the production
                # Gate (``Gate.pipeline`` only ever returns this action with items=(scored,)).
                scored_item = decision.items[0]
                surfaced.append((decision.action, scored_item, queue_item))
            else:
                hold_scored_item = decision.items[0] if decision.items else None
                holds.append(_hold_record(hold_scored_item, queue_item))

        if surfaced:
            action, scored_item, queue_item = surfaced[0]
            if len(entries) > 1:
                # DEFENSIVE (structurally unreachable at batch_size=1): every entry past the
                # forwarded one is dropped from this StepResult (a StageResult carries exactly
                # one artifact) — log LOUD so nothing goes missing silently.
                for _, _, extra_item in surfaced[1:]:
                    logger.warning(
                        "review_or_speak: defensive >1 batch — surplus surfaced item %r "
                        "acked but NOT forwarded (only the first surfaced entry routes on)",
                        extra_item.idempotency_key,
                    )
                for hold in holds:
                    logger.warning(
                        "review_or_speak: defensive >1 batch — hold item %r acked but its "
                        "reason %r is NOT journaled this step (a surfaced entry took the "
                        "StepResult instead)",
                        hold["item_id"],
                        hold["reason"],
                    )
            return Transition(
                to="compose_dispatch",
                output=Artifact(
                    kind=SURFACED_ITEM,
                    produced_by=self.name,
                    provenance=Provenance(
                        source="system", confidence=1.0, recorded_at=ctx.clock()
                    ),
                    data=surfaced_item_to_artifact_data(action, scored_item, queue_item),
                ),
            )

        return Done(
            output=Artifact(
                kind=HOLD_REPORT,
                produced_by=self.name,
                provenance=Provenance(source="system", confidence=1.0, recorded_at=ctx.clock()),
                data=hold_report_to_artifact_data(holds),
            )
        )


__all__ = ["ReviewOrSpeakStage"]
