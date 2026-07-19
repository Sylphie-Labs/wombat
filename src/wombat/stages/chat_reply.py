"""ChatReplyStage — delivers a composed item's text back off the chat reply broker (TK-222,
EP-32, Q-110(d)).

The NEW hop ``ComposeStage`` (``stages/compose.py``) transitions through before ``speak``:
``compose`` -> ``chat_reply`` -> ``speak``. Reads the SAME ``wombat.composed_output`` artifact
``compose`` produced (via ``ctx.last_output("compose")``, byte-identical to what ``SpeakSink``
itself reads) and calls ``broker.resolve(item_id, text)`` — a GUARDED, NEVER-RAISE call: any
exception the broker raises is caught, logged as ONE loud WARNING, and degrades to
``delivered=False``; ``asyncio.CancelledError`` is re-raised explicitly, ahead of the broad
except (mirrors ``ComposeStage``/``SpeakSink``'s own cancellation discipline).

Every ``compose``-composed item hops through here now, not just chat ones — ``ChatReplyBroker.
resolve`` is a documented no-op for an item id it never registered (``chat.surface``), so a
non-chat item (``ItemKind.GENERIC`` phrased by the same mouth) harmlessly resolves nothing and
``delivered`` is simply ``False`` for it. Constructed with ``broker=None`` is a PURE PASS-THROUGH
— no resolve attempted at all, ``delivered=False`` always — the shape a chat-disabled boot wires
(``bootstrap.assemble_runtime``, TK-222 ruling 5's loud-skip).

TK-267 (DEC-55) inserts a NEW ``speech_shape`` hop between this stage and ``speak``: ``compose`` ->
``chat_reply`` -> ``speech_shape`` -> ``speak``. ``run()`` ALWAYS returns
``Transition(to="speech_shape", ...)`` — even on a broker failure or when no broker is wired — so
``SpeakSink`` (which reads ``ctx.last_output("compose")``/``ctx.last_output("speech_shape")`` BY
STAGE NAME, not the immediately-preceding stage) is completely unaffected by this hop's insertion
(byte-identical text-channel delivery either way; the composed text this stage resolves to the chat
broker stays the FULL, byte-identical composed text — shaping happens only downstream, for the
spoken channel).

``wombat.chat_delivery`` is a small wire LOCAL to this module (``{"item_id", "delivered"}``) —
nothing downstream consumes it, so it doesn't join the shared ``stages/artifacts.py`` convention;
it exists only so the journal carries a typed record of what this hop did.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from cogworx.claims.provenance import Artifact, Provenance
from cogworx.loop.result import StageResult, Transition
from cogworx.loop.stage import StageContext

from wombat.chat.surface import ChatReplyBroker
from wombat.stages.artifacts import composed_output_from_artifact_data

logger = logging.getLogger(__name__)

CHAT_DELIVERY = "wombat.chat_delivery"


def chat_delivery_to_artifact_data(*, item_id: str, delivered: bool) -> dict[str, Any]:
    """Serialize this stage's terminal output into an Artifact ``data`` payload."""
    return {"item_id": item_id, "delivered": delivered}


def chat_delivery_from_artifact_data(data: dict[str, Any]) -> tuple[str, bool]:
    """The inverse of ``chat_delivery_to_artifact_data`` — the ONLY path back."""
    return data["item_id"], data["delivered"]


class ChatReplyStage:
    """Resolves a composed item's text back to any HTTP connection awaiting it, then always
    passes through to ``speak`` (TK-222)."""

    name: str = "chat_reply"
    # TK-267 (DEC-55): the onward edge is now "speech_shape" (a new hop that produces the spoken
    # summary) rather than "speak" directly — see the module docstring.
    transitions: tuple[str, ...] = ("speech_shape",)

    def __init__(self, *, broker: ChatReplyBroker | None) -> None:
        self._broker = broker

    async def run(self, ctx: StageContext) -> StageResult:
        art = await ctx.last_output("compose")
        if art is None:
            msg = "chat_reply: no compose output available yet"
            raise RuntimeError(msg)
        text, item_id, _item_kind, _degraded = composed_output_from_artifact_data(art.data)

        delivered = False
        if self._broker is not None:
            try:
                self._broker.resolve(item_id, text)
                delivered = True
            except asyncio.CancelledError:
                # Never swallow cancellation — only the broker's own failures degrade.
                raise
            except Exception:
                logger.warning(
                    "chat_reply: broker.resolve failed for item_id=%r; degrading to "
                    "delivered=False (voice/text delivery via speak is unaffected)",
                    item_id,
                    exc_info=True,
                )
                delivered = False

        return Transition(
            to="speech_shape",
            output=Artifact(
                kind=CHAT_DELIVERY,
                produced_by=self.name,
                provenance=Provenance(source="system", confidence=1.0, recorded_at=ctx.clock()),
                data=chat_delivery_to_artifact_data(item_id=item_id, delivered=delivered),
            ),
        )


__all__ = [
    "CHAT_DELIVERY",
    "ChatReplyStage",
    "chat_delivery_from_artifact_data",
    "chat_delivery_to_artifact_data",
]
