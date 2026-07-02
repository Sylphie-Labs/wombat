"""ComposeStage — the DeepSeek mouth via S4, terse-template degrade (TK-8, EP-5, Q-50).

Phrases ONE pre-decided surfaced item per invocation (the router, TK-10, dispatches per item)
by calling ``ctx.model.complete`` — the ENGINE assembles the per-drive DeepSeek model from TK-1's
registered ``model_profile="deepseek"`` spec; ``ComposeStage`` never calls ``build_model`` and
never constructs a client itself (S4). The mouth must never break the drain loop: a timeout, any
provider/connection/HTTP-5xx/generic model error, a ``BudgetExceededError`` (budget POLICY is
TK-9's — this stage only degrades, never enforces), or an empty/blank response ALL degrade to
``TemplateComposer``'s deterministic terse line with ``degraded=True``; ``run()`` never raises
(``asyncio.CancelledError`` is re-raised explicitly, ahead of the broad except).

``ctx`` surface is exactly ``ctx.model`` + ``ctx.last_output("compose_dispatch")`` +
``ctx.clock`` (provenance only, mirroring ``GateStage``'s Q-48 pattern). The input wire
(``wombat.compose_request``) is defined now so TK-10 can produce it later with zero rework;
NO scores/``GateAction``/queue internals may cross it (Q-50) — the prompt the model sees is built
ONLY from the wire's ``payload`` + ``item_kind``, never from anything else.

``ComposeStage`` is the terminal node of the mvp spine (``transitions = ()``, returns ``Done``);
a future deliver/voice stage (EP-30) flips ``Done`` -> ``Transition(to="deliver")`` as a one-line
change.
"""

from __future__ import annotations

import asyncio
import logging

from cogworx.claims.provenance import Artifact, Provenance
from cogworx.loop.result import Done, StageResult
from cogworx.loop.stage import StageContext
from cogworx.model.base import ChatMessage

from wombat.compose.templates import TemplateComposer, format_payload_fields
from wombat.config import ConfigurationError, WombatConfig
from wombat.stages.artifacts import (
    COMPOSED_OUTPUT,
    compose_request_from_artifact_data,
    composed_output_to_artifact_data,
)

logger = logging.getLogger(__name__)

# A fixed, terse steward instruction (AC1) — no prompt iteration (mvp, TK-8 non_goal).
_SYSTEM_INSTRUCTION = (
    "You are a quiet steward. Phrase this one item for the user in one terse, calm line. "
    "No preamble."
)

# AC-FIXED (Q-50) — not a TK-13 tunable.
_DEFAULT_TIMEOUT_SECONDS = 2.0


class ComposeStage:
    """Phrases ONE surfaced item via the DeepSeek mouth; degrades to a terse template (TK-8)."""

    name: str = "compose"
    transitions: tuple[str, ...] = ()

    def __init__(
        self,
        *,
        config: WombatConfig,
        template_composer: TemplateComposer,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        # AC3: fail at CONSTRUCTION, not first call. load_config() already fails loud on an
        # ABSENT env var; this check also catches a blank-string value pydantic-settings would
        # otherwise accept, making the mouth's dependency explicit at wiring time.
        if not config.deepseek_api_key.get_secret_value():
            msg = "ComposeStage: DEEPSEEK_API_KEY is missing/blank; the mouth cannot be wired"
            raise ConfigurationError(msg)
        self._template_composer = template_composer
        self._timeout_seconds = timeout_seconds

    async def run(self, ctx: StageContext) -> StageResult:
        art = await ctx.last_output("compose_dispatch")
        if art is None:
            msg = "compose: no compose_dispatch output available yet"
            raise RuntimeError(msg)
        item_id, item_kind, payload = compose_request_from_artifact_data(art.data)

        messages = [
            ChatMessage(role="system", content=_SYSTEM_INSTRUCTION),
            ChatMessage(
                role="user",
                content=f"item_kind: {item_kind.value}\n{format_payload_fields(payload)}",
            ),
        ]

        degraded = False
        text: str | None = None
        try:
            response = await asyncio.wait_for(
                ctx.model.complete(messages=messages), timeout=self._timeout_seconds
            )
            text = response.text
        except asyncio.CancelledError:
            # Never swallow cancellation — only the mouth's own failures degrade (AC2).
            raise
        except Exception:
            # Timeout, provider/connection/HTTP-5xx errors, and BudgetExceededError (budget
            # POLICY is TK-9's — this stage only degrades, never enforces) all land here: the
            # mouth must never break the drain loop.
            logger.warning("compose: model call failed; degrading to template", exc_info=True)
            degraded = True

        if not degraded and (text is None or not text.strip()):
            degraded = True

        if degraded:
            text = self._template_composer.render(item_kind, payload)

        assert text is not None  # either the model's text or the template's render, always a str

        return Done(
            output=Artifact(
                kind=COMPOSED_OUTPUT,
                produced_by=self.name,
                provenance=Provenance(source="system", confidence=1.0, recorded_at=ctx.clock()),
                data=composed_output_to_artifact_data(text, item_id, item_kind, degraded),
            )
        )


__all__ = ["ComposeStage"]
