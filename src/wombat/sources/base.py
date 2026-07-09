"""wombat.sources.base — the InputSource Protocol (TK-3, EP-3, Q-32) + PushSource (TK-161, Q-86).

Q-32: this is wombat's OWN input-source contract — it is NOT the cog-worx ``SourceRegistry``
class. Namesake only; the two are unrelated types.

``InputSource`` is poll-only (non_goal: no push/webhook sources for v1). Each source owns a
stable ``id`` (used for degrade/log tracking and as half of the enqueue idempotency key) and its
own ``poll_interval_seconds`` (AC3: each source is polled on its own, independently configured,
cadence). ``start``/``stop`` are the lifecycle hooks the ``SourceRegistry`` calls exactly once
each, bracketing the poll loop it drives for this source.

``poll()`` returns a list of ``SourceEvent`` — zero or more events observed since the previous
poll. The event -> ``QueueItem`` mapping is deliberately trivial: the registry combines the
source's ``id`` with the event's own ``event_key`` to form the ``QueueItem.idempotency_key``,
and passes ``payload`` through as the ``QueueItem.payload``. Canonical cross-source item
identity is TK-12's job, not this ticket's — no identity machinery lives here.

``PushSource`` (TK-161, Q-86 ruling) is the uniform push-over-poll mechanism every push-shaped
source (ASR TK-162, feedback TK-51) rides, WITHOUT touching this poll-only contract or the
registry's poll loop: it satisfies ``InputSource`` by buffering pushed events in an internal
deque and draining them on its next ``poll()`` tick — push-over-poll, not push-instead-of-poll.
The registry stays structurally unchanged; it only ever calls ``poll()``.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class SourceEvent:
    """One event yielded by ``InputSource.poll()``.

    ``event_key`` is scoped to the source that produced it (the registry combines it with the
    source's ``id`` to form the queue's ``idempotency_key``); ``payload`` becomes the
    ``QueueItem.payload`` as-is.
    """

    event_key: str
    payload: dict[str, Any]


class InputSource(Protocol):
    """wombat's input-source contract. Poll-based only (no push/webhook — non_goal)."""

    id: str
    poll_interval_seconds: float

    async def start(self) -> None:
        """Called once by the registry before this source's poll loop begins."""
        ...

    async def stop(self) -> None:
        """Called once by the registry after this source's poll loop has been cancelled."""
        ...

    async def poll(self) -> list[SourceEvent]:
        """Return zero or more events observed since the previous poll."""
        ...


class PushSource:
    """A concrete ``InputSource`` for push-shaped producers (TK-161, Q-86 ruling).

    ``push()`` appends a ``SourceEvent`` to an internal FIFO deque; ``poll()`` drains and
    returns everything buffered since the previous call, in push order. This is
    push-OVER-poll, not push-instead-of-poll: a pushed event only reaches the queue on this
    source's NEXT poll tick, driven by the registry exactly like every other source — the
    registry is never touched and never learns this source is push-backed.

    ``start``/``stop`` are no-ops: there is no background task or connection for a push
    source to bracket a lifecycle around; the caller pushing events owns its own lifecycle.

    Thread-safety (AC3): ``push()`` is a plain ``deque.append`` — safe for the v1
    single-event-loop runtime (append is atomic under the GIL and there is exactly one
    consumer, this source's own ``poll()``, running on that same loop). Pushing from a
    thread OTHER than the one running the event loop is out of scope for v1.
    """

    __slots__ = ("_buffer", "id", "poll_interval_seconds")

    def __init__(self, id: str, poll_interval_seconds: float) -> None:
        self.id = id
        self.poll_interval_seconds = poll_interval_seconds
        self._buffer: deque[SourceEvent] = deque()

    def push(self, event: SourceEvent) -> None:
        """Buffer ``event`` for delivery on this source's next ``poll()`` tick."""
        self._buffer.append(event)

    async def start(self) -> None:
        """No-op: a push source has no lifecycle of its own to start."""

    async def stop(self) -> None:
        """No-op: a push source has no lifecycle of its own to stop."""

    async def poll(self) -> list[SourceEvent]:
        """Drain and return every event pushed since the previous ``poll()``, in push order."""
        drained = list(self._buffer)
        self._buffer.clear()
        return drained


__all__ = ["InputSource", "PushSource", "SourceEvent"]
