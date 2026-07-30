"""ComposeStage — the DeepSeek mouth via S4, terse-template degrade (TK-8, EP-5, Q-50).

Phrases ONE pre-decided surfaced item per invocation (the router, TK-10, dispatches per item)
by calling ``ctx.model.complete`` — the ENGINE assembles the per-drive DeepSeek model from TK-1's
registered ``model_profile="deepseek"`` spec; ``ComposeStage`` never calls ``build_model`` and
never constructs a client itself (S4). The mouth must never break the drain loop: a timeout, any
provider/connection/HTTP-5xx/generic model error, a ``BudgetExceededError`` (layer-1 budget
POLICY is cog-worx's per-drive ``BudgetPolicy``, wired real by TK-9's bootstrap config — this
stage only degrades, never enforces it), or an empty/blank response ALL degrade to
``TemplateComposer``'s deterministic terse line with ``degraded=True``; ``run()`` never raises
(``asyncio.CancelledError`` is re-raised explicitly, ahead of the broad except).

TK-9 (Q-68) layers a wombat-owned daily TOKEN-spend ceiling on top (layer 2, durable, outer cap):
an injected ``spend_ledger``/``daily_token_ceiling`` add a PRE-call gate — at/over the ceiling,
degrade to the template WITHOUT calling the model — and POST-call accounting — a successful,
non-degraded call's ``usage.prompt_tokens + completion_tokens`` is added to the ledger and rides
the output artifact as ``tokens_spent``. Both are optional (default ``None``): omitting them
disables layer 2 entirely and preserves TK-8's exact behavior (existing callers unaffected). A
ledger READ failure fails CLOSED to the template (no model call while accounting is blind); a
ledger WRITE failure only logs loud — the already-composed output stands (the call already spent).

``ctx`` surface is exactly ``ctx.model`` + ``ctx.last_output("compose_dispatch")`` +
``ctx.clock`` (provenance only, mirroring ``GateStage``'s Q-48 pattern). The input wire
(``wombat.compose_request``) is defined now so TK-10 can produce it later with zero rework;
NO scores/``GateAction``/queue internals may cross it (Q-50) — the prompt the model sees is built
ONLY from the wire's ``payload`` + ``item_kind``, never from anything else.

TK-164 (Q-96) lands the EP-30-reserved flip: ``ComposeStage`` is no longer the drain spine's
terminal node — it transitions onward carrying the SAME ``wombat.composed_output`` artifact,
byte-identical, instead of returning ``Done``.

TK-222 (EP-32, Q-110(d)) inserts the chat-reply hop between this stage and the voice sink:
``transitions`` is now ``("chat_reply",)`` and ``run()`` returns ``Transition(to="chat_reply",
output=...)``. ``SpeakSink`` (``sinks/speak.py``) is UNAFFECTED — it reads this exact artifact via
``ctx.last_output("compose")`` BY STAGE NAME, not via whatever stage ran immediately before it,
so inserting ``chat_reply`` (``stages/chat_reply.py``) as a pass-through hop between ``compose``
and ``speak`` leaves ``SpeakSink`` byte-identical.

TK-267 (DEC-55) inserts a NEW ``speech_shape`` hop further downstream, between ``chat_reply`` and
``speak`` — this stage's own transition (``chat_reply``) and every artifact it produces are
UNCHANGED; the module docstring above stays accurate as written.

TK-209 (EP-33): an OPTIONAL ``live_persona`` (``wombat.persona.live.LivePersona``) — ``None``
(the default) keeps the frozen-at-``__init__`` instruction above, byte-identical to every existing
caller/test; when wired, ``run()`` reads ``live_persona.instruction(Mouth.COMPOSE)`` fresh EVERY
turn instead, so a hot-applied persona matrix change lands on the NEXT rendered turn, no restart.

TK-279 (DEC-60b): ``voice_turn`` threads through identically to ``held_chat`` — read off the
compose-request wire and re-stamped onto the composed-output wire, unchanged otherwise.

TK-293 (DEC-65b): a chat turn (``item_kind is ItemKind.CHAT`` — typed AND voice both carry this
kind, DEC-57/DEC-60) composes under ``Mouth.CHAT`` instead of ``Mouth.COMPOSE``, selected at the
SAME render-time branch point ``run()`` already used for the live/frozen split (TK-209): with a
``live_persona`` wired, ``live_persona.instruction(Mouth.CHAT)`` is read fresh every turn (the
live persona already threads ``user_name`` through unconditionally — TK-292); with none wired, a
NEW frozen ``self._chat_system_instruction`` (built once in ``__init__`` from
``config.wombat_assistant_name``/``config.wombat_user_name``, pinned byte-equivalent to
``instruction_for(Mouth.CHAT, DEFAULT_MATRIX, name, user_name=...)``) stands in for the frozen
``self._system_instruction`` above. Every other ``item_kind`` is completely unaffected — same
selection, same instruction, byte-identical. The degrade path (``TemplateComposer``) is untouched
for chat too (DEC-37e's honest asymmetry — personality is model-path-only).

REPAIR (batch review, TK-293 x TK-296 cross-ticket): TK-289/TK-290/TK-296's ``context_hook``
(``bootstrap.py``'s ``asr_context_hook``) stamps grounding-only fields — ``replying_to``,
``known_user_context``, ``context_calendar_today``, ``context_recent_email`` — onto a chat item's
payload for the MODEL prompt's benefit only; they were never part of the item's own user-facing
content. A degrade must never dump them verbatim: voice is shielded downstream by
``SpeechShapeStage``'s DEC-55c never-verbatim bar, but ``ChatReplyStage`` resolves this stage's
degrade text straight to the typed chat pane unshaped. ``run()`` therefore strips
``_GROUNDING_ONLY_KEYS`` from the payload handed to ``TemplateComposer.render`` ONLY on the
degrade branch below — the model-facing prompt above still sees every grounding field
unfiltered, and non-chat item kinds are unaffected in practice (these keys are never stamped on
them).
"""

from __future__ import annotations

import asyncio
import logging

from cogworx.claims.provenance import Artifact, Provenance
from cogworx.loop.result import StageResult, Transition
from cogworx.loop.stage import StageContext
from cogworx.model.base import ChatMessage

from wombat.compose.templates import TemplateComposer, format_payload_fields
from wombat.config import ConfigurationError, WombatConfig
from wombat.cost.daily_spend_ledger import DailySpendLedger
from wombat.gate.models import ItemKind
from wombat.persona.builder import Mouth
from wombat.persona.capabilities import CAPABILITY_CHARTER
from wombat.persona.live import LivePersona
from wombat.stages.artifacts import (
    COMPOSED_OUTPUT,
    compose_request_from_artifact_data,
    compose_request_held_chat_from_artifact_data,
    compose_request_voice_turn_from_artifact_data,
    composed_output_to_artifact_data,
)

logger = logging.getLogger(__name__)


# A fixed, terse steward instruction (AC1) — no prompt iteration (mvp, TK-8 non_goal). TK-194
# (Q-105e) slots config.wombat_assistant_name into the name position ONLY; the remainder of the
# text is byte-identical to the pre-TK-194 fixed string. Display/persona only — never parsed,
# never in the gate, never an event field.
def _system_instruction(name: str = "Steward") -> str:
    # TK-284, DEC-62(a): appends the same imported CAPABILITY_CHARTER as the render_expression
    # seam (persona/expression.py) so this frozen fallback stays byte-equivalent to
    # instruction_for(Mouth.COMPOSE, DEFAULT_MATRIX, name).
    return (
        f"You are {name}, a quiet steward. Phrase this one item for the user in one terse, "
        "calm line. No preamble. " + CAPABILITY_CHARTER
    )


# TK-293, DEC-65b: the frozen Mouth.CHAT fallback (live_persona is None) — mirrors
# _system_instruction's role above but for chat turns. Pinned byte-equivalent to
# instruction_for(Mouth.CHAT, DEFAULT_MATRIX, assistant_name, user_name=user_name): at
# DEFAULT_MATRIX every persona_policy.yaml clause for chat renders the empty string (same
# invariant _system_instruction relies on for compose), so the base role plus the chat guard
# suffix (the same COMPOSE capability charter, DEC-65a/c) is the whole string. user_display
# falls back to "the user" exactly like ClauseAlgebraStrategy.render's special case for CHAT.
def _chat_system_instruction(assistant_name: str, user_name: str) -> str:
    user_display = user_name if user_name else "the user"
    return (
        f"You are {assistant_name}, {user_display}'s personal assistant and companion, chatting "
        f"with {user_display}. Reply naturally and conversationally in a warm, familiar voice - "
        "match the user's tone, and roll with jokes, banter, and playfulness when the user "
        "brings them. Casual conversation is welcome for its own sake; do not steer the chat "
        "back to schedules, email, or duties unless asked. Ground anything factual in what you "
        "are given, and keep replies short and human - a sentence or two unless more is clearly "
        "wanted. No preamble. " + CAPABILITY_CHARTER
    )


# AC-FIXED (Q-50) — not a TK-13 tunable.
_DEFAULT_TIMEOUT_SECONDS = 2.0

# REPAIR (batch review, TK-293 x TK-296): the exact key names ``context_hook`` may stamp onto a
# chat payload — ``replying_to`` (bootstrap.py's asr_context_hook, TK-289), and
# ``known_user_context``/``context_calendar_today``/``context_recent_email``
# (voice/context_prefetch.py, TK-290/TK-296). Prompt-only grounding, never echoed verbatim by the
# degrade template — see the module docstring.
_GROUNDING_ONLY_KEYS = frozenset(
    {
        "replying_to",
        "known_user_context",
        "context_calendar_today",
        "context_recent_email",
    }
)


class ComposeStage:
    """Phrases ONE surfaced item via the DeepSeek mouth; degrades to a terse template (TK-8)."""

    name: str = "compose"
    # TK-164, Q-96: the EP-30-reserved flip — the mouth transitions onward instead of ending the
    # drain spine itself. TK-222, Q-110(d): the onward edge is now "chat_reply" (a pass-through
    # hop to "speak") rather than "speak" directly — see the module docstring.
    transitions: tuple[str, ...] = ("chat_reply",)

    def __init__(
        self,
        *,
        config: WombatConfig,
        template_composer: TemplateComposer,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        spend_ledger: DailySpendLedger | None = None,
        daily_token_ceiling: int | None = None,
        live_persona: LivePersona | None = None,
    ) -> None:
        # AC3: fail at CONSTRUCTION, not first call. load_config() already fails loud on an
        # ABSENT env var; this check also catches a blank-string value pydantic-settings would
        # otherwise accept, making the mouth's dependency explicit at wiring time.
        if not config.deepseek_api_key.get_secret_value().strip():
            msg = "ComposeStage: DEEPSEEK_API_KEY is missing/blank; the mouth cannot be wired"
            raise ConfigurationError(msg)
        self._template_composer = template_composer
        self._timeout_seconds = timeout_seconds
        # TK-9 layer 2 (Q-68): both default to None, disabling the daily ceiling gate entirely
        # and preserving TK-8's exact behavior for any caller that doesn't wire them.
        self._spend_ledger = spend_ledger
        self._daily_token_ceiling = daily_token_ceiling
        # TK-194: built ONCE from config.wombat_assistant_name — display/persona only. Stands as
        # the frozen fallback when live_persona is None (TK-209).
        self._system_instruction = _system_instruction(config.wombat_assistant_name)
        # TK-293 (DEC-65b): the chat-mouth counterpart, built ONCE from the same config object —
        # no new ctor params. Stands as the frozen fallback for chat turns when live_persona is
        # None, mirroring self._system_instruction above.
        self._chat_system_instruction = _chat_system_instruction(
            config.wombat_assistant_name, config.wombat_user_name
        )
        # TK-209 (EP-33): OPTIONAL — None preserves the frozen-at-__init__ instruction above
        # (every existing caller/test stands unchanged); when provided, run() reads
        # live_persona.instruction(Mouth.COMPOSE) fresh EVERY turn instead (hot-apply, no
        # restart).
        self._live_persona = live_persona

    async def run(self, ctx: StageContext) -> StageResult:
        art = await ctx.last_output("compose_dispatch")
        if art is None:
            msg = "compose: no compose_dispatch output available yet"
            raise RuntimeError(msg)
        item_id, item_kind, payload = compose_request_from_artifact_data(art.data)
        held_chat = compose_request_held_chat_from_artifact_data(art.data)
        voice_turn = compose_request_voice_turn_from_artifact_data(art.data)

        # TK-209: render-time read when a LivePersona is wired — a matrix change applies on the
        # NEXT rendered turn, no restart. None -> the frozen-at-__init__ instruction (unchanged).
        # TK-293 (DEC-65b): a chat turn selects Mouth.CHAT / self._chat_system_instruction at this
        # SAME branch point instead — every other item_kind is byte-identical to before.
        is_chat = item_kind is ItemKind.CHAT
        mouth = Mouth.CHAT if is_chat else Mouth.COMPOSE
        frozen_fallback = self._chat_system_instruction if is_chat else self._system_instruction
        system_instruction = (
            self._live_persona.instruction(mouth)
            if self._live_persona is not None
            else frozen_fallback
        )

        messages = [
            ChatMessage(role="system", content=system_instruction),
            ChatMessage(
                role="user",
                content=f"item_kind: {item_kind.value}\n{format_payload_fields(payload)}",
            ),
        ]

        degraded = False
        text: str | None = None
        tokens_spent: int | None = None

        # TK-9 layer 2 PRE-call gate (Q-68): only armed if both are wired (layer 2 is optional,
        # TK-8 callers that wire neither skip straight to the model call below, unaffected).
        if self._spend_ledger is not None and self._daily_token_ceiling is not None:
            try:
                spent_today = self._spend_ledger.tokens_spent_today()
            except Exception:
                # Ledger READ failure: fail CLOSED to the template — no model call while spend
                # accounting is blind (Q-68's conservative, quiet-thesis direction).
                logger.warning(
                    "compose: daily spend ledger read failed; failing closed to template "
                    "without calling the model",
                    exc_info=True,
                )
                degraded = True
            else:
                if spent_today >= self._daily_token_ceiling:
                    logger.warning(
                        "compose: daily token ceiling reached (%d spent >= %d ceiling); "
                        "degrading to template without calling the model",
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
                text = response.text
            except asyncio.CancelledError:
                # Never swallow cancellation — only the mouth's own failures degrade (AC2).
                raise
            except Exception:
                # Timeout, provider/connection/HTTP-5xx errors, and BudgetExceededError (layer-1
                # per-drive policy is cog-worx's — this stage only degrades, never enforces it)
                # all land here: the mouth must never break the drain loop.
                logger.warning("compose: model call failed; degrading to template", exc_info=True)
                degraded = True

        if not degraded and (text is None or not text.strip()):
            degraded = True

        if not degraded and response is not None:
            # TK-9 layer 2 POST-call accounting (Q-68): a genuinely successful, non-degraded call
            # records what it spent. A ledger WRITE failure only logs loud — the already-composed
            # output stands (the call already spent).
            tokens_spent = response.usage.prompt_tokens + response.usage.completion_tokens
            if self._spend_ledger is not None:
                try:
                    self._spend_ledger.add_tokens(tokens_spent)
                except Exception:
                    logger.warning(
                        "compose: daily spend ledger write failed; composed output stands",
                        exc_info=True,
                    )

        if degraded:
            # REPAIR (batch review, TK-293 x TK-296): strip grounding-only keys before the
            # template renders — those fields grounded the model prompt above, never the user's
            # own item content, so a degrade must not dump them verbatim (see module docstring).
            degrade_payload = {
                key: value for key, value in payload.items() if key not in _GROUNDING_ONLY_KEYS
            }
            text = self._template_composer.render(item_kind, degrade_payload)
            tokens_spent = None

        assert text is not None  # either the model's text or the template's render, always a str

        # TK-164/TK-222: transitions onward to "chat_reply" carrying the SAME artifact,
        # byte-identical (SpeakSink still reads it back via ctx.last_output("compose") +
        # composed_output_from_artifact_data — this is the one and only wire it consumes,
        # unaffected by the chat_reply hop in between).
        return Transition(
            to="chat_reply",
            output=Artifact(
                kind=COMPOSED_OUTPUT,
                produced_by=self.name,
                provenance=Provenance(source="system", confidence=1.0, recorded_at=ctx.clock()),
                data=composed_output_to_artifact_data(
                    text,
                    item_id,
                    item_kind,
                    degraded,
                    tokens_spent=tokens_spent,
                    held_chat=held_chat,
                    voice_turn=voice_turn,
                ),
            ),
        )


__all__ = ["ComposeStage"]
