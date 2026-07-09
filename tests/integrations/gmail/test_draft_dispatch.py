"""TK-79 — draft_dispatch acceptance criteria (EP-18, Q-92/Q-93; TK-179/Q-94 dropped the
``ask_step_index`` ctor-arg shape in favor of a position-independent, stage-identity lookup).

AC1/AC2 use a REAL cog-worx ``Engine`` over the 3-stage construction Q-93 amended in: a
``compose_dispatch`` trigger stub (mirrors ``test_draft_composer.py``'s ``_ComposeDispatchStub``)
-> TK-78's REAL ``DraftComposer`` -> this ticket's REAL ``DraftDispatchStage`` (replacing TK-78's
frozen raising terminal stub; compose_dispatch commits at step 0, draft_composer parks
``AwaitHuman`` at step 1 — ``DraftDispatchStage`` now locates that step by stage identity rather
than a passed-in index). AC3 is unit-level: a minimal duck-typed ctx in the ``_FakeDraftContext``
style (mirrors ``test_draft_composer.py``/``test_approval_gate.py``), now carrying a fake
``journal``/``run_id`` seam so the stage's stage-identity lookup has something to walk.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest
from cogworx.capability.registry import Registry, function_capability
from cogworx.claims.provenance import Artifact, Provenance
from cogworx.loop.graph import StageGraph
from cogworx.loop.pathway import PathwayRegistry
from cogworx.loop.result import Done, StageResult, Transition
from cogworx.loop.stage import StageContext
from cogworx.loop.state import RunStatus
from cogworx.model.base import ModelResponse
from cogworx.model.registry import ModelRegistry
from cogworx.runtime.engine import Engine
from cogworx.testing.doubles import InMemoryGraphStore, InMemoryJournal, InMemoryLatentStore
from cogworx.testing.fake_model import ReplayModel

from wombat.gate.models import ItemKind
from wombat.integrations.gmail.draft_composer import DRAFT_CREATE_CAPABILITY, DraftComposer
from wombat.integrations.gmail.reply_intent import ReplyIntent
from wombat.stages.artifacts import COMPOSE_REQUEST, compose_request_to_artifact_data
from wombat.stages.dispatch_approved import MissingApprovalAnswer
from wombat.stages.draft_dispatch import DRAFT_DISPATCH_RESULT, DraftDispatchStage
from wombat.trail.schema import ActionType

_FIXED_NOW = datetime(2026, 7, 9, 9, 0, tzinfo=UTC)
_PATHWAY_ID = "draft-dispatch-test"


# --------------------------------------------------------------------------------- shared plumbing


def _reply_intent() -> ReplyIntent:
    return ReplyIntent(
        recipient="jane@example.com",
        subject_or_thread_ref="Q3 budget",
        reply_kind="high",
        quoted_excerpt="Quick update on the budget.",
        message_id="msg-1",
        matched_rules=("urgent-keyword",),
    )


class _RecordingWriter:
    """A recording fake trail writer satisfying every seam this ticket's graph needs:
    ``ProposalWriter`` (``DraftComposer``'s ``record_proposal``) and ``DraftApprovalTrailWriter``
    (``DraftDispatchStage``'s ``mark_dispatched``/``mark_cancelled``/``record_refusal``) — mirrors
    ``test_draft_composer.py``'s ``_RecordingWriter`` (the shared ``events`` ordering log) plus
    ``test_approval_gate.py``'s ``_RecordingWriter`` (the dispatched/cancelled lists)."""

    def __init__(self, events: list[str] | None = None) -> None:
        self._events = events if events is not None else []
        self.proposals: list[dict[str, object]] = []
        self.refusals: list[dict[str, object]] = []
        self.dispatched: list[tuple[str, datetime]] = []
        self.cancelled: list[tuple[str, datetime]] = []

    def record_proposal(
        self,
        *,
        action_id: str,
        action_type: ActionType,
        human_summary: str,
        target: str,
        proposed_at: datetime,
    ) -> None:
        self._events.append("record_proposal")
        self.proposals.append(
            {
                "action_id": action_id,
                "action_type": action_type,
                "human_summary": human_summary,
                "target": target,
                "proposed_at": proposed_at,
            }
        )

    def record_refusal(
        self,
        *,
        action_id: str,
        human_summary: str,
        target: str,
        proposed_at: datetime,
    ) -> None:
        self._events.append("record_refusal")
        self.refusals.append(
            {
                "action_id": action_id,
                "human_summary": human_summary,
                "target": target,
                "proposed_at": proposed_at,
            }
        )

    def mark_dispatched(self, action_id: str, dispatched_at: datetime) -> None:
        self._events.append("mark_dispatched")
        self.dispatched.append((action_id, dispatched_at))

    def mark_cancelled(self, action_id: str, cancelled_at: datetime) -> None:
        self._events.append("mark_cancelled")
        self.cancelled.append((action_id, cancelled_at))


def _register_draft_capability(registry: Registry, events: list[str]) -> list[tuple[str, str, str]]:
    """The fake, UNTAGGED external ``gmail.drafts.create`` capability — spies on every call."""
    calls: list[tuple[str, str, str]] = []

    async def _drafts_create(to: str, subject: str, body: str) -> str:
        events.append("drafts_create")
        calls.append((to, subject, body))
        return "draft-id-1"

    registry.register(
        function_capability(_drafts_create, name=DRAFT_CREATE_CAPABILITY, tier="external")
    )
    return calls


def _register_send_spy(registry: Registry) -> list[tuple[str, str]]:
    """A fake ``gmail.messages.send`` capability — must NEVER be called by this ticket's stage
    (CON-5/DEC-19/NG-5). Registered only to prove zero calls."""
    calls: list[tuple[str, str]] = []

    async def _send_email(to: str, body: str) -> str:
        calls.append((to, body))
        return "sent"

    registry.register(function_capability(_send_email, name="gmail.messages.send", tier="external"))
    return calls


# ------------------------------------------------------------------------ engine-level plumbing


def _trigger() -> Artifact:
    return Artifact(
        kind="wombat.draft_dispatch_trigger",
        produced_by="test",
        provenance=Provenance(source="system", confidence=1.0, recorded_at=_FIXED_NOW),
        data={},
    )


def _build_engine(graph: StageGraph, registry: Registry) -> tuple[Engine, InMemoryJournal]:
    """A REAL cog-worx Engine over a REAL Registry/ToolGate, entirely in-memory (mirrors
    ``test_draft_composer.py``'s ``_build_engine``)."""
    journal = InMemoryJournal()
    pathways = PathwayRegistry()
    pathways.register(_PATHWAY_ID, graph)
    models = ModelRegistry()
    models.register(
        "default",
        ReplayModel(
            [ModelResponse(text="phrased reply", model_id="replay", finish_reason="stop")]
        ),
    )
    engine = Engine(
        models=models,
        journal=journal,
        graph_store=InMemoryGraphStore(),
        latent=InMemoryLatentStore(),
        pathways=pathways,
        registry=registry,
        clock=lambda: _FIXED_NOW,
    )
    return engine, journal


class _ComposeDispatchStub:
    """The entry stub (mirrors ``test_draft_composer.py``'s ``_ComposeDispatchStub``): emits the
    scored DRAFT ``wombat.compose_request`` artifact carrying a ``ReplyIntent`` payload."""

    name = "compose_dispatch"
    transitions: tuple[str, ...] = ("draft_composer",)

    def __init__(self, reply_intent: ReplyIntent) -> None:
        self._reply_intent = reply_intent

    async def run(self, ctx: StageContext) -> StageResult:
        return Transition(
            to="draft_composer",
            output=Artifact(
                kind=COMPOSE_REQUEST,
                produced_by=self.name,
                provenance=Provenance(source="system", confidence=1.0, recorded_at=ctx.clock()),
                data=compose_request_to_artifact_data(
                    "item-1", ItemKind.DRAFT, self._reply_intent.to_payload()
                ),
            ),
        )


def _three_stage_graph(writer: _RecordingWriter, reply_intent: ReplyIntent) -> StageGraph:
    """The Q-93-amended 3-stage construction: compose_dispatch stub -> real DraftComposer -> this
    ticket's real DraftDispatchStage. compose_dispatch commits at step 0, draft_composer parks
    AwaitHuman at step 1 — DraftDispatchStage (TK-179/Q-94) locates that step itself by walking
    the run's committed step history for the last ``stage_name == "draft_composer"`` step, so no
    index is passed in here."""
    entry = _ComposeDispatchStub(reply_intent)
    composer = DraftComposer(writer=writer, clock=lambda: _FIXED_NOW)
    dispatch = DraftDispatchStage(writer=writer)
    return StageGraph([entry, composer, dispatch], entry="compose_dispatch")


# --------------------------------------------------------------------------------- AC1 (approve)


async def test_ac1_approve_finalizes_trail_as_dispatched_with_zero_capability_calls() -> None:
    registry = Registry()
    events: list[str] = []
    calls = _register_draft_capability(registry, events)
    send_calls = _register_send_spy(registry)
    writer = _RecordingWriter(events)
    reply_intent = _reply_intent()
    engine, _journal = _build_engine(_three_stage_graph(writer, reply_intent), registry)

    parked = await engine.run(
        run_id="ac1", session_id="ac1", pathway_id=_PATHWAY_ID, initial=_trigger()
    )
    assert parked.status is RunStatus.AWAITING_HUMAN
    # TK-78's pre-park call.
    assert calls == [("jane@example.com", "Re: Q3 budget", "phrased reply")]

    final = await engine.provide_human_input("ac1", payload={"decision": "approve"})

    assert final.status is RunStatus.COMPLETED
    # ZERO calls attributable to draft_dispatch — the fake drafts.create count stays at 1.
    assert calls == [("jane@example.com", "Re: Q3 budget", "phrased reply")]
    # gmail.messages.send is never called — the draft stays in Gmail Drafts.
    assert send_calls == []

    assert writer.dispatched == [("ac1:draft_composer", _FIXED_NOW)]
    assert writer.cancelled == []
    assert writer.refusals == []


# --------------------------------------------------------------------------------- AC2 (reject)


async def test_ac2_reject_cancels_trail_row_with_zero_calls_and_halts_cleanly() -> None:
    registry = Registry()
    events: list[str] = []
    calls = _register_draft_capability(registry, events)
    send_calls = _register_send_spy(registry)
    writer = _RecordingWriter(events)
    reply_intent = _reply_intent()
    engine, _journal = _build_engine(_three_stage_graph(writer, reply_intent), registry)

    parked = await engine.run(
        run_id="ac2", session_id="ac2", pathway_id=_PATHWAY_ID, initial=_trigger()
    )
    assert parked.status is RunStatus.AWAITING_HUMAN
    assert calls == [("jane@example.com", "Re: Q3 budget", "phrased reply")]

    final = await engine.provide_human_input("ac2", payload={"decision": "reject"})

    assert final.status is RunStatus.COMPLETED
    assert calls == [("jane@example.com", "Re: Q3 budget", "phrased reply")]  # unchanged — still 1
    assert send_calls == []

    assert writer.cancelled == [("ac2:draft_composer", _FIXED_NOW)]
    assert writer.dispatched == []
    assert writer.refusals == []


# ---------------------------------------------------------------------- AC3 (bypass, unit-level)


@dataclass(frozen=True)
class _FakeStepRecord:
    """A minimal duck-typed stand-in for cog-worx's ``StepRecord`` — only the two fields
    ``_locate_propose_step_index`` reads (``stage_name``/``step_index``)."""

    stage_name: str
    step_index: int


@dataclass(frozen=True)
class _FakeRunState:
    """A minimal duck-typed stand-in for cog-worx's ``RunState`` — only ``steps``, walked in
    reverse by ``_locate_propose_step_index`` (TK-179/Q-94)."""

    steps: tuple[_FakeStepRecord, ...]


class _FakeJournal:
    """A minimal duck-typed ``Journal`` exposing only ``load_run`` — what
    ``DraftDispatchStage._locate_propose_step_index`` needs to walk this run's step history by
    stage identity instead of a precomputed index."""

    def __init__(self, run_state: _FakeRunState | None) -> None:
        self._run_state = run_state

    async def load_run(self, run_id: str) -> _FakeRunState | None:
        return self._run_state


class _FakeDraftDispatchContext:
    """A minimal duck-typed StageContext exercising only what ``DraftDispatchStage.run`` touches
    when the journaled human-input answer is absent or malformed (mirrors
    ``test_approval_gate.py``'s ``_AnswerlessFakeContext``). Carries no ``dispatch`` method at
    all — a structural proof that this stage cannot reach for a capability even if it tried.

    ``propose_step_history`` defaults to a single ``("draft_composer", 1)`` step — mirroring the
    3-stage graph's real park position — so the stage-identity lookup (TK-179/Q-94) succeeds and
    the tests below exercise the answer-read/decision logic exactly as before. Pass an empty (or
    non-matching) history to exercise the "no propose-stage step found" refusal path."""

    def __init__(
        self,
        answer: Artifact | None,
        *,
        run_id: str = "bypass",
        propose_step_history: tuple[tuple[str, int], ...] = (("draft_composer", 1),),
    ) -> None:
        self._answer = answer
        self.run_id = run_id
        self.journal = _FakeJournal(
            _FakeRunState(
                steps=tuple(
                    _FakeStepRecord(stage_name=name, step_index=idx)
                    for name, idx in propose_step_history
                )
            )
        )

    async def read_human_input(self, step_index: int) -> Artifact | None:
        return self._answer

    @property
    def clock(self) -> Any:
        return lambda: _FIXED_NOW


async def test_ac3_absent_answer_refuses_loud_and_records_a_refusal() -> None:
    writer = _RecordingWriter()
    stage = DraftDispatchStage(writer=writer)
    ctx = _FakeDraftDispatchContext(answer=None, run_id="bypass-absent")

    with pytest.raises(MissingApprovalAnswer):
        await stage.run(ctx)  # type: ignore[arg-type]

    assert len(writer.refusals) == 1
    assert writer.refusals[0]["action_id"] == "bypass-absent:draft_composer"
    assert writer.dispatched == []
    assert writer.cancelled == []


async def test_ac3_malformed_decision_refuses_loud_and_records_a_refusal() -> None:
    writer = _RecordingWriter()
    stage = DraftDispatchStage(writer=writer)
    malformed_answer = Artifact(
        kind="human-input",
        produced_by="human",
        provenance=Provenance(source="human", confidence=1.0, recorded_at=_FIXED_NOW),
        data={"decision": "maybe"},
    )
    ctx = _FakeDraftDispatchContext(answer=malformed_answer, run_id="bypass-malformed")

    with pytest.raises(MissingApprovalAnswer):
        await stage.run(ctx)  # type: ignore[arg-type]

    assert len(writer.refusals) == 1
    assert writer.refusals[0]["action_id"] == "bypass-malformed:draft_composer"
    assert writer.dispatched == []
    assert writer.cancelled == []


async def test_no_propose_stage_step_refuses_loud_and_records_a_refusal() -> None:
    """TK-179 AC4: a run whose step history contains NO ``draft_composer`` step (a misconstruction)
    — the stage-identity lookup finds nothing, so this refuses loud exactly like a missing answer,
    never a silent no-op and never a capability dispatch."""
    writer = _RecordingWriter()
    stage = DraftDispatchStage(writer=writer)
    ctx = _FakeDraftDispatchContext(
        answer=None, run_id="bypass-no-propose-step", propose_step_history=()
    )

    with pytest.raises(MissingApprovalAnswer):
        await stage.run(ctx)  # type: ignore[arg-type]

    assert len(writer.refusals) == 1
    assert writer.refusals[0]["action_id"] == "bypass-no-propose-step:draft_composer"
    assert writer.dispatched == []
    assert writer.cancelled == []


# ---------------------------------------------------------------------- structural spot-checks


def test_gmail_messages_send_is_never_defined_in_this_module() -> None:
    """Structural regression guard (mirrors test_draft_composer.py's equivalent): this module
    defines no send capability and exports no ``*_CAPABILITY`` constant at all."""
    import wombat.stages.draft_dispatch as draft_dispatch_module

    capability_name_constants = {
        name: value
        for name, value in vars(draft_dispatch_module).items()
        if name in draft_dispatch_module.__all__ and name.endswith("_CAPABILITY")
    }
    assert capability_name_constants == {}


def test_draft_dispatch_stage_name_and_transitions_are_frozen() -> None:
    writer = _RecordingWriter()
    stage = DraftDispatchStage(writer=writer)

    assert stage.name == "draft_dispatch"
    assert stage.transitions == ()


def test_draft_dispatch_stage_binds_no_external_tool_policy() -> None:
    """This stage does NOT call bind_external_tier (unlike DispatchApprovedStage/DraftComposer) —
    dispatching nothing means admitting it to the external tier would be unearned surface."""
    writer = _RecordingWriter()
    stage = DraftDispatchStage(writer=writer)

    assert not hasattr(stage, "tool_policy")


async def test_approve_output_artifact_kind_and_status() -> None:
    writer = _RecordingWriter()
    stage = DraftDispatchStage(writer=writer)
    answer = Artifact(
        kind="human-input",
        produced_by="human",
        provenance=Provenance(source="human", confidence=1.0, recorded_at=_FIXED_NOW),
        data={"decision": "approve"},
    )
    ctx = _FakeDraftDispatchContext(answer=answer, run_id="approve-unit")

    result = await stage.run(ctx)  # type: ignore[arg-type]

    assert isinstance(result, Done)
    assert result.output is not None
    assert result.output.kind == DRAFT_DISPATCH_RESULT
    assert result.output.data["status"] == "dispatched"
    assert result.output.data["action_id"] == "approve-unit:draft_composer"


async def test_reject_output_artifact_kind_and_status() -> None:
    writer = _RecordingWriter()
    stage = DraftDispatchStage(writer=writer)
    answer = Artifact(
        kind="human-input",
        produced_by="human",
        provenance=Provenance(source="human", confidence=1.0, recorded_at=_FIXED_NOW),
        data={"decision": "reject"},
    )
    ctx = _FakeDraftDispatchContext(answer=answer, run_id="reject-unit")

    result = await stage.run(ctx)  # type: ignore[arg-type]

    assert isinstance(result, Done)
    assert result.output is not None
    assert result.output.kind == DRAFT_DISPATCH_RESULT
    assert result.output.data["status"] == "cancelled"
    assert result.output.data["action_id"] == "reject-unit:draft_composer"
