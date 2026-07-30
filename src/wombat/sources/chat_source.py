"""wombat.sources.chat_source — ChatSource: the chat input surface's PushSource (TK-222, EP-32,
Q-110(d)).

Per DEC-39(1): chat is a REAL input source, never a side-channel to the model (CON-1). This
module is deliberately trivial — a bare ``PushSource`` (TK-161, Q-86) registered under id
``"chat"`` at a fixed poll cadence, exactly like every other source
(``sources.registry.SourceRegistry``). ALL the message-shaping work (minting ``event_key``,
building the ``payload``, pre-computing the expected item id) lives in ``wombat.chat.surface``,
which owns the loopback HTTP transport and calls ``push()`` on an instance of this class — this
module itself never touches HTTP, JSON parsing, or auth.

STRUCTURAL (CON-1): this module imports NOTHING beyond ``sources.base`` — no model/compose/mouth
module is reachable from here, so the mouth can never see a correlation id or reach back into the
chat transport.

TK-269 (DEC-56a): ``poll()`` is overridden to fire an optional ``wake`` callable (set by
``wombat.runtime._drive_and_serve`` once the running loop's ``DrainWake`` exists — this instance
is constructed too early in ``assemble_runtime`` to have it) right after draining the buffer, IFF
that drain returned at least one event. The wake fires INSIDE ``poll()``, before the registry's
own enqueue loop runs, but this is still safe (v2.113 ruling): ``asyncio.Event.set()`` only marks
a waiting task ready, it never itself yields control — the pump can only actually observe the
wake once THIS coroutine chain yields, and ``sources.registry.SourceRegistry._poll_loop`` has no
await point between ``poll()`` returning and its synchronous sink-call + enqueue loop finishing
(the next await is its own ``asyncio.sleep`` afterward). So by the time the pump task is actually
scheduled, the queue write has already committed — single-event-loop atomicity, not a race.

TK-296 (DEC-65f, RULING r3 v2.159): an optional ctor kwarg ``context_hook`` — a callable returning
extra str-to-str fields — held as a PUBLIC attribute (the ``wake`` precedent above, unlike
``sources.asr.ASRSource``'s own PRIVATE ``_context_hook``): ``wombat.chat.surface.ChatSurface.
_accept_message`` reads ``source.context_hook`` directly at payload-build time and merges its
returned mapping UNDER the built-in ``item_kind``/``text``/``received_at`` fields — this module
itself never calls it. ``None`` (the default) leaves every existing behavior byte-identical.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from wombat.sources.base import PushSource, SourceEvent

# The canonical registered source id — surface.py reads it off the constructed instance
# (``source.id``) rather than hardcoding it a second time, so the two never drift.
CHAT_SOURCE_ID = "chat"

# TK-222 (Q-110(d)): a fixed, deliberately short poll cadence — chat is an interactive surface
# (a human is waiting on a held HTTP connection), not a background poller like gcal/gmail/asr.
CHAT_POLL_INTERVAL_SECONDS = 1.0


class ChatSource(PushSource):
    """The chat surface's registered ``InputSource`` (Q-110(d) ruling 1). A bare ``PushSource``
    under id ``"chat"`` — no behavior of its own beyond what ``PushSource`` already provides."""

    def __init__(self, *, context_hook: Callable[[], Mapping[str, str]] | None = None) -> None:
        super().__init__(id=CHAT_SOURCE_ID, poll_interval_seconds=CHAT_POLL_INTERVAL_SECONDS)
        # TK-269 (DEC-56a): the drain pump's wake callable — ``None`` (today's behavior, no wake)
        # until ``wombat.runtime._drive_and_serve`` sets it on the running loop. A default-None
        # plain attribute (not a constructor arg) because assemble_runtime constructs this
        # instance well before that loop-bound wake exists.
        self.wake: Callable[[], None] | None = None
        # TK-296 (DEC-65f, RULING r3 v2.159): PUBLIC (mirrors ``wake`` above) so ``chat.surface.
        # ChatSurface._accept_message`` can read it directly — see the module docstring.
        self.context_hook = context_hook

    async def poll(self) -> list[SourceEvent]:
        """``PushSource.poll()`` plus one thing: if it drained >=1 event, fire ``self.wake`` (see
        the module docstring for why doing so INSIDE this call, before the registry's enqueue
        loop runs, is still safe)."""
        events = await super().poll()
        if events and self.wake is not None:
            self.wake()
        return events


__all__ = ["CHAT_POLL_INTERVAL_SECONDS", "CHAT_SOURCE_ID", "ChatSource"]
