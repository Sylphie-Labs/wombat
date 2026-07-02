"""The inter-stage Artifact convention (TK-5, Q-47) — defined ONCE, spine-wide.

Stages in the drain pathway hand data downstream ONLY via the journaled ``StageResult.output``
Artifact — never a side channel. ``kind`` is a namespaced dotted string ``wombat.<snake_noun>``
(this module: ``wombat.drained_batch`` / ``wombat.drain_heartbeat``; future stages add e.g.
``wombat.gate_decision``); ``produced_by`` is the producing stage's canonical snake_case ``name``
verbatim, so a downstream stage can pull ``ctx.last_output(produced_by)``.

``queue_items_to_artifact_data`` / ``queue_items_from_artifact_data`` are the ONLY serialization
path for a drained batch between stages — TK-6 deserializes a batch through the inverse helper,
never a hand-rolled dict-unpack, so read and write cannot drift (Q-41 wire-helper principle).
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from wombat.queue import QueueItem

DRAINED_BATCH = "wombat.drained_batch"
DRAIN_HEARTBEAT = "wombat.drain_heartbeat"


def queue_items_to_artifact_data(items: list[QueueItem]) -> dict[str, Any]:
    """Serialize drained ``QueueItem``s into an Artifact ``data`` payload: ``{"items": [...]}``."""
    return {"items": [asdict(item) for item in items]}


def queue_items_from_artifact_data(data: dict[str, Any]) -> list[QueueItem]:
    """The inverse of ``queue_items_to_artifact_data`` — the ONLY path back to ``QueueItem``s."""
    return [QueueItem(**raw) for raw in data["items"]]


__all__ = [
    "DRAINED_BATCH",
    "DRAIN_HEARTBEAT",
    "queue_items_from_artifact_data",
    "queue_items_to_artifact_data",
]
