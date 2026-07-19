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
from wombat.persona.builder import Mouth
from wombat.persona.live import LivePersona
from wombat.stages.artifacts import (
    COMPOSED_OUTPUT,
    compose_request_from_artifact_data,
    compose_request_held_chat_from_artifact_data,
    composed_output_to_artifact_data,
)

logger = logging.getLogger(__name__)


# A fixed, terse steward instruction (AC1) — no prompt iteration (mvp, TK-8 non_goal). TK-194
# (Q-105e) slots config.wombat_assistant_name into the name position ONLY; the remainder of the
# text is byte-identical to the pre-TK-194 fixed string. Display/persona only — never parsed,
# never in the gate, never an event field.
def _system_instruction(name: str = "Steward") -> str:
    return (
        f"You are {name}, a quiet steward. Phrase this one item for the user in one terse, "
        "calm line. No preamble."
    )


# AC-FIXED (Q-50) — not a TK-13 tunable.
_DEFAULT_TIMEOUT_SECONDS = 2.0


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

        # TK-209: render-time read when a LivePersona is wired — a matrix change applies on the
        # NEXT rendered turn, no restart. None -> the frozen-at-__init__ instruction (unchanged).
        system_instruction = (
            self._live_persona.instruction(Mouth.COMPOSE)
            if self._live_persona is not None
            else self._system_instruction
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
            text = self._template_composer.render(item_kind, payload)
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
                ),
            ),
        )


__all__ = ["ComposeStage"]
