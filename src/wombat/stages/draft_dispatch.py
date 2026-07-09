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

``ask_step_index`` is a REQUIRED, no-default constructor argument (Q-92/Q-93): the engine records
the human answer at the parked step's positional index (``engine.py`` — ``seq =
last_step.step_index``), which is graph-position-sensitive. There is no safe default; a caller
must pass the value for its own graph (1 in this ticket's 3-stage test graph; TK-177 supplies the
live drain graph's index at boot-wiring time).
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
        ask_step_index: int,
        propose_stage_name: str = "draft_composer",
    ) -> None:
        self.name = "draft_dispatch"
        self._writer = writer
        self._ask_step_index = ask_step_index
        self._propose_stage_name = propose_stage_name

    async def run(self, ctx: StageContext) -> StageResult:
        action_id = f"{ctx.run_id}:{self._propose_stage_name}"
        now = ctx.clock()

        answer = await ctx.read_human_input(self._ask_step_index)
        decision = answer.data.get("decision") if answer is not None else None

        if decision not in _VALID_DECISIONS:
            self._writer.record_refusal(
                action_id=action_id,
                human_summary=(
                    f"dispatch refused: no valid approval answer at step "
                    f"{self._ask_step_index} (decision={decision!r})"
                ),
                target=self._propose_stage_name,
                proposed_at=now,
            )
            raise MissingApprovalAnswer(
                f"{self.name}: no valid 'decision' in human input at step "
                f"{self._ask_step_index} for action_id={action_id!r} (got {decision!r})"
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


__all__ = [
    "DRAFT_DISPATCH_RESULT",
    "DraftApprovalTrailWriter",
    "DraftDispatchStage",
]
