"""wombat.sources.registry — SourceRegistry (TK-3, EP-3, Q-36/Q-46, ASMP-2).

Registers ``InputSource`` instances and drives each one's poll loop as its own independent
asyncio task, at its own ``poll_interval_seconds`` (AC3 — two sources with different intervals
are each polled on their own cadence, never the other's). ``start()`` calls each source's
``start()`` then spawns its loop; ``stop()`` cancels every loop task, awaits their completion,
then calls each source's ``stop()`` — so once ``stop()`` returns, no further ``poll()`` will
ever run (AC2).

ASMP-2: the registry is ENQUEUE-ONLY. It never drains the queue — exactly one draining process
exists elsewhere (``DrainQueueStage``); no drain logic lives here. It depends on the injected
``Enqueuer`` seam below rather than on ``WombatQueue`` directly (Q-36/Q-46), so unit tests can
inject a bare stub; the one DB-backed test wires a real ``WombatQueue``.

A source's ``poll()`` raising is caught per-loop-iteration (AC4): the exception is logged with
the source id, the source is added to ``degraded_sources``, and its OWN loop keeps running (a
later poll may recover) — other sources' loops are separate asyncio tasks, so one source's
failure never stops or crashes another's.

CRF-4: the enqueue() calls in the same loop iteration are guarded the same way, riding the
two-arm ``QueueFullError``-then-``Exception`` precedent at ``pattern_detector`` (TK-204/CR3-2) —
a full queue or any enqueue-time error is logged loud naming the source and the dropped event
key, marks the source degraded, and the loop continues (a pull source redelivers on a later
poll; a push-buffered event is honestly dropped-and-logged). The degraded mark only clears on an
iteration where every enqueue in that poll succeeded. ``stop()`` awaits every task inside a
try/except rather than a bare ``CancelledError`` suppress, so a task left dead by a non-cancel
exception can never abort the remaining ``stop()`` iterations or the runtime's teardown
``finally``.

TK-72/Q-59 SANCTIONED RIDER: the interim ``f"{source.id}:{event.event_key}"`` join has been
replaced by TK-12's canonical ``item_identity.idempotency_key(source_id, source_natural_id)``
derivation — the ONE place every dedup path (queue, gate pending-set, outcome binding) agrees
on identity (Q-18/D). This makes TK-12's AC4 one-derivation obligation real at the first real
source (``gcal``, TK-72); registry behavior is otherwise unchanged.

TK-245 (DEC-45(c), ruling v2.68 r6): an OPTIONAL ``sink`` — ``(source_id, events) -> None`` —
is called ONCE per successful poll iteration (a poll() that did not raise), in its OWN
try/except: a sink failure logs exactly ONE WARNING naming the source and never kills the poll
task or touches the enqueue arm above/below it, which stays byte-untouched. The registry stays
enqueue-only toward the queue (ASMP-2) — the sink is a sibling side-write, never a drain; a
``None`` sink (the default) leaves poll behavior byte-unchanged.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Protocol

from wombat.domain.item_identity import idempotency_key as derive_key
from wombat.queue import EnqueueResult, QueueFullError, QueueItem
from wombat.sources.base import InputSource, SourceEvent

_log = logging.getLogger(__name__)

# TK-245: the optional store-write seam a poll iteration's events are handed to, once per
# successful poll — see the module docstring for the full contract.
Sink = Callable[[str, list[SourceEvent]], None]


class Enqueuer(Protocol):
    """The one queue method the registry needs (Q-36/Q-46) — enqueue only, per ASMP-2."""

    def enqueue(self, item: QueueItem) -> EnqueueResult: ...


class SourceRegistry:
    """Registers ``InputSource`` instances and drives each on its own asyncio poll loop."""

    def __init__(self, enqueue: Enqueuer, sink: Sink | None = None) -> None:
        self._enqueue = enqueue
        self._sink = sink
        self._sources: dict[str, InputSource] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._degraded: set[str] = set()

    def register(self, source: InputSource) -> None:
        """Add a source to the registry. Raises ``ValueError`` on a duplicate ``id``."""
        if source.id in self._sources:
            raise ValueError(f"source id already registered: {source.id!r}")
        self._sources[source.id] = source

    @property
    def degraded_sources(self) -> frozenset[str]:
        """Ids of sources whose most recent ``poll()`` raised (AC4)."""
        return frozenset(self._degraded)

    @property
    def source_ids(self) -> frozenset[str]:
        """Ids of every registered source (TK-161, AC2 — registration-not-rewrite, DEC-5).

        Read-only; registration still happens exclusively through ``register()``. Lets a
        caller (e.g. a test) confirm a source was registered without reaching into registry
        internals — no push entry point, no dispatch branch, nothing else changes here.
        """
        return frozenset(self._sources)

    async def start(self) -> None:
        """Start every registered source and spawn its poll loop task."""
        for source in self._sources.values():
            await source.start()
            self._tasks[source.id] = asyncio.create_task(self._poll_loop(source))

    async def stop(self) -> None:
        """Cancel every poll loop task and stop every registered source (AC2)."""
        for task in self._tasks.values():
            task.cancel()
        for source_id, task in self._tasks.items():
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                _log.exception(
                    "source %s: poll loop task raised on teardown; suppressing so stop() "
                    "finishes awaiting the remaining tasks",
                    source_id,
                )
        self._tasks.clear()
        for source in self._sources.values():
            await source.stop()

    async def _poll_loop(self, source: InputSource) -> None:
        """Poll ``source`` forever on its own interval until this task is cancelled."""
        while True:
            try:
                events = await source.poll()
            except Exception:
                _log.exception("source %s: poll() raised; marking degraded", source.id)
                self._degraded.add(source.id)
            else:
                if self._sink is not None:
                    try:
                        self._sink(source.id, events)
                    except Exception:
                        _log.warning(
                            "source %s: sink failed; store write skipped for this poll "
                            "(enqueue is unaffected)",
                            source.id,
                            exc_info=True,
                        )
                iteration_ok = True
                for event in events:
                    try:
                        self._enqueue.enqueue(
                            QueueItem(
                                idempotency_key=derive_key(source.id, event.event_key),
                                payload=event.payload,
                            )
                        )
                    except QueueFullError:
                        _log.error(
                            "source %s: enqueue failed — wombat_queue is at capacity; event "
                            "%r is dropped",
                            source.id,
                            event.event_key,
                            exc_info=True,
                        )
                        self._degraded.add(source.id)
                        iteration_ok = False
                    except Exception:
                        _log.exception(
                            "source %s: enqueue failed unexpectedly; event %r is dropped",
                            source.id,
                            event.event_key,
                        )
                        self._degraded.add(source.id)
                        iteration_ok = False
                if iteration_ok:
                    self._degraded.discard(source.id)
            await asyncio.sleep(source.poll_interval_seconds)


__all__ = ["Enqueuer", "Sink", "SourceRegistry"]
