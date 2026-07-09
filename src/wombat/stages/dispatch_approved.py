"""wombat.stages.dispatch_approved — DispatchApprovedStage, the shared dispatch-side half of the
TK-149 two-step outbound base (EP-28, Q-91).

Paired with a :class:`~wombat.stages.dispatch_base.ProposeDispatchStage` subclass: the propose
stage parks the run AWAITING_HUMAN via ``AwaitHuman(to=<this stage's name>)``; once
``Engine.provide_human_input`` records the answer and re-drives, the engine advances here. This
stage reads the journaled decision (PULL, via ``ctx.read_human_input`` — never a live in-process
push) and either dispatches EXACTLY ONCE (approved) or cancels with ZERO dispatch calls (rejected)
— there is no path from a parked proposal to a real external side effect that skips this stage.

Generic + reusable over ONE capability: a caller parameterizes ``capability`` (the name to
dispatch), ``args_from_artifact`` (reads the propose stage's own committed proposal Artifact —
pulled via ``ctx.last_output(propose_stage_name)``, journal-backed and cold-resume safe — into the
capability's args mapping), the shared trail ``writer``, and ``ask_step_index`` (the propose
stage's position in the two-stage graph; always the default ``0`` — it is the entry stage, and a
COMMITTED ``AwaitHuman`` replays as a plain advance on every re-drive, so the index never moves).

Admits itself to the cog-worx external capability tier at construction time (via
``wombat.safety.tier_policy.bind_external_tier`` — the ONE sanctioned call site, TK-151/DEC-22):
dispatching an approved external side effect is this stage's entire purpose, so there is no
composition-root step to forget. Dispatch always goes through ``ctx.dispatch_approved`` (bypasses
ONLY ``ApprovalRequired`` — tier/schema/taint/timeout stay enforced, per
``cogworx.runtime.context.RunContext.dispatch_approved``): the human answer this stage just read
back off the journal IS the S10 approval grant, so there is nothing left to gain by routing through
the un-approved ``ctx.dispatch`` and risking an ``ApprovalRequired`` re-refusal of a decision a
human already made.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any, Protocol

from cogworx.capability.policy import StageToolPolicy
from cogworx.claims.provenance import Artifact, Provenance
from cogworx.loop.result import Done, StageResult
from cogworx.loop.stage import StageContext
from cogworx.runtime.context import RunContext

from wombat.safety.tier_policy import bind_external_tier

# DispatchApprovedStage's own terminal output kind.
DISPATCH_RESULT = "wombat.dispatch_result"

_VALID_DECISIONS = ("approve", "reject")


class MissingApprovalAnswer(RuntimeError):
    """The journaled human-input answer at ``ask_step_index`` was absent or carried no valid
    ``decision``. A structural protocol violation: this stage is only ever reached via its paired
    ``ProposeDispatchStage``'s ``AwaitHuman``, which the engine advances past only once
    ``Engine.provide_human_input`` has already committed an answer artifact — this should be
    unreachable in normal operation. Refused loud (recorded + raised), never a silent no-op."""


class ApprovalTrailWriter(Protocol):
    """The ``ActionTrailWriter`` surface ``DispatchApprovedStage`` needs (structural seam)."""

    def mark_dispatched(self, action_id: str, dispatched_at: datetime) -> object: ...

    def mark_cancelled(self, action_id: str, cancelled_at: datetime) -> object: ...

    def record_refusal(
        self,
        *,
        action_id: str,
        human_summary: str,
        target: str,
        proposed_at: datetime,
    ) -> object: ...


ArgsFromArtifact = Callable[[Artifact], dict[str, Any]]


class DispatchApprovedStage:
    """The shared approved-dispatch stage (TK-149, Q-91) — generic over ONE capability.

    Terminal (``transitions == ()``): every path out is a ``Done`` (approve or reject) or a loud
    raise (the missing-answer protocol violation) — the two-stage outbound graph ends here.
    """

    transitions: tuple[str, ...] = ()
    # Bound by bind_external_tier in __init__ (TK-151/DEC-22 — the ONE sanctioned admission call
    # site); declared here so mypy strict knows the attribute exists without a getattr/ignore.
    tool_policy: StageToolPolicy

    def __init__(
        self,
        *,
        name: str,
        capability: str,
        propose_stage_name: str,
        args_from_artifact: ArgsFromArtifact,
        writer: ApprovalTrailWriter,
        ask_step_index: int = 0,
    ) -> None:
        self.name = name
        self._capability = capability
        self._propose_stage_name = propose_stage_name
        self._args_from_artifact = args_from_artifact
        self._writer = writer
        self._ask_step_index = ask_step_index
        # The ONE sanctioned admission call site (TK-151/DEC-22) — scoped to this stage instance
        # only; the engine rebinds the gate fresh before every stage, so this never leaks.
        bind_external_tier(self)

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
                target=self._capability,
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
                    kind=DISPATCH_RESULT,
                    produced_by=self.name,
                    provenance=Provenance(source="system", confidence=1.0, recorded_at=now),
                    data={
                        "action_id": action_id,
                        "status": "cancelled",
                        "capability": self._capability,
                    },
                )
            )

        proposal_artifact = await ctx.last_output(self._propose_stage_name)
        if proposal_artifact is None:
            msg = (
                f"{self.name}: no committed output from propose stage "
                f"{self._propose_stage_name!r} — cannot build dispatch args"
            )
            raise RuntimeError(msg)
        args = self._args_from_artifact(proposal_artifact)

        # dispatch_approved is concrete-only on RunContext (NOT on the StageContext Protocol) —
        # a stage that needs it must type-narrow first (S9/S10: model-driven code cannot reach it).
        assert isinstance(ctx, RunContext), (
            f"{self.name}: dispatch_approved requires a RunContext — got {type(ctx).__name__}"
        )
        await ctx.dispatch_approved(self._capability, args)

        self._writer.mark_dispatched(action_id, now)
        return Done(
            output=Artifact(
                kind=DISPATCH_RESULT,
                produced_by=self.name,
                provenance=Provenance(source="system", confidence=1.0, recorded_at=now),
                data={
                    "action_id": action_id,
                    "status": "dispatched",
                    "capability": self._capability,
                },
            )
        )


__all__ = [
    "DISPATCH_RESULT",
    "ApprovalTrailWriter",
    "ArgsFromArtifact",
    "DispatchApprovedStage",
    "MissingApprovalAnswer",
]
