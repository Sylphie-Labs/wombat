"""BriefComposeStage — mouth renders sealed brief contents into terse natural language
(TK-100, Q-77).

Third stage of the morning-brief cluster: reads the sealed ``BriefDecisionArtifact`` (TK-99,
``ctx.last_output("brief_force_flush")``), builds the ONE terse prompt body via
``wombat.compose.brief_template.render_brief_lines`` (Q-50 boundary — the model sees ONLY the
already-decided sealed contents, never raw source data or selection logic), and calls the mouth
``ctx.model.complete`` EXACTLY ONCE (the S4 seam — this stage never builds a client itself).

SIBLING of ``ComposeStage`` (``wombat.stages.compose``), not a modification of it. It MIRRORS
that stage's catch-set exactly (parity): ``asyncio.CancelledError`` is re-raised ahead of the
broad except; a timeout, any provider/connection/HTTP-5xx/generic model error, a
``BudgetExceededError`` (layer-1 per-drive policy is cog-worx's, this stage only degrades), and
a blank/empty response ALL degrade to the SAME ``render_brief_lines`` string (single source of
truth shared with the model's own prompt) with ``degraded=True``; ``run()`` never raises.

TK-9-style layer 2 (an injected ``spend_ledger``/``daily_token_ceiling``, both optional,
default ``None``) adds the same PRE-call ceiling gate (a ceiling/ledger-READ failure fails
CLOSED to the template WITHOUT calling the model) and POST-call accounting (a successful,
non-degraded call's token spend is added to the ledger; a ledger WRITE failure only logs loud —
the already-composed output stands) as ``ComposeStage``. The factory
(``bootstrap.build_brief_compose_stage``) wires this onto the SAME ``"spend:tokens"`` ledger row
as ``build_compose_stage``, so drain and brief share ONE daily token cap.

``ctx`` surface is exactly ``ctx.model`` + ``ctx.last_output("brief_force_flush")`` + ``ctx.clock``
(provenance only) — this stage NEVER touches ``ctx.journal``. Terminal wire is
``wombat.brief_text`` (``BRIEF_TEXT``); the stage always transitions to ``brief_deliver``
(TK-101's declared-ahead name), even on a fully degraded run.
"""

from __future__ import annotations

import asyncio
import logging
from zoneinfo import ZoneInfo

from cogworx.claims.provenance import Artifact, Provenance
from cogworx.loop.result import StageResult, Transition
from cogworx.loop.stage import StageContext
from cogworx.model.base import ChatMessage

from wombat.compose.brief_template import brief_system_instruction, render_brief_lines
from wombat.config import ConfigurationError, WombatConfig
from wombat.cost.daily_spend_ledger import DailySpendLedger
from wombat.domain.brief_decision_artifact import BriefDecisionArtifact
from wombat.stages.artifacts import BRIEF_TEXT, brief_text_to_artifact_data

logger = logging.getLogger(__name__)

# A generous fixed timeout (Q-77) — the brief is a once-a-day, non-latency-critical call, unlike
# the per-item ComposeStage's tighter 2s default.
_DEFAULT_TIMEOUT_SECONDS = 10.0


class BriefComposeStage:
    """Phrases the sealed morning brief via the mouth; degrades to the shared template (TK-100)."""

    name: str = "brief_compose"
    transitions: tuple[str, ...] = ("brief_deliver",)

    def __init__(
        self,
        *,
        config: WombatConfig,
        tz: ZoneInfo,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        spend_ledger: DailySpendLedger | None = None,
        daily_token_ceiling: int | None = None,
    ) -> None:
        # Fail at CONSTRUCTION, not first call (mirrors ComposeStage's AC3 posture, TK-8).
        if not config.deepseek_api_key.get_secret_value().strip():
            msg = (
                "BriefComposeStage: DEEPSEEK_API_KEY is missing/blank; the mouth cannot be wired"
            )
            raise ConfigurationError(msg)
        self._tz = tz
        self._timeout_seconds = timeout_seconds
        # Layer 2 (Q-68 precedent): both default to None, disabling the daily ceiling gate.
        self._spend_ledger = spend_ledger
        self._daily_token_ceiling = daily_token_ceiling
        # TK-194: built ONCE from config.wombat_assistant_name — display/persona only.
        self._system_instruction = brief_system_instruction(config.wombat_assistant_name)

    async def run(self, ctx: StageContext) -> StageResult:
        art = await ctx.last_output("brief_force_flush")
        if art is None:
            msg = "brief_compose: no brief_force_flush output available yet"
            raise RuntimeError(msg)
        artifact = BriefDecisionArtifact.from_payload(art.data)

        # The SINGLE source of truth (Q-77): this exact string is both the model's user message
        # and the S8 fallback body, so the two paths can never drift apart.
        body = render_brief_lines(artifact, tz=self._tz)

        messages = [
            ChatMessage(role="system", content=self._system_instruction),
            ChatMessage(role="user", content=body),
        ]

        degraded = False
        text: str | None = None
        tokens_spent = 0

        # Layer 2 PRE-call gate: only armed if both are wired.
        if self._spend_ledger is not None and self._daily_token_ceiling is not None:
            try:
                spent_today = self._spend_ledger.tokens_spent_today()
            except Exception:
                # Ledger READ failure: fail CLOSED to the template — no model call while spend
                # accounting is blind.
                logger.warning(
                    "brief_compose: daily spend ledger read failed; failing closed to template "
                    "without calling the model",
                    exc_info=True,
                )
                degraded = True
            else:
                if spent_today >= self._daily_token_ceiling:
                    logger.warning(
                        "brief_compose: daily token ceiling reached (%d spent >= %d ceiling); "
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
                # Never swallow cancellation — only the mouth's own failures degrade.
                raise
            except Exception:
                # Timeout, provider/connection/HTTP-5xx errors, and BudgetExceededError all land
                # here: the mouth must never break the pathway.
                logger.warning(
                    "brief_compose: model call failed; degrading to template", exc_info=True
                )
                degraded = True

        if not degraded and (text is None or not text.strip()):
            degraded = True

        if not degraded and response is not None:
            # POST-call accounting: a genuinely successful, non-degraded call records its spend.
            # A ledger WRITE failure only logs loud — the already-composed output stands.
            tokens_spent = response.usage.prompt_tokens + response.usage.completion_tokens
            if self._spend_ledger is not None:
                try:
                    self._spend_ledger.add_tokens(tokens_spent)
                except Exception:
                    logger.warning(
                        "brief_compose: daily spend ledger write failed; composed output stands",
                        exc_info=True,
                    )

        if degraded:
            text = body
            tokens_spent = 0

        assert text is not None  # either the model's text or the template body, always a str

        return Transition(
            to="brief_deliver",
            output=Artifact(
                kind=BRIEF_TEXT,
                produced_by=self.name,
                provenance=Provenance(source="system", confidence=1.0, recorded_at=ctx.clock()),
                data=brief_text_to_artifact_data(text, degraded, tokens_spent),
            ),
        )


__all__ = ["BriefComposeStage"]
