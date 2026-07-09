"""TK-149 — approval gate wiring acceptance criteria (EP-28, Q-91).

The structural CON-5/DEC-19/NG-5 guarantee, proven end-to-end with a REAL cog-worx ``Engine``
driving a REAL ``Registry``/``ToolGate`` over two STUB propose->dispatch pathways (a Gmail-draft
stub and a browser-form-submit stub — the real consumers are TK-78/TK-135, out of scope here) plus
one bypass pathway proving there is no shortcut around the gate:

  AC1 the propose stage (``ComposeGmailDraft``, riding ``ProposeDispatchStage``) returns
      ``AwaitHuman`` — the run parks AWAITING_HUMAN, a PENDING trail row is written, and the spy
      capability sees ZERO calls.
  AC2 ``provide_human_input(payload={'decision': 'approve'})`` re-drives the engine ->
      ``DispatchApprovedStage`` makes EXACTLY ONE capability call with the journaled payload; a
      second ``provide_human_input`` is an idempotent no-op (no additional calls).
  AC3 ``payload={'decision': 'reject'}`` -> ZERO capability calls, the trail row is cancelled, the
      run completes cleanly.
  AC4 a SECOND stub (``SubmitBrowserForm``) rides the SAME base over a mock-Playwright-shaped
      capability — proves the pattern generalizes, not just the Gmail case.
  AC5 (DSN-gated, the ONE pg test in this ticket) the propose stage's ``record_proposal`` write,
      via the REAL ``ActionTrailWriter``, survives independent of what happens next in-process —
      queried directly off Postgres right after the propose stage parks.
  AC6 a stage that dispatches the external capability directly WITHOUT going through
      ``DispatchApprovedStage`` (never admitted to the external tier) is refused by
      ``check_dispatch`` with ``TierViolation`` (not ``ApprovalRequired`` — the tier gate is
      first), and a ``blocked_by_taint``-shaped refusal row is written.

Everything except AC5 is in-memory / fast (mirrors ``tests/safety/test_taint_latch_adversarial.py``
and ``tests/integration/test_brief_pathway_e2e.py``'s own construction).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest
from cogworx.capability.policy import StageToolPolicy, TierViolation, ToolGate
from cogworx.capability.registry import Registry, function_capability
from cogworx.claims.provenance import Artifact, Provenance
from cogworx.loop.graph import StageGraph
from cogworx.loop.pathway import PathwayRegistry
from cogworx.loop.result import StageResult
from cogworx.loop.stage import StageContext
from cogworx.loop.state import RunStatus
from cogworx.model.registry import ModelRegistry
from cogworx.runtime.engine import Engine
from cogworx.testing.doubles import InMemoryGraphStore, InMemoryJournal, InMemoryLatentStore
from cogworx.testing.fake_model import ReplayModel

from wombat.stages.dispatch_approved import (
    DispatchApprovedStage,
    MissingApprovalAnswer,
)
from wombat.stages.dispatch_base import ProposedAction, ProposeDispatchStage
from wombat.trail.schema import ActionType, ensure_schema
from wombat.trail.writer import ActionTrailWriter, InsertResult, TransitionResult

_NOW = datetime(2026, 7, 9, 9, 0, tzinfo=UTC)
_PATHWAY_ID = "dispatch-gate-test"


# --------------------------------------------------------------------------------- shared plumbing


def _trigger() -> Artifact:
    return Artifact(
        kind="wombat.dispatch_gate_trigger",
        produced_by="test",
        provenance=Provenance(source="system", confidence=1.0, recorded_at=_NOW),
        data={},
    )


def _build_engine(graph: StageGraph, registry: Registry) -> tuple[Engine, InMemoryJournal]:
    """A REAL cog-worx Engine over a REAL Registry/ToolGate, entirely in-memory (mirrors
    ``test_await_human_resume_live.py``'s ``_live_engine`` construction)."""
    journal = InMemoryJournal()
    pathways = PathwayRegistry()
    pathways.register(_PATHWAY_ID, graph)
    models = ModelRegistry()
    models.register("default", ReplayModel([]))
    engine = Engine(
        models=models,
        journal=journal,
        graph_store=InMemoryGraphStore(),
        latent=InMemoryLatentStore(),
        pathways=pathways,
        registry=registry,
        clock=lambda: _NOW,
    )
    return engine, journal


class _RecordingWriter:
    """A recording fake trail writer (no DSN) satisfying both ``ProposalWriter`` and
    ``ApprovalTrailWriter`` — mirrors ``_RecordingFakeWriter`` in
    ``test_taint_latch_adversarial.py``."""

    def __init__(self) -> None:
        self.proposals: list[dict[str, object]] = []
        self.dispatched: list[tuple[str, datetime]] = []
        self.cancelled: list[tuple[str, datetime]] = []
        self.refusals: list[dict[str, object]] = []

    def record_proposal(
        self,
        *,
        action_id: str,
        action_type: ActionType,
        human_summary: str,
        target: str,
        proposed_at: datetime,
    ) -> InsertResult:
        self.proposals.append(
            {
                "action_id": action_id,
                "action_type": action_type,
                "human_summary": human_summary,
                "target": target,
                "proposed_at": proposed_at,
            }
        )
        return InsertResult.INSERTED

    def mark_dispatched(self, action_id: str, dispatched_at: datetime) -> TransitionResult:
        self.dispatched.append((action_id, dispatched_at))
        return TransitionResult.APPLIED

    def mark_cancelled(self, action_id: str, cancelled_at: datetime) -> TransitionResult:
        self.cancelled.append((action_id, cancelled_at))
        return TransitionResult.APPLIED

    def record_refusal(
        self,
        *,
        action_id: str,
        human_summary: str,
        target: str,
        proposed_at: datetime,
    ) -> InsertResult:
        self.refusals.append(
            {
                "action_id": action_id,
                "human_summary": human_summary,
                "target": target,
                "proposed_at": proposed_at,
            }
        )
        return InsertResult.INSERTED


# --------------------------------------------------------------------- Gmail-draft stub (AC1-AC3)

_GMAIL_CAPABILITY = "gmail_draft_create"
_PROPOSE_GMAIL = "compose_gmail_draft"
_DISPATCH_GMAIL = "dispatch_gmail_draft"


def _gmail_capability_and_calls(
    registry: Registry,
) -> list[tuple[str, str, str]]:
    calls: list[tuple[str, str, str]] = []

    async def _draft_create(to: str, subject: str, body: str) -> str:
        calls.append((to, subject, body))
        return f"draft-id-for-{to}"

    registry.register(function_capability(_draft_create, name=_GMAIL_CAPABILITY, tier="external"))
    return calls


class ComposeGmailDraft(ProposeDispatchStage):
    """Stub propose stage riding the shared base (AC1/AC2/AC3) — the real consumer is TK-78."""

    name = _PROPOSE_GMAIL

    def __init__(self, *, writer: Any) -> None:
        super().__init__(
            writer=writer,
            dispatch_stage_name=_DISPATCH_GMAIL,
            action_type=ActionType.DRAFT_EMAIL,
        )

    async def build_proposal(self, ctx: StageContext) -> ProposedAction:
        return ProposedAction(
            human_summary="Draft a reply to jane@example.com about the Q3 budget?",
            target="jane@example.com",
            dispatch_args={
                "to": "jane@example.com",
                "subject": "Q3 budget",
                "body": "Here is the Q3 budget draft.",
            },
        )


def _gmail_graph(writer: Any) -> StageGraph:
    propose = ComposeGmailDraft(writer=writer)
    dispatch = DispatchApprovedStage(
        name=_DISPATCH_GMAIL,
        capability=_GMAIL_CAPABILITY,
        propose_stage_name=_PROPOSE_GMAIL,
        args_from_artifact=lambda art: dict(art.data),
        writer=writer,
    )
    return StageGraph([propose, dispatch], entry=_PROPOSE_GMAIL)


# --------------------------------------------------------------------------------- AC1


async def test_ac1_propose_parks_awaiting_human_with_pending_row_and_zero_dispatch() -> None:
    registry = Registry()
    calls = _gmail_capability_and_calls(registry)
    writer = _RecordingWriter()
    engine, _journal = _build_engine(_gmail_graph(writer), registry)

    final = await engine.run(
        run_id="ac1", session_id="ac1", pathway_id=_PATHWAY_ID, initial=_trigger()
    )

    assert final.status is RunStatus.AWAITING_HUMAN
    assert calls == []  # the spy capability sees ZERO calls pre-approval

    assert len(writer.proposals) == 1
    proposal = writer.proposals[0]
    assert proposal["action_id"] == f"ac1:{_PROPOSE_GMAIL}"
    assert proposal["action_type"] is ActionType.DRAFT_EMAIL
    assert proposal["target"] == "jane@example.com"


# --------------------------------------------------------------------------------- AC2


async def test_ac2_approve_dispatches_exactly_once_with_journaled_payload() -> None:
    registry = Registry()
    calls = _gmail_capability_and_calls(registry)
    writer = _RecordingWriter()
    engine, _journal = _build_engine(_gmail_graph(writer), registry)

    parked = await engine.run(
        run_id="ac2", session_id="ac2", pathway_id=_PATHWAY_ID, initial=_trigger()
    )
    assert parked.status is RunStatus.AWAITING_HUMAN

    final = await engine.provide_human_input("ac2", payload={"decision": "approve"})

    assert final.status is RunStatus.COMPLETED
    assert calls == [("jane@example.com", "Q3 budget", "Here is the Q3 budget draft.")]
    assert len(writer.dispatched) == 1
    assert writer.dispatched[0][0] == f"ac2:{_PROPOSE_GMAIL}"

    # A second provide_human_input on a completed run is an idempotent no-op — no extra calls.
    again = await engine.provide_human_input("ac2", payload={"decision": "approve"})
    assert again.status is RunStatus.COMPLETED
    assert len(calls) == 1
    assert len(writer.dispatched) == 1


# --------------------------------------------------------------------------------- AC3


async def test_ac3_reject_makes_zero_calls_and_cancels_the_trail_row() -> None:
    registry = Registry()
    calls = _gmail_capability_and_calls(registry)
    writer = _RecordingWriter()
    engine, _journal = _build_engine(_gmail_graph(writer), registry)

    parked = await engine.run(
        run_id="ac3", session_id="ac3", pathway_id=_PATHWAY_ID, initial=_trigger()
    )
    assert parked.status is RunStatus.AWAITING_HUMAN

    final = await engine.provide_human_input("ac3", payload={"decision": "reject"})

    assert final.status is RunStatus.COMPLETED
    assert calls == []
    assert writer.dispatched == []
    assert len(writer.cancelled) == 1
    assert writer.cancelled[0][0] == f"ac3:{_PROPOSE_GMAIL}"


# ------------------------------------------------------------- browser-form-submit stub (AC4)

_BROWSER_CAPABILITY = "browser_submit_form"
_PROPOSE_BROWSER = "submit_browser_form"
_DISPATCH_BROWSER = "dispatch_browser_form"


def _browser_capability_and_calls(
    registry: Registry,
) -> list[tuple[str, dict[str, str]]]:
    calls: list[tuple[str, dict[str, str]]] = []

    async def _submit_form(url: str, fields: dict[str, str]) -> str:
        calls.append((url, fields))
        return "submitted"

    registry.register(function_capability(_submit_form, name=_BROWSER_CAPABILITY, tier="external"))
    return calls


class SubmitBrowserForm(ProposeDispatchStage):
    """A SECOND stub over the SAME base (AC4 — pattern generality, mock-Playwright-shaped)."""

    name = _PROPOSE_BROWSER

    def __init__(self, *, writer: Any) -> None:
        super().__init__(
            writer=writer,
            dispatch_stage_name=_DISPATCH_BROWSER,
            action_type=ActionType.FORM_SUBMIT,
        )

    async def build_proposal(self, ctx: StageContext) -> ProposedAction:
        return ProposedAction(
            human_summary="Submit the contact form on https://example.com/contact?",
            target="https://example.com/contact",
            dispatch_args={
                "url": "https://example.com/contact",
                "fields": {"name": "Jim", "message": "hello"},
            },
        )


def _browser_graph(writer: Any) -> StageGraph:
    propose = SubmitBrowserForm(writer=writer)
    dispatch = DispatchApprovedStage(
        name=_DISPATCH_BROWSER,
        capability=_BROWSER_CAPABILITY,
        propose_stage_name=_PROPOSE_BROWSER,
        args_from_artifact=lambda art: dict(art.data),
        writer=writer,
    )
    return StageGraph([propose, dispatch], entry=_PROPOSE_BROWSER)


async def test_ac4_browser_stub_zero_invocations_before_approval() -> None:
    registry = Registry()
    calls = _browser_capability_and_calls(registry)
    writer = _RecordingWriter()
    engine, _journal = _build_engine(_browser_graph(writer), registry)

    parked = await engine.run(
        run_id="ac4", session_id="ac4", pathway_id=_PATHWAY_ID, initial=_trigger()
    )
    assert parked.status is RunStatus.AWAITING_HUMAN
    assert calls == []  # mock-Playwright submit sees ZERO invocations before approval

    final = await engine.provide_human_input("ac4", payload={"decision": "approve"})
    assert final.status is RunStatus.COMPLETED
    assert calls == [
        ("https://example.com/contact", {"name": "Jim", "message": "hello"}),
    ]


# --------------------------------------------------------------------------------- AC5 (DSN-gated)

_DSN = os.environ.get("WOMBAT_TEST_PG_DSN")

_requires_pg = pytest.mark.skipif(
    not _DSN,
    reason=(
        "WOMBAT_TEST_PG_DSN is not set — skipping the real-Postgres proposal-survives-a-kill "
        "integration test. Start one with:\n"
        "  docker run --rm -d -p 5433:5432 -e POSTGRES_PASSWORD=wombat postgres:16\n"
        "then export WOMBAT_TEST_PG_DSN=postgresql://postgres:wombat@localhost:5433/postgres"
    ),
)


@pytest.fixture
def clean_table() -> None:
    assert _DSN is not None
    with psycopg.connect(_DSN) as conn:
        ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE action_trail_projection")
        conn.commit()


@_requires_pg
async def test_ac5_pending_row_survives_via_the_real_action_trail_writer(clean_table: None) -> None:
    """The propose stage's record_proposal write, via the REAL ActionTrailWriter, is durably in
    Postgres BEFORE the AwaitHuman is even returned to the engine — a kill (or just: never
    resuming this run at all, as this test does) leaves the PENDING row behind regardless."""
    assert _DSN is not None
    registry = Registry()
    _gmail_capability_and_calls(registry)
    writer = ActionTrailWriter(_DSN)
    try:
        engine, _journal = _build_engine(_gmail_graph(writer), registry)

        final = await engine.run(
            run_id="ac5", session_id="ac5", pathway_id=_PATHWAY_ID, initial=_trigger()
        )
        assert final.status is RunStatus.AWAITING_HUMAN

        with psycopg.connect(_DSN) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT action_id, action_type, human_summary, target, status "
                "FROM action_trail_projection WHERE action_id = %s",
                (f"ac5:{_PROPOSE_GMAIL}",),
            )
            row = cur.fetchone()
        assert row is not None
        action_id, action_type, human_summary, target, status = row
        assert action_id == f"ac5:{_PROPOSE_GMAIL}"
        assert action_type == ActionType.DRAFT_EMAIL.value
        assert target == "jane@example.com"
        assert status == "pending"
        assert human_summary
    finally:
        writer.close()


# --------------------------------------------------------------------------------- AC6 (bypass)

_BYPASS_CAPABILITY = "gmail_draft_create_bypass"
_BYPASS_STAGE_NAME = "unadmitted_direct_dispatch"


class _UnadmittedDirectDispatchStage:
    """A stage that dispatches the external capability DIRECTLY, never wired to
    ``ProposeDispatchStage``/``DispatchApprovedStage`` and never admitted to the external tier
    (no ``tool_policy`` attribute) — proves there is no shortcut around the gate (AC6)."""

    name: str = _BYPASS_STAGE_NAME
    transitions: tuple[str, ...] = ()

    def __init__(self, *, writer: Any) -> None:
        self._writer = writer

    async def run(self, ctx: StageContext) -> StageResult:
        now = ctx.clock()
        action_id = f"{ctx.run_id}:{self.name}"
        try:
            await ctx.dispatch(_BYPASS_CAPABILITY, {"to": "x", "subject": "y", "body": "z"})
        except TierViolation:
            self._writer.record_refusal(
                action_id=action_id,
                human_summary="blocked: direct dispatch from a non-admitted stage",
                target=_BYPASS_CAPABILITY,
                proposed_at=now,
            )
            raise
        raise AssertionError("unreachable: dispatch must have raised TierViolation")


async def test_ac6_direct_dispatch_from_a_non_admitted_stage_raises_tier_violation() -> None:
    registry = Registry()
    calls: list[tuple[str, str, str]] = []

    async def _draft_create(to: str, subject: str, body: str) -> str:
        calls.append((to, subject, body))
        return "unreachable"

    registry.register(function_capability(_draft_create, name=_BYPASS_CAPABILITY, tier="external"))

    writer = _RecordingWriter()
    graph = StageGraph([_UnadmittedDirectDispatchStage(writer=writer)], entry=_BYPASS_STAGE_NAME)
    engine, _journal = _build_engine(graph, registry)

    with pytest.raises(TierViolation):
        await engine.run(
            run_id="ac6", session_id="ac6", pathway_id=_PATHWAY_ID, initial=_trigger()
        )

    assert calls == []  # the tier gate refused before the capability was ever invoked
    assert len(writer.refusals) == 1
    assert writer.refusals[0]["target"] == _BYPASS_CAPABILITY


def test_ac6_unadmitted_default_policy_excludes_external_tier_directly() -> None:
    """Direct proof (mirrors test_taint_latch_adversarial.py's style) that the DEFAULT policy
    (what a stage with no ``tool_policy`` attribute gets bound) does not expose the external
    tier at all — the structural reason AC6's TierViolation fires."""
    registry = Registry()

    async def _noop() -> str:
        return "unreachable"

    registry.register(function_capability(_noop, name="external_noop", tier="external"))
    gate = ToolGate(registry, policy=StageToolPolicy())  # the engine's DEFAULT_TOOL_POLICY shape

    assert "external_noop" not in {spec.name for spec in gate.exposed_specs()}
    with pytest.raises(TierViolation):
        gate.check_dispatch("external_noop")


# ----------------------------------------------------------- DispatchApprovedStage refusal-loud


@dataclass(frozen=True)
class _FakeStepRecord:
    """A minimal duck-typed stand-in for cog-worx's ``StepRecord`` — only the two fields
    ``DispatchApprovedStage._locate_propose_step_index`` reads (TK-179/Q-94)."""

    stage_name: str
    step_index: int


@dataclass(frozen=True)
class _FakeRunState:
    """A minimal duck-typed stand-in for cog-worx's ``RunState`` — only ``steps``, walked in
    reverse by ``_locate_propose_step_index`` (TK-179/Q-94)."""

    steps: tuple[_FakeStepRecord, ...]


class _FakeJournal:
    """A minimal duck-typed ``Journal`` exposing only ``load_run`` — what
    ``DispatchApprovedStage._locate_propose_step_index`` needs to walk this run's step history by
    stage identity instead of a precomputed index."""

    def __init__(self, run_state: _FakeRunState | None) -> None:
        self._run_state = run_state

    async def load_run(self, run_id: str) -> _FakeRunState | None:
        return self._run_state


class _AnswerlessFakeContext:
    """A minimal duck-typed StageContext exercising only what DispatchApprovedStage.run touches
    when the journaled human-input answer is absent — proves the refuse-loud path directly rather
    than contriving an engine-level replay that can never actually produce this state.

    ``propose_step_history`` defaults to a single ``(_PROPOSE_GMAIL, 0)`` step (the propose
    stage's real park position in the 2-stage graph), so the stage-identity lookup (TK-179/Q-94)
    succeeds and the test below exercises the missing-ANSWER refusal, not the missing-STEP one."""

    def __init__(
        self,
        *,
        propose_step_history: tuple[tuple[str, int], ...] = ((_PROPOSE_GMAIL, 0),),
    ) -> None:
        self.run_id = "refuse-loud"
        self.journal = _FakeJournal(
            _FakeRunState(
                steps=tuple(
                    _FakeStepRecord(stage_name=name, step_index=idx)
                    for name, idx in propose_step_history
                )
            )
        )

    async def read_human_input(self, step_index: int) -> Artifact | None:
        return None

    @property
    def clock(self) -> Any:
        return lambda: _NOW


async def test_missing_human_answer_refuses_loud_and_records_a_refusal() -> None:
    writer = _RecordingWriter()
    stage = DispatchApprovedStage(
        name=_DISPATCH_GMAIL,
        capability=_GMAIL_CAPABILITY,
        propose_stage_name=_PROPOSE_GMAIL,
        args_from_artifact=lambda art: dict(art.data),
        writer=writer,
    )

    with pytest.raises(MissingApprovalAnswer):
        await stage.run(_AnswerlessFakeContext())  # type: ignore[arg-type]

    assert len(writer.refusals) == 1
    assert writer.refusals[0]["action_id"] == f"refuse-loud:{_PROPOSE_GMAIL}"
    assert writer.dispatched == []
    assert writer.cancelled == []


async def test_no_propose_stage_step_refuses_loud_and_records_a_refusal() -> None:
    """TK-179 AC4: a run whose step history contains NO propose-stage step (a misconstruction) —
    the stage-identity lookup finds nothing, so this refuses loud exactly like a missing answer,
    never a silent no-op and never a capability dispatch."""
    writer = _RecordingWriter()
    stage = DispatchApprovedStage(
        name=_DISPATCH_GMAIL,
        capability=_GMAIL_CAPABILITY,
        propose_stage_name=_PROPOSE_GMAIL,
        args_from_artifact=lambda art: dict(art.data),
        writer=writer,
    )
    ctx = _AnswerlessFakeContext(propose_step_history=())

    with pytest.raises(MissingApprovalAnswer):
        await stage.run(ctx)  # type: ignore[arg-type]

    assert len(writer.refusals) == 1
    assert writer.refusals[0]["action_id"] == f"refuse-loud:{_PROPOSE_GMAIL}"
    assert writer.dispatched == []
    assert writer.cancelled == []


# ------------------------------------------------------------------ DEC-26 invariant spot-check


def test_dispatch_approved_stage_never_disables_taint_drops_external() -> None:
    """DEC-26 binding constraint: no StageToolPolicy(taint_drops_external=False) anywhere. The
    tier admission DispatchApprovedStage binds via bind_external_tier must keep the invariant."""
    writer = _RecordingWriter()
    stage = DispatchApprovedStage(
        name=_DISPATCH_GMAIL,
        capability=_GMAIL_CAPABILITY,
        propose_stage_name=_PROPOSE_GMAIL,
        args_from_artifact=lambda art: dict(art.data),
        writer=writer,
    )
    policy: StageToolPolicy = stage.tool_policy
    assert policy.taint_drops_external is True
    assert "external" in policy.allowed_tiers
