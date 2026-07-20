"""SpeakSink — terse local-TTS output for drain surfacings (TK-164, Q-96).

The drain pathway's TERMINAL stage (``transitions=()``), landing after ``compose`` (the
TK-8-reserved flip: ``compose``'s ``Done`` -> ``Transition(to="speak", ...)`` carrying the SAME
``wombat.composed_output`` artifact byte-identical, ``stages/compose.py``). ``SpeakSink`` reads
that artifact via ``ctx.last_output("compose")`` — for item identity (``item_id``/``item_kind``)
ONLY, byte-identical to before.

TK-267 (DEC-55): the drain graph now runs ``compose -> chat_reply -> speech_shape -> speak``.
Iff voice is enabled AND a working TTS adapter is wired, ``SpeakSink`` speaks
``SpeechShapeStage``'s shaped summary (``ctx.last_output("speech_shape")``) — NEVER the composed
text (DEC-55c never-verbatim). A degraded or absent ``speech_shape`` output (voice-off/no-adapter
pass-throughs never reach this branch at all, since the voice-off check below still gates first)
degrades THIS stage to the existing terminal ``Degraded(spoken=False, degraded=True, to=None)``
shape, naming ``speech_shape`` in the reason — the mouth failing never means silence turns into
speaking the raw composed text instead.

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

DEC-57/TK-272: a SURFACED chat item reaches this sink exactly as any other item does (a real
``compose`` -> ``speech_shape`` -> ``speak`` run). A HELD chat item ALSO reaches ``compose`` (Q-50
routing is unaffected by hold-vs-surface) but carries ``held_chat=True`` on the composed-output
artifact; this sink folds that flag into the SAME voice-off branch above — quiet-by-design, never
the speech-text-None ``Degraded`` outcome below.

TK-279 (DEC-60b, supersedes DEC-57 IN PART — voice origin only): the silent branch's condition
becomes ``held_chat and not voice_turn`` (``voice_turn`` read off the SAME composed-output
artifact this sink already reads) — an exact lock-step mirror of ``SpeechShapeStage``'s own gate.
A held reply to a SPOKEN turn falls through to the existing ``speech_shape`` read below exactly
as a surfaced item would (no new branch); a shaping failure/rejection still hits the existing
speech-text-None ``Degraded`` path, never speaking the raw composed text (DEC-55c).
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
    composed_output_held_chat_from_artifact_data,
    composed_output_voice_turn_from_artifact_data,
    speech_output_from_artifact_data,
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
        _composed_text, item_id, item_kind, _degraded = composed_output_from_artifact_data(art.data)
        held_chat = composed_output_held_chat_from_artifact_data(art.data)
        voice_turn = composed_output_voice_turn_from_artifact_data(art.data)

        if not self._voice_enabled or self._adapter is None or (held_chat and not voice_turn):
            # Voice-off (or no adapter wired) is a silent no-op — the text path is unaffected;
            # voice is additive only (CON-3). Byte-identical to before TK-267 (speech_shape is
            # never even consulted on this branch). DEC-57/TK-272: a held chat reply takes this
            # SAME silent Done(spoken=False, degraded=False) shape — quiet-by-design is not
            # degradation, so it must NEVER fall through to the speech-text-None Degraded branch
            # below.
            return Done(
                output=Artifact(
                    kind=SPOKEN_OUTPUT,
                    produced_by=self.name,
                    provenance=Provenance(source="system", confidence=1.0, recorded_at=ctx.clock()),
                    data=spoken_output_to_artifact_data(
                        item_id=item_id, item_kind=item_kind, spoken=False, degraded=False
                    ),
                )
            )

        # TK-267 (DEC-55): the TEXT actually spoken is SpeechShapeStage's shaped summary — NEVER
        # the composed text (DEC-55c never-verbatim). A degraded/absent speech_shape output
        # degrades THIS stage, naming speech_shape, rather than falling back to composed text.
        speech_art = await ctx.last_output("speech_shape")
        speech_text: str | None = None
        if speech_art is not None:
            _sp_item_id, _sp_item_kind, speech_text, _sp_degraded = (
                speech_output_from_artifact_data(speech_art.data)
            )

        if speech_text is None:
            logger.warning(
                "speak: speech_shape produced no speech text; degrading (text delivery unaffected)"
            )
            return Degraded(
                reason="speak: speech_shape produced no speech text (degraded or absent output)",
                output=Artifact(
                    kind=SPOKEN_OUTPUT,
                    produced_by=self.name,
                    provenance=Provenance(source="system", confidence=1.0, recorded_at=ctx.clock()),
                    data=spoken_output_to_artifact_data(
                        item_id=item_id, item_kind=item_kind, spoken=False, degraded=True
                    ),
                ),
                to=None,
            )

        try:
            self._adapter.speak(speech_text)
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
                    provenance=Provenance(source="system", confidence=1.0, recorded_at=ctx.clock()),
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
