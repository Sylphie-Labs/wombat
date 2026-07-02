"""DrainQueueStage — the FIRST cog-worx Stage in the drain pathway (TK-5, EP-4, Q-47).

Pulls a bounded batch off the injected queue each cycle and hands it downstream to the gate
(``to="gate"`` — a forward string reference; TK-6 builds that stage) via a journaled Artifact
(the convention lives once in ``stages/artifacts.py``) — the pathway never sees scoring logic
(DEC-14, DEC-8). An empty queue re-parks the stage on itself with a cog-worx ``Wait`` heartbeat
(DEC-8 idles-on-empty); ``wake_at`` is always derived from ``ctx.clock()``, never wall-clock, so
engine-driven tests stay deterministic.

This stage NEVER acks (ack is TK-7's job on hold/completion) and NEVER writes the journal (the
engine journals the ``StageResult`` itself) — it touches ONLY ``ctx.clock()`` on the
``StageContext``; the queue is an injected constructor dependency, not part of ``ctx`` (Q-47).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Protocol

from cogworx.claims.provenance import Artifact, Provenance
from cogworx.loop.result import StageResult, Transition, Wait
from cogworx.loop.stage import StageContext

from wombat.queue import QueueItem
from wombat.stages.artifacts import (
    DRAIN_HEARTBEAT,
    DRAINED_BATCH,
    queue_items_to_artifact_data,
)


class _DrainableQueue(Protocol):
    """The one queue method DrainQueueStage needs — a structural seam so tests can inject a bare
    stub instead of a real ``WombatQueue`` (which the composition root passes per ASMP-2)."""

    def drain(self, limit: int | None = None) -> list[QueueItem]: ...


class DrainQueueStage:
    """Pulls up to ``batch_size`` items off the injected queue each cycle."""

    name: str = "drain_queue"
    # Both real edges MUST be declared so a real-Engine route guard accepts BOTH results this
    # stage returns: Transition(to="gate") on a non-empty drain AND Wait(to="drain_queue") (self)
    # on an empty queue (the idle heartbeat re-parks the stage on itself, DEC-8). Q-53 rider.
    transitions: tuple[str, ...] = ("gate", "drain_queue")

    def __init__(
        self,
        queue: _DrainableQueue,
        batch_size: int,
        poll_interval_seconds: float,
    ) -> None:
        self._queue = queue
        self._batch_size = batch_size
        self._poll_interval_seconds = poll_interval_seconds

    async def run(self, ctx: StageContext) -> StageResult:
        items = self._queue.drain(limit=self._batch_size)

        if items:
            return Transition(
                to="gate",
                output=Artifact(
                    kind=DRAINED_BATCH,
                    produced_by=self.name,
                    provenance=Provenance(
                        source="system", confidence=1.0, recorded_at=ctx.clock()
                    ),
                    data=queue_items_to_artifact_data(items),
                ),
            )

        return Wait(
            to=self.name,
            wake_at=ctx.clock() + timedelta(seconds=self._poll_interval_seconds),
            output=Artifact(
                kind=DRAIN_HEARTBEAT,
                produced_by=self.name,
                provenance=Provenance(source="system", confidence=1.0, recorded_at=ctx.clock()),
                data={},
            ),
        )


__all__ = ["DrainQueueStage"]
