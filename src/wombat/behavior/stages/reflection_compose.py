"""ReflectionComposeStage — the mouth for ONE gate-surfaced reflection (TK-114, EP-22, Q-102b-f).

Closes FEAT-8's morning-render half: ``PatternDetectorStage`` (TK-113) enqueues at most one
``pattern_reflection`` item per night; once the standard gate surfaces it, ``ComposeDispatchRouter``
dispatches ``ItemKind.REFLECTION`` here (``composer_by_kind[ItemKind.REFLECTION] =
"reflection_compose"``, wired in ``bootstrap.py``). ``transitions = ()`` — TERMINAL by ruling
(Q-102c): unlike ``ComposeStage``'s TK-164 flip onward to ``speak``, routing a reflection there
would crash ``SpeakSink`` (its wire is ``last_output("compose")`` by STAGE NAME, not
``last_output("reflection_compose")``). Speaking the reflection is an explicit, flagged follow-up
— out of scope here.

**Input** (Q-102b): the SAME ``wombat.compose_request`` wire every composer reads
(``ctx.last_output("compose_dispatch")``), carrying TK-113's payload shape verbatim —
``{item_kind, event_class, kind, pattern_id, window_ref, date}`` — never scores/``GateAction``/
queue internals (CON-1/Q-50 payload boundary, inherited structurally from
``ComposeDispatchRouter``'s own payload-boundary construction).

**Assembly** (Q-102d/e): a LOCAL, per-turn ``cogworx.context.assembler.ContextAssembler`` —
never a shared/global instance — with EXACTLY three slots: ``instructions`` (head, required,
priority 0) carries a FIXED system prompt (module-local static contributor, never a prompt
iterated per pattern) forbidding clinical/diagnosis language, motive inference, and multi-sentence
analytics; ``reflection_hints`` (head, preferred, priority 1) is TK-118's
``PhrasingHintContributor`` bound to this turn's ``pattern_id`` — KB guidance text ONLY, never
recited verbatim as the model's output (NG-2/CON-6, proven by this stage's own AC4/AC2); ``task``
(tail, required, priority 0) is left UNWIRED so the assembler's built-in fallback synthesizes it
from ``ContextRequest.task`` — a single terse line built from payload fields ONLY (kind/date;
NEVER pattern_id/window_ref — those stay KB/queue-internal, never crossing into the model-visible
task line). ``ContextRequest.instructions`` is never populated (the verified assembler only ever
reads it as an unused overlay) — the fixed prompt rides the ``instructions`` SLOT, not that field.

**Degrade** (CON-3, mirrors ``ComposeStage``'s ``run()`` parity): a required-slot assembly failure
(``ContextAssemblyError``/``ContextBudgetError``), any model exception/timeout (ONE
``asyncio.wait_for(ctx.model.complete(...), timeout=self._timeout_seconds)`` call, never more),
or a blank response ALL degrade to ``_fallback(pattern_id)`` — a LOCAL, pure, deterministic terse
one-sentence observation derived from the humanized ``pattern_id`` string (NOT
``TemplateComposer.render``; its raw payload dump fails the NG-1/NG-2/NG-3 language bar).
``asyncio.CancelledError`` is re-raised first, ahead of any broad except — ``run()`` otherwise
never raises for a compose reason.

Scope (binding, out of this ticket): no transition to ``speak``, no spend-ledger layer (at most
one reflection enqueued per night — cog-worx layer-1 ``BudgetPolicy`` already bounds the call),
no ``ctx.journal`` touch, no ``StageToolPolicy`` (DEC-26), no KB/hint-content edits, no prompt
iteration beyond the one fixed instruction.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from typing import Any

from cogworx.claims.provenance import Artifact, Provenance
from cogworx.context.assembler import ContextAssembler
from cogworx.context.errors import ContextAssemblyError, ContextBudgetError
from cogworx.context.types import ContextRequest, SlotAllocation, SlotChunk, SlotContent, SlotSpec
from cogworx.loop.result import Done, StageResult
from cogworx.loop.stage import StageContext

from wombat.gate.models import ItemKind
from wombat.kb.contributors.phrasing_hint_contributor import PhrasingHintContributor
from wombat.kb.schema import KBEntry
from wombat.stages.artifacts import (
    COMPOSED_OUTPUT,
    compose_request_from_artifact_data,
    composed_output_to_artifact_data,
)

logger = logging.getLogger(__name__)

# A fixed, terse steward instruction (mirrors ComposeStage's own _SYSTEM_INSTRUCTION posture) —
# no prompt iteration beyond this one line (out of scope per the briefing). Explicitly forbids
# clinical/diagnosis framing, motive inference, and multi-sentence analytics (NG-1/NG-2/NG-3).
_SYSTEM_INSTRUCTION = (
    "You are a quiet steward reflecting one gentle behavioral observation back to the user. "
    "Phrase it in ONE terse, calm sentence. Never use clinical, diagnostic, or therapy language "
    "(never say 'diagnosis', 'disorder', or 'symptom'), never frame this as a diagnosis or as "
    "what a pattern 'indicates', never infer or state the user's motives or reasons (never say "
    "'you seem to', 'you tend to', 'because you', or 'due to your'), and never produce a "
    "multi-sentence analytics summary. No preamble."
)

# AC-FIXED — not a tunable; mirrors ComposeStage's own default timeout (Q-50 precedent).
_DEFAULT_TIMEOUT_SECONDS = 2.0

_SLOTS: tuple[SlotSpec, ...] = (
    SlotSpec(name="instructions", band="head", necessity="required", priority=0),
    SlotSpec(name="reflection_hints", band="head", necessity="preferred", priority=1),
    SlotSpec(name="task", band="tail", necessity="required", priority=0),
)


class _InstructionsContributor:
    """Tiny module-local static contributor for the ``instructions`` slot.

    Always returns the SAME one ``SlotChunk`` carrying ``_SYSTEM_INSTRUCTION`` — no per-turn
    variation, no I/O, never raises.
    """

    async def contribute(
        self,
        request: ContextRequest,
        allocation: SlotAllocation,
    ) -> SlotContent:
        return SlotContent(
            chunks=(
                SlotChunk(
                    text=_SYSTEM_INSTRUCTION, key="instructions:0", source_slot="instructions"
                ),
            ),
            status="ok",
        )


# ONE shared, stateless instance — safe to reuse across turns/instances (no per-turn state).
_INSTRUCTIONS_CONTRIBUTOR = _InstructionsContributor()


def _task_text(payload: dict[str, Any]) -> str:
    """One terse line built from payload fields ONLY — never scores/GateAction/queue internals
    (CON-1/Q-50), and deliberately narrower than ``format_payload_fields``: only ``kind``/
    ``date`` — never ``pattern_id``/``window_ref``, which stay KB/queue-internal."""
    kind = payload.get("kind", "reflection")
    date = payload.get("date", "")
    return f"kind: {kind}; date: {date}"


def _fallback(pattern_id: str | None) -> str:
    """A LOCAL, pure, deterministic terse one-sentence observation from a humanized
    ``pattern_id`` (CON-3 degrade path) — never ``TemplateComposer.render`` (its raw payload dump
    fails the NG-1/NG-2/NG-3 language bar). Always non-blank."""
    if not pattern_id:
        return "A quiet note on today."
    humanized = pattern_id.replace("_", " ").replace("-", " ").strip()
    if not humanized:
        return "A quiet note on today."
    return f"A quiet note on {humanized} today."


class ReflectionComposeStage:
    """Phrases ONE gate-surfaced reflection via the mouth; degrades to a terse local fallback
    (TK-114, EP-22). See module docstring for the full assemble/degrade contract."""

    name: str = "reflection_compose"
    # TERMINAL by ruling (Q-102c) — never "speak" (SpeakSink's wire is last_output("compose") by
    # stage name; routing here would crash it). Speaking the reflection is a flagged follow-up.
    transitions: tuple[str, ...] = ()

    def __init__(
        self,
        *,
        kb: Sequence[KBEntry],
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._kb = kb
        self._timeout_seconds = timeout_seconds

    async def run(self, ctx: StageContext) -> StageResult:
        art = await ctx.last_output("compose_dispatch")
        if art is None:
            msg = "reflection_compose: no compose_dispatch output available yet"
            raise RuntimeError(msg)
        item_id, _item_kind, payload = compose_request_from_artifact_data(art.data)
        raw_pattern_id = payload.get("pattern_id")
        pattern_id = str(raw_pattern_id) if raw_pattern_id else None

        # PER TURN, a LOCAL assembler — never shared/global (Q-102d): the reflection_hints
        # contributor is bound to THIS turn's pattern_id alone.
        assembler = ContextAssembler(slots=_SLOTS)
        assembler.register("instructions", _INSTRUCTIONS_CONTRIBUTOR)
        assembler.register(
            "reflection_hints", PhrasingHintContributor(pattern_id or "", self._kb)
        )

        degraded = False
        text: str | None = None

        try:
            assembled = await assembler.assemble(ContextRequest(task=_task_text(payload)))
        except (ContextAssemblyError, ContextBudgetError):
            logger.warning(
                "reflection_compose: context assembly failed; degrading to fallback",
                exc_info=True,
            )
            degraded = True
            assembled = None

        if not degraded:
            assert assembled is not None  # guaranteed by the try/except above
            try:
                response = await asyncio.wait_for(
                    ctx.model.complete(messages=list(assembled.messages)),
                    timeout=self._timeout_seconds,
                )
                text = response.text
            except asyncio.CancelledError:
                # Never swallow cancellation — only the mouth's own failures degrade.
                raise
            except Exception:
                logger.warning(
                    "reflection_compose: model call failed; degrading to fallback", exc_info=True
                )
                degraded = True

        if not degraded and (text is None or not text.strip()):
            degraded = True

        if degraded:
            text = _fallback(pattern_id)

        assert text is not None  # either the model's text or the fallback's render, always a str

        return Done(
            output=Artifact(
                kind=COMPOSED_OUTPUT,
                produced_by=self.name,
                provenance=Provenance(source="system", confidence=1.0, recorded_at=ctx.clock()),
                data=composed_output_to_artifact_data(
                    text, item_id, ItemKind.REFLECTION, degraded, tokens_spent=None
                ),
            )
        )


__all__ = ["ReflectionComposeStage"]
