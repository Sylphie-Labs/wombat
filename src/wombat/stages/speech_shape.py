"""SpeechShapeStage — the speech-shaped DeepSeek summary hop between ``chat_reply`` and ``speak``
(TK-267, DEC-55).

Jim's directive (DEC-55): the spoken channel gets a plain-English summary produced by ONE
DeepSeek call; TTS never receives formatted/composed text; the text channel (journal, chat pane,
brief) is byte-untouched. The live drain graph becomes ``compose -> chat_reply -> speech_shape ->
speak`` — this stage is the NEW hop, inserted AFTER ``chat_reply`` (which still resolves the FULL
composed text to the chat broker, upstream of any shaping).

``run()`` reads the SAME ``wombat.composed_output`` artifact ``compose`` produced (via
``ctx.last_output("compose")``, mirroring ``chat_reply``/``SpeakSink``'s own read) and, iff voice
is enabled AND a TTS adapter is available, calls ``ctx.model.complete`` ONCE (the same TK-8 mouth
pattern as ``ComposeStage``: a bounded ``asyncio.wait_for``, never-raise, degrade-on-any-failure)
to produce a plain spoken-English summary. Voice-off or no adapter is a ZERO-model-call
pass-through — the voice-off outcome downstream at ``speak`` stays byte-identical to today.

TK-9 layer 2 (Q-68) rides along exactly as it does for ``ComposeStage``: an optional
``spend_ledger``/``daily_token_ceiling`` gate the call PRE-call (fail-closed to no speech at/over
the ceiling, or on a ledger read failure) and account POST-call (a ledger write failure only logs
loud — the already-produced speech output stands).

NO-PLACEBO VALIDATION (DEC-55f): ``_shape_speech_text`` is a pure function over the model's raw
text — ANY of the enumerated closed token classes (bold/italic markers, heading ``#``, backticks/
code fences, ``[text](url)`` links, raw ``http(s)://`` URLs, list markers) or an overlong response
REJECTS the whole text outright (never a partially-stripped rewrite — a rewrite risks producing
mangled, ungrammatical speech that never existed in anyone's actual output). Rejection, a blank
response, or the model call itself raising/timing out ALL produce the SAME outcome: no speech
text, ``degraded=True`` — NEVER a fallback to the composed text (DEC-55c never-verbatim; there is
no template/verbatim fallback for this mouth, unlike ``ComposeStage``'s degrade-to-template).

NO fifth 'speech' persona mouth (DEC-55e, deferred): the prompt is a FIXED module constant with
``persona.expression.guard_suffix(Mouth.COMPOSE)`` appended verbatim (a read-only import) — zero
persona-package diff.

DEC-57/TK-272: when the composed-output artifact carries ``held_chat=True`` (a chat item the gate
held for voice purposes only), ``run()`` takes the EXACT SAME voice-off pass-through shape as
today — ZERO model calls, ``speech_text=None``, ``degraded=False`` — regardless of
``voice_enabled``/``adapter_present``. A held chat reply is quiet by design, never degraded.
"""

from __future__ import annotations

import asyncio
import logging
import re

from cogworx.claims.provenance import Artifact, Provenance
from cogworx.loop.result import StageResult, Transition
from cogworx.loop.stage import StageContext
from cogworx.model.base import ChatMessage

from wombat.config import ConfigurationError, WombatConfig
from wombat.cost.daily_spend_ledger import DailySpendLedger
from wombat.persona.builder import Mouth
from wombat.persona.expression import guard_suffix
from wombat.stages.artifacts import (
    SPEECH_OUTPUT,
    composed_output_from_artifact_data,
    composed_output_held_chat_from_artifact_data,
    speech_output_to_artifact_data,
)

logger = logging.getLogger(__name__)

# AC-FIXED (mirrors ComposeStage's Q-50 posture) — not a TK-13 tunable.
_DEFAULT_TIMEOUT_SECONDS = 2.0

# A fixed, plain-spoken-English summarization instruction (DEC-55) — no prompt iteration. The
# guard suffix for Mouth.COMPOSE is appended verbatim at construction (below); no fifth 'speech'
# mouth is introduced (DEC-55e deferral).
_SPEECH_SHAPE_INSTRUCTION = (
    "You summarize one item for the user to be read aloud by text-to-speech. Rewrite it as plain "
    "spoken English, in one or two short, natural sentences. Never use markdown or any other "
    "formatting. Never read a URL or link aloud — describe it in words instead."
)

# DEC-55f: the hard brevity bound a validated speech text must fit within.
_MAX_SPEECH_CHARS = 400

# DEC-55f no-placebo validator: one compiled pattern per enumerated closed token class. ANY match
# rejects the whole text outright (see the module docstring) — never a partial strip/rewrite.
_FORBIDDEN_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\*\*.+?\*\*", re.DOTALL),  # bold (**text**)
    re.compile(r"(?<!\w)_[^_\n]+_(?!\w)"),  # italic (_text_)
    re.compile(r"(?<!\*)\*[^*\n]+\*(?!\*)"),  # italic (*text*)
    re.compile(r"^\s*#{1,6}\s", re.MULTILINE),  # heading (# text)
    re.compile(r"`"),  # backtick / code fence
    re.compile(r"\[[^\]]+\]\([^)]+\)"),  # markdown link ([text](url))
    re.compile(r"https?://\S+", re.IGNORECASE),  # raw URL
    re.compile(r"^\s*[-*+]\s", re.MULTILINE),  # bullet list marker
    re.compile(r"^\s*\d+\.\s", re.MULTILINE),  # numbered list marker
)


def _shape_speech_text(raw_text: str | None) -> str | None:
    """DEC-55f no-placebo validator: ``raw_text`` unchanged (trimmed) if it is free of every
    enumerated forbidden token class and within ``_MAX_SPEECH_CHARS``; otherwise ``None``
    (unsanitizable/overlong/blank -> no speech text, never a rewritten guess)."""
    if raw_text is None:
        return None
    stripped = raw_text.strip()
    if not stripped or len(stripped) > _MAX_SPEECH_CHARS:
        return None
    for pattern in _FORBIDDEN_PATTERNS:
        if pattern.search(stripped):
            return None
    return stripped


class SpeechShapeStage:
    """Produces the spoken-channel summary via a SECOND DeepSeek mouth call; degrades to no
    speech (never composed text) on any failure (TK-267, DEC-55)."""

    name: str = "speech_shape"
    transitions: tuple[str, ...] = ("speak",)

    def __init__(
        self,
        *,
        config: WombatConfig,
        voice_enabled: bool,
        adapter_present: bool,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        spend_ledger: DailySpendLedger | None = None,
        daily_token_ceiling: int | None = None,
    ) -> None:
        # Mirrors ComposeStage's AC3: fail at CONSTRUCTION when this mouth WILL be called (voice
        # on + adapter present) but the shared deepseek profile has no key to build against.
        deepseek_key = config.deepseek_api_key.get_secret_value().strip()
        if voice_enabled and adapter_present and not deepseek_key:
            msg = "SpeechShapeStage: DEEPSEEK_API_KEY is missing/blank; the mouth cannot be wired"
            raise ConfigurationError(msg)
        self._voice_enabled = voice_enabled
        self._adapter_present = adapter_present
        self._timeout_seconds = timeout_seconds
        self._spend_ledger = spend_ledger
        self._daily_token_ceiling = daily_token_ceiling
        # Built ONCE — the FIXED prompt (DEC-55) plus Mouth.COMPOSE's guard suffix, appended
        # verbatim via the read-only persona.expression seam (no fifth 'speech' mouth, DEC-55e).
        self._system_instruction = " ".join(
            [_SPEECH_SHAPE_INSTRUCTION, guard_suffix(Mouth.COMPOSE)]
        )

    async def run(self, ctx: StageContext) -> StageResult:
        art = await ctx.last_output("compose")
        if art is None:
            msg = "speech_shape: no compose output available yet"
            raise RuntimeError(msg)
        composed_text, item_id, item_kind, _degraded = composed_output_from_artifact_data(art.data)
        held_chat = composed_output_held_chat_from_artifact_data(art.data)

        if not self._voice_enabled or not self._adapter_present or held_chat:
            # ZERO model calls (AC4): a silent pass-through — the voice-off/no-adapter outcome at
            # speak stays byte-identical to today. DEC-57/TK-272: a held chat reply takes this
            # EXACT same voice-off shape (speech_text=None, degraded=False) — quiet-by-design,
            # never a model call, never routed through the degraded-warning branch downstream.
            return Transition(
                to="speak",
                output=Artifact(
                    kind=SPEECH_OUTPUT,
                    produced_by=self.name,
                    provenance=Provenance(source="system", confidence=1.0, recorded_at=ctx.clock()),
                    data=speech_output_to_artifact_data(item_id, item_kind, None, False),
                ),
            )

        messages = [
            ChatMessage(role="system", content=self._system_instruction),
            ChatMessage(role="user", content=composed_text),
        ]

        degraded = False
        speech_text: str | None = None

        # TK-9 layer 2 PRE-call gate (Q-68), mirroring ComposeStage: only armed if both are wired.
        if self._spend_ledger is not None and self._daily_token_ceiling is not None:
            try:
                spent_today = self._spend_ledger.tokens_spent_today()
            except Exception:
                logger.warning(
                    "speech_shape: daily spend ledger read failed; failing closed to no speech "
                    "without calling the model",
                    exc_info=True,
                )
                degraded = True
            else:
                if spent_today >= self._daily_token_ceiling:
                    logger.warning(
                        "speech_shape: daily token ceiling reached (%d spent >= %d ceiling); "
                        "degrading to no speech without calling the model",
                        spent_today,
                        self._daily_token_ceiling,
                    )
                    degraded = True

        response = None
        if not degraded:
            try:
                response = await asyncio.wait_for(
                    ctx.model.complete(messages=messages), timeout=self._timeout_seconds
                )
            except asyncio.CancelledError:
                # Never swallow cancellation — only the mouth's own failures degrade.
                raise
            except Exception:
                logger.warning(
                    "speech_shape: model call failed; degrading to no speech", exc_info=True
                )
                degraded = True

        if not degraded and response is not None:
            # TK-9 layer 2 POST-call accounting: a genuinely successful call records what it
            # spent, regardless of whether the returned text later fails validation below — the
            # call already spent the tokens either way. A ledger WRITE failure only logs loud.
            tokens_spent = response.usage.prompt_tokens + response.usage.completion_tokens
            if self._spend_ledger is not None:
                try:
                    self._spend_ledger.add_tokens(tokens_spent)
                except Exception:
                    logger.warning(
                        "speech_shape: daily spend ledger write failed; speech output stands",
                        exc_info=True,
                    )
            speech_text = _shape_speech_text(response.text)
            if speech_text is None:
                logger.warning(
                    "speech_shape: model response failed speech validation (blank, overlong, or "
                    "carrying a forbidden token class); degrading to no speech"
                )
                degraded = True

        if degraded:
            speech_text = None

        return Transition(
            to="speak",
            output=Artifact(
                kind=SPEECH_OUTPUT,
                produced_by=self.name,
                provenance=Provenance(source="system", confidence=1.0, recorded_at=ctx.clock()),
                data=speech_output_to_artifact_data(item_id, item_kind, speech_text, degraded),
            ),
        )


__all__ = ["SpeechShapeStage"]
