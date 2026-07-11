"""DrainQueueStage — the FIRST cog-worx Stage in the drain pathway (TK-5, EP-4, Q-47).

Pulls a bounded batch off the injected queue each cycle and hands it downstream to the gate
(``to="gate"`` — a forward string reference; TK-6 builds that stage) via a journaled Artifact
(the convention lives once in ``stages/artifacts.py``) — the pathway never sees scoring logic
(DEC-14, DEC-8).

TK-230 (CRF-2, DEC-41): an empty queue returns cog-worx ``Done`` (a ``wombat.drain_heartbeat``
Artifact) instead of self-parking on a ``Wait``. The old self-park pattern was a structural bug:
once a run reaches ANY terminal result the engine cancels every timer for that run
(``cogworx.runtime.engine`` 855-857), and the Sweeper only ever re-drives a WAITING/RETRYING run
(``fire_timer``) — so a "self-park on empty, resume on the next Sweeper beat" idle heartbeat can
never actually be woken again once the run is Done, stranding anything enqueued afterward until a
process restart. The stage itself NEVER parks any more; DEC-8's idles-on-empty guarantee is now
realized by ``wombat.runtime``'s drain pump (TK-230) simply starting ZERO fresh runs on a beat
where ``WombatQueue.pending_count()`` reads zero, rather than by this stage waiting in place.

``poll_interval_seconds`` is kept as a constructor parameter ONLY so callers (``bootstrap.py``)
need no signature change — it is now UNUSED by this stage; the drain cadence is owned entirely by
the runtime pump's own beat interval.

This stage NEVER acks (ack is TK-7's job on hold/completion) and NEVER writes the journal (the
engine journals the ``StageResult`` itself) — it touches ONLY ``ctx.clock()`` on the
``StageContext``; the queue is an injected constructor dependency, not part of ``ctx`` (Q-47).
"""

from __future__ import annotations

from typing import Protocol

from cogworx.claims.provenance import Artifact, Provenance
from cogworx.loop.result import Done, StageResult, Transition
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
    # The ONE real edge: Transition(to="gate") on a non-empty drain. An empty drain returns Done
    # (no "to" — TK-230/DEC-41), so no self-edge is declared any more.
    transitions: tuple[str, ...] = ("gate",)

    def __init__(
        self,
        queue: _DrainableQueue,
        batch_size: int,
        poll_interval_seconds: float,
    ) -> None:
        self._queue = queue
        self._batch_size = batch_size
        self._poll_interval_seconds = poll_interval_seconds  # unused (TK-230): see module docstring

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

        return Done(
            output=Artifact(
                kind=DRAIN_HEARTBEAT,
                produced_by=self.name,
                provenance=Provenance(source="system", confidence=1.0, recorded_at=ctx.clock()),
                data={},
            ),
        )


__all__ = ["DrainQueueStage"]
