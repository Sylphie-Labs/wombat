"""wombat.sources.base — the InputSource Protocol (TK-3, EP-3, Q-32).

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
"""

from __future__ import annotations

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


__all__ = ["InputSource", "SourceEvent"]
