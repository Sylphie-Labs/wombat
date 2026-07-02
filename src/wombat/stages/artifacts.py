"""The inter-stage Artifact convention (TK-5/TK-6, Q-47/Q-48) — defined ONCE, spine-wide.

Stages in the drain pathway hand data downstream ONLY via the journaled ``StageResult.output``
Artifact — never a side channel. ``kind`` is a namespaced dotted string ``wombat.<snake_noun>``
(``wombat.drained_batch`` / ``wombat.drain_heartbeat`` / ``wombat.gate_decisions``);
``produced_by`` is the producing stage's canonical snake_case ``name`` verbatim, so a downstream
stage can pull ``ctx.last_output(produced_by)``.

``queue_items_to_artifact_data`` / ``queue_items_from_artifact_data`` are the ONLY serialization
path for a drained batch between stages — the gate stage deserializes a batch through the inverse
helper, never a hand-rolled dict-unpack, so read and write cannot drift (Q-41 wire-helper
principle). ``gate_decisions_to_artifact_data`` / ``gate_decisions_from_artifact_data`` are the
same convention applied to the gate's own output (Q-48): each entry carries the ORIGINAL Q-47
queue-item dict alongside the decision so TK-7 can ack holds without a second lookup.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from wombat.gate.models import GateAction, GateDecision, ItemKind, ScoredItem
from wombat.queue import QueueItem

DRAINED_BATCH = "wombat.drained_batch"
DRAIN_HEARTBEAT = "wombat.drain_heartbeat"
GATE_DECISIONS = "wombat.gate_decisions"

# One gate decision paired with the original queue item it was derived from (TK-7 acks holds off
# the carried queue_item dict, so the pairing travels together through the wire helpers).
GateDecisionEntry = tuple[GateDecision, QueueItem]


def queue_items_to_artifact_data(items: list[QueueItem]) -> dict[str, Any]:
    """Serialize drained ``QueueItem``s into an Artifact ``data`` payload: ``{"items": [...]}``."""
    return {"items": [asdict(item) for item in items]}


def queue_items_from_artifact_data(data: dict[str, Any]) -> list[QueueItem]:
    """The inverse of ``queue_items_to_artifact_data`` — the ONLY path back to ``QueueItem``s."""
    return [QueueItem(**raw) for raw in data["items"]]


def gate_decisions_to_artifact_data(entries: list[GateDecisionEntry]) -> dict[str, Any]:
    """Serialize gate decisions into an Artifact ``data`` payload (Q-48).

    ``{"decisions": [{"action": <GateAction value str>, "scored_item": <ScoredItem asdict>,
    "queue_item": <the original Q-47 queue-item dict>}, ...]}`` — one entry per queue item,
    each carrying its original queue-item dict so TK-7 can ack holds.
    """
    decisions: list[dict[str, Any]] = []
    for decision, queue_item in entries:
        assert len(decision.items) == 1, "stub_evaluate always emits exactly one ScoredItem"
        scored_item = decision.items[0]
        # Artifact.data must be plain-JSON-native (Q-49): enums go on the wire as their .value,
        # never as the enum MEMBER. asdict() leaves item_kind as the ItemKind member, so overwrite
        # it with .value — mirroring how "action" already stores GateAction.value.
        scored_dict = asdict(scored_item)
        scored_dict["item_kind"] = scored_item.item_kind.value
        decisions.append(
            {
                "action": decision.action.value,
                "scored_item": scored_dict,
                "queue_item": asdict(queue_item),
            }
        )
    return {"decisions": decisions}


def gate_decisions_from_artifact_data(data: dict[str, Any]) -> list[GateDecisionEntry]:
    """The inverse of ``gate_decisions_to_artifact_data`` — the ONLY path back (Q-48)."""
    entries: list[GateDecisionEntry] = []
    for raw in data["decisions"]:
        scored_raw = raw["scored_item"]
        scored_item = ScoredItem(
            item_id=scored_raw["item_id"],
            item_kind=ItemKind(scored_raw["item_kind"]),
            urgency=scored_raw["urgency"],
            load=scored_raw["load"],
        )
        decision = GateDecision(action=GateAction(raw["action"]), items=(scored_item,))
        queue_item = QueueItem(**raw["queue_item"])
        entries.append((decision, queue_item))
    return entries


__all__ = [
    "DRAINED_BATCH",
    "DRAIN_HEARTBEAT",
    "GATE_DECISIONS",
    "GateDecisionEntry",
    "gate_decisions_from_artifact_data",
    "gate_decisions_to_artifact_data",
    "queue_items_from_artifact_data",
    "queue_items_to_artifact_data",
]
