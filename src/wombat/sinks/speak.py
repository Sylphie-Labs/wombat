"""SpeakSink — terse local-TTS output for drain surfacings (TK-164, Q-96).

The drain pathway's new TERMINAL stage (``transitions=()``), landing after ``compose`` (the
TK-8-reserved flip: ``compose``'s ``Done`` -> ``Transition(to="speak", ...)`` carrying the SAME
``wombat.composed_output`` artifact byte-identical, ``stages/compose.py``). ``SpeakSink`` reads
that artifact via ``ctx.last_output("compose")`` and, iff voice is enabled AND a working TTS
adapter is wired, speaks the composed text VERBATIM exactly once.

HOLD-SILENCE IS STRUCTURAL (Q-96/ISS-4): a held item is acked at ``ReviewOrSpeakStage`` and never
routed to ``compose``/``speak`` at all — this stage never inspects a ``GateAction`` (ISS-4's
one-vocabulary rule: speak-vs-text is a SINK concern keyed only on ``voice_enabled`` + adapter
availability, never a second gate-decision read).

Voice is ADDITIVE (CON-3): a TTS adapter failure (lazy import, engine init, or ``speak()`` itself
raising) never threatens the already-composed/journaled output — ``run()`` degrades to
``Degraded(reason, output=<spoken_output artifact, spoken=False, degraded=True>, to=None)``
rather than raising. Per ``cogworx.loop.result``, ``Degraded(to=None)`` is TERMINAL — the engine
ends the run exactly as it would on ``Done`` (``runtime/engine.py``). ``run()`` NEVER raises for a
voice reason; ``asyncio.CancelledError`` is re-raised explicitly, ahead of the broad except
(mirrors ``ComposeStage``'s own cancellation discipline).

No model call, no ``ctx.journal`` touch, no ``StageToolPolicy`` — ``ctx`` surface is exactly
``ctx.last_output("compose")`` + ``ctx.clock`` (provenance only).
"""

from __future__ import annotations

import asyncio
import logging

from cogworx.claims.provenance import Artifact, Provenance
from cogworx.loop.result import Degraded, Done, StageResult
from cogworx.loop.stage import StageContext

from wombat.sinks.tts_adapter import TTSAdapter
from wombat.stages.artifacts import (
    SPOKEN_OUTPUT,
    composed_output_from_artifact_data,
    spoken_output_to_artifact_data,
)

logger = logging.getLogger(__name__)


class SpeakSink:
    """Terminal drain-graph stage: speaks the composed text via TTS, iff voice is enabled and an
    adapter is available (TK-164, Q-96)."""

    name: str = "speak"
    transitions: tuple[str, ...] = ()

    def __init__(self, *, voice_enabled: bool, adapter: TTSAdapter | None) -> None:
        self._voice_enabled = voice_enabled
        self._adapter = adapter

    async def run(self, ctx: StageContext) -> StageResult:
        art = await ctx.last_output("compose")
        if art is None:
            msg = "speak: no compose output available yet"
            raise RuntimeError(msg)
        text, item_id, item_kind, _degraded = composed_output_from_artifact_data(art.data)

        if not self._voice_enabled or self._adapter is None:
            # Voice-off (or no adapter wired) is a silent no-op — the text path is unaffected;
            # voice is additive only (CON-3).
            return Done(
                output=Artifact(
                    kind=SPOKEN_OUTPUT,
                    produced_by=self.name,
                    provenance=Provenance(
                        source="system", confidence=1.0, recorded_at=ctx.clock()
                    ),
                    data=spoken_output_to_artifact_data(
                        item_id=item_id, item_kind=item_kind, spoken=False, degraded=False
                    ),
                )
            )

        try:
            self._adapter.speak(text)
        except asyncio.CancelledError:
            # Never swallow cancellation — only the adapter's own failures degrade.
            raise
        except Exception as exc:
            # ANY adapter failure (lazy import, engine init, or speak() itself raising) degrades
            # rather than raises: the already-composed/journaled output stands (CON-3).
            logger.warning(
                "speak: TTS adapter failed; degrading (text delivery unaffected)", exc_info=True
            )
            return Degraded(
                reason=f"speak: TTS adapter failed: {exc}",
                output=Artifact(
                    kind=SPOKEN_OUTPUT,
                    produced_by=self.name,
                    provenance=Provenance(
                        source="system", confidence=1.0, recorded_at=ctx.clock()
                    ),
                    data=spoken_output_to_artifact_data(
                        item_id=item_id, item_kind=item_kind, spoken=False, degraded=True
                    ),
                ),
                to=None,
            )

        return Done(
            output=Artifact(
                kind=SPOKEN_OUTPUT,
                produced_by=self.name,
                provenance=Provenance(source="system", confidence=1.0, recorded_at=ctx.clock()),
                data=spoken_output_to_artifact_data(
                    item_id=item_id, item_kind=item_kind, spoken=True, degraded=False
                ),
            )
        )


__all__ = ["SpeakSink"]
