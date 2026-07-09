"""wombat.stages.draft_dispatch — DraftDispatchStage: the terminal approval-consumption stage for
TK-78's Gmail draft pathway (EP-18, Q-92/Q-93, pass (b) of the Q-13 split).

Mirrors :class:`~wombat.stages.dispatch_approved.DispatchApprovedStage`'s decision-read/refusal
semantics WITHOUT any capability dispatch: TK-78's ``DraftComposer`` already created the Gmail
draft BEFORE parking (the taint-order proof, draft_composer.py's module docstring), so this stage
dispatching ``gmail.drafts.create`` again would double-create it. This stage dispatches **ZERO
capabilities on every path** — never-send is structural (CON-5/DEC-19/NG-5): approving a draft only
finalizes the action trail as approved-for-send (``TrailStatus.DISPATCHED`` via ``mark_dispatched``
— there is no approved-for-send trail member of its own, Q-92); the human still sends from Gmail.

Does NOT call ``bind_external_tier`` (unlike ``DispatchApprovedStage``) — admitting this stage to
the external capability tier would be unearned surface for a stage that dispatches nothing. The
resumed drive is tainted anyway (the engine rebuilds ``ctx`` with ``tainted=state.tainted`` at
resume, ``cogworx.runtime.engine.Engine.resume``), so an external dispatch attempted from here would
be refused regardless — this stage simply never tries.

The generic TK-149 ``DispatchApprovedStage`` cannot be reused directly here: its approve path
unconditionally dispatches its one configured capability. This stage reuses only its
``ApprovalTrailWriter`` seam and ``MissingApprovalAnswer`` refusal type (both imported, not
reimplemented) plus its ``ctx.read_human_input`` PULL-based decision-read pattern.

``action_id = f"{run_id}:{propose_stage_name}"`` (``propose_stage_name`` defaults to
``"draft_composer"``) — the SAME derivation TK-78's ``DraftComposer`` used for its own
``record_proposal`` call (``draft_composer.py:233``), so ``mark_dispatched``/``mark_cancelled``
land on the SAME trail row.

Locates the parked proposal step POSITION-INDEPENDENTLY (TK-179/Q-94, superseding the Q-92/Q-93
precomputed-position-index constructor-arg shape): the drain graph is ONE long-lived cog-worx run,
so every idle Sweeper poll commits a ``Wait`` step and ``draft_composer``'s ``AwaitHuman`` parks at
an ever-higher ``step_index`` — a fixed, precomputed static graph position (e.g. 4) goes stale the
moment the run idles even once before the draft item surfaces. Instead ``run()`` loads the run's
committed step history (``ctx.journal.load_run(ctx.run_id)``) and walks it in reverse for the LAST
step whose ``stage_name == propose_stage_name`` — that step's OWN ``step_index`` is exactly where
``Engine.provide_human_input`` recorded the answer (``engine.py`` — ``seq = last_step.step_index``,
and the awaiting step IS the last committed propose-stage step). This is the identical
journal-backed pull idiom ``StageContext.last_output`` already uses (``cogworx/runtime/context.py``
— ``last_output`` walks ``run.steps`` in reverse for a matching ``stage_name``). No matching step in
the run's history (a misconstruction) is treated exactly like a missing/malformed answer: a loud
refusal, never a silent no-op.
"""

from __future__ import annotations

from typing import Protocol

from cogworx.claims.provenance import Artifact, Provenance
from cogworx.loop.result import Done, StageResult
from cogworx.loop.stage import StageContext

from wombat.stages.dispatch_approved import ApprovalTrailWriter, MissingApprovalAnswer

# This stage's own terminal output kind — mirrors dispatch_approved.py's DISPATCH_RESULT shape
# without importing it (a distinct kind: draft-dispatch results are never externally dispatched).
DRAFT_DISPATCH_RESULT = "wombat.draft_dispatch_result"

_VALID_DECISIONS = ("approve", "reject")


class DraftApprovalTrailWriter(ApprovalTrailWriter, Protocol):
    """The exact ``ActionTrailWriter`` surface this stage needs — identical to
    ``ApprovalTrailWriter`` (mark_dispatched/mark_cancelled/record_refusal); named separately so a
    reader of this module doesn't have to cross-reference dispatch_approved.py to see the seam."""


class DraftDispatchStage:
    """The terminal stage TK-78's ``AwaitHuman(to="draft_dispatch")`` parks against (EP-18).

    Terminal (``transitions == ()``): every path out is a ``Done`` (approve or reject) or a loud
    raise (the missing-answer protocol violation, mirroring ``DispatchApprovedStage``). Dispatches
    NO capability on any path — approving a draft only finalizes the trail; the draft itself was
    already created by ``DraftComposer`` before the park.
    """

    transitions: tuple[str, ...] = ()

    def __init__(
        self,
        *,
        writer: DraftApprovalTrailWriter,
        propose_stage_name: str = "draft_composer",
    ) -> None:
        self.name = "draft_dispatch"
        self._writer = writer
        self._propose_stage_name = propose_stage_name

    async def run(self, ctx: StageContext) -> StageResult:
        action_id = f"{ctx.run_id}:{self._propose_stage_name}"
        now = ctx.clock()

        propose_step_index = await self._locate_propose_step_index(ctx)
        if propose_step_index is None:
            self._writer.record_refusal(
                action_id=action_id,
                human_summary=(
                    f"dispatch refused: no {self._propose_stage_name!r} step found in this "
                    "run's step history — cannot locate the parked approval answer"
                ),
                target=self._propose_stage_name,
                proposed_at=now,
            )
            raise MissingApprovalAnswer(
                f"{self.name}: no {self._propose_stage_name!r} step in run {ctx.run_id!r}'s "
                f"step history for action_id={action_id!r} — cannot locate the parked approval "
                "answer"
            )

        answer = await ctx.read_human_input(propose_step_index)
        decision = answer.data.get("decision") if answer is not None else None

        if decision not in _VALID_DECISIONS:
            self._writer.record_refusal(
                action_id=action_id,
                human_summary=(
                    f"dispatch refused: no valid approval answer at step "
                    f"{propose_step_index} (decision={decision!r})"
                ),
                target=self._propose_stage_name,
                proposed_at=now,
            )
            raise MissingApprovalAnswer(
                f"{self.name}: no valid 'decision' in human input at step "
                f"{propose_step_index} for action_id={action_id!r} (got {decision!r})"
            )

        if decision == "reject":
            self._writer.mark_cancelled(action_id, now)
            return Done(
                output=Artifact(
                    kind=DRAFT_DISPATCH_RESULT,
                    produced_by=self.name,
                    provenance=Provenance(source="system", confidence=1.0, recorded_at=now),
                    data={"action_id": action_id, "status": "cancelled"},
                )
            )

        # approve — finalize the trail as approved-for-send (TrailStatus.DISPATCHED via
        # mark_dispatched, Q-92). NO capability dispatch: the draft already exists in Gmail
        # Drafts (DraftComposer created it pre-park); the human sends from Gmail, never wombat.
        self._writer.mark_dispatched(action_id, now)
        return Done(
            output=Artifact(
                kind=DRAFT_DISPATCH_RESULT,
                produced_by=self.name,
                provenance=Provenance(source="system", confidence=1.0, recorded_at=now),
                data={"action_id": action_id, "status": "dispatched"},
            )
        )

    async def _locate_propose_step_index(self, ctx: StageContext) -> int | None:
        """Walk this run's committed step history for the LAST step whose ``stage_name`` matches
        ``propose_stage_name`` and return its own ``step_index`` — the position-independent
        replacement for a precomputed constructor-supplied step index (TK-179/Q-94). Mirrors
        ``StageContext.last_output`` exactly (``cogworx/runtime/context.py``): journal-backed,
        reverse-walked, crash-correct on cold resume. ``None`` when no such step exists (a
        misconstruction) — the caller treats this as a loud refusal, never a silent no-op.
        """
        run = await ctx.journal.load_run(ctx.run_id)
        if run is None:
            return None
        for step in reversed(run.steps):
            if step.stage_name == self._propose_stage_name:
                return step.step_index
        return None


__all__ = [
    "DRAFT_DISPATCH_RESULT",
    "DraftApprovalTrailWriter",
    "DraftDispatchStage",
]
