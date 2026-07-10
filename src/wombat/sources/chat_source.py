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
"""

from __future__ import annotations

from wombat.sources.base import PushSource

# The canonical registered source id — surface.py reads it off the constructed instance
# (``source.id``) rather than hardcoding it a second time, so the two never drift.
CHAT_SOURCE_ID = "chat"

# TK-222 (Q-110(d)): a fixed, deliberately short poll cadence — chat is an interactive surface
# (a human is waiting on a held HTTP connection), not a background poller like gcal/gmail/asr.
CHAT_POLL_INTERVAL_SECONDS = 1.0


class ChatSource(PushSource):
    """The chat surface's registered ``InputSource`` (Q-110(d) ruling 1). A bare ``PushSource``
    under id ``"chat"`` — no behavior of its own beyond what ``PushSource`` already provides."""

    def __init__(self) -> None:
        super().__init__(id=CHAT_SOURCE_ID, poll_interval_seconds=CHAT_POLL_INTERVAL_SECONDS)


__all__ = ["CHAT_POLL_INTERVAL_SECONDS", "CHAT_SOURCE_ID", "ChatSource"]
