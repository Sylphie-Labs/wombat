"""wombat.stages.dispatch_base — the shared propose-side dispatch base (TK-149, EP-28, Q-91).

The structural CON-5/DEC-19/NG-5 guarantee, as code: every outbound side effect wombat ever takes
goes ``journal-proposed-action -> AwaitHuman -> dispatch_approved``. There is no shortcut. This
module owns the FIRST half of that pipeline — the propose side, shared by every concrete propose
stage (a stub ``ComposeGmailDraft``, a stub ``SubmitBrowserForm``, and later the real TK-78/TK-135
consumers) so none of them can independently reinvent (or accidentally skip) the journal-then-park
step. :mod:`wombat.stages.dispatch_approved` owns the paired SECOND half.

``ProposeDispatchStage`` is a base class, not a standalone stage: a concrete subclass sets
``name`` and implements :meth:`ProposeDispatchStage.build_proposal` to describe ONE proposed
action (a human-facing summary, a target, and the JSON-native args the eventual approved dispatch
will pass through unchanged). ``run()`` itself is NOT overridable — it is the one structural path
every subclass takes: derive the deterministic ``action_id``, ``record_proposal`` BEFORE parking
(so a kill between the insert and the human's answer still leaves the row behind — TK-146 AC5),
then return :class:`~cogworx.loop.result.AwaitHuman`. A subclass has no way to return anything
else — there is no code path from "proposed" to a live external side effect that skips the park.

``action_id = f"{run_id}:{name}"`` is deterministic and replay-stable (discharges the Q-63
action_id carry-forward): a re-drive of a still-parked run either replays the ALREADY-committed
AwaitHuman as a plain advance (``run()`` never executes again, per ``loop/result.py``) or, if the
step never committed, calls ``build_proposal``/``record_proposal`` again with the SAME action_id —
``ActionTrailWriter.record_proposal``'s ``ON CONFLICT (action_id) DO NOTHING`` makes that a no-op.

Non-goals (out of scope for TK-149): no real propose-stage content (TK-78 Gmail drafts, TK-135
browser forms), no boot wiring (TK-177), no Speak wiring (v1 speak is TK-101's injected seam, not
an external Capability; TK-164 revisits).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from cogworx.claims.provenance import Artifact, Provenance
from cogworx.loop.result import AwaitHuman, StageResult
from cogworx.loop.stage import StageContext

from wombat.trail.schema import ActionType

# The propose stage's own committed output kind — carries the JSON-native dispatch args forward
# on the AwaitHuman's ``output`` Artifact, so DispatchApprovedStage can pull them back later via
# ``ctx.last_output(propose_stage_name)`` (journal-backed, cold-resume safe, never a live handoff).
DISPATCH_PROPOSAL = "wombat.dispatch_proposal"


class ProposalWriter(Protocol):
    """The one ``ActionTrailWriter`` method a propose stage needs (structural seam, TK-149) —
    tests inject a recording fake instead of a real ``ActionTrailWriter``."""

    def record_proposal(
        self,
        *,
        action_id: str,
        action_type: ActionType,
        human_summary: str,
        target: str,
        proposed_at: datetime,
    ) -> object: ...


@dataclass(frozen=True, slots=True)
class ProposedAction:
    """ONE proposed outbound side effect — what a concrete propose stage builds per run.

    ``human_summary`` is both the trail row's human-facing text AND the ``AwaitHuman.question``
    (the human approves/rejects exactly what they were shown — no second, divergent description).
    ``target`` is the trail row's target column (a recipient address, a form URL, ...).
    ``dispatch_args`` is the JSON-native args mapping the eventual APPROVED dispatch will pass to
    the capability UNCHANGED — carried on the parked step's output Artifact.
    """

    human_summary: str
    target: str
    dispatch_args: dict[str, Any]


class ProposeDispatchStage(ABC):
    """Shared propose-side base (TK-149, Q-91): journal -> AwaitHuman, never a shortcut.

    A concrete subclass sets ``name`` (its own canonical stage name) and implements
    :meth:`build_proposal`; the constructor takes the paired dispatch stage's name (becomes
    ``AwaitHuman.to`` and ``self.transitions``), the ``ActionType`` for the trail row, and the
    injected trail writer. Subclasses never override :meth:`run`.
    """

    name: str
    transitions: tuple[str, ...]

    def __init__(
        self,
        *,
        writer: ProposalWriter,
        dispatch_stage_name: str,
        action_type: ActionType,
    ) -> None:
        self._writer = writer
        self.dispatch_stage_name = dispatch_stage_name
        self.action_type = action_type
        self.transitions = (dispatch_stage_name,)

    @abstractmethod
    async def build_proposal(self, ctx: StageContext) -> ProposedAction:
        """Build the human-facing summary + target + dispatch args for this proposed action."""
        raise NotImplementedError

    async def run(self, ctx: StageContext) -> StageResult:
        proposal = await self.build_proposal(ctx)
        action_id = f"{ctx.run_id}:{self.name}"
        now = ctx.clock()

        self._writer.record_proposal(
            action_id=action_id,
            action_type=self.action_type,
            human_summary=proposal.human_summary,
            target=proposal.target,
            proposed_at=now,
        )

        return AwaitHuman(
            question=proposal.human_summary,
            to=self.dispatch_stage_name,
            output=Artifact(
                kind=DISPATCH_PROPOSAL,
                produced_by=self.name,
                provenance=Provenance(source="system", confidence=1.0, recorded_at=now),
                data=proposal.dispatch_args,
            ),
        )


__all__ = [
    "DISPATCH_PROPOSAL",
    "ProposalWriter",
    "ProposeDispatchStage",
    "ProposedAction",
]
