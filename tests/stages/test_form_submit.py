"""TK-135 acceptance criteria — ``FormSubmitStage`` (Q-114): journal ONE proposed
``submit_form`` action, park ``AwaitHuman``, then the shared ``DispatchApprovedStage`` dispatches
exactly ONE approved ``submit_form`` call — never more, never less, and never past a taint latch
even when a human approves.

Harness mirrors ``tests/safety/test_approval_gate.py`` exactly: a REAL cog-worx ``Engine`` +
``InMemoryJournal`` driving a REAL ``Registry``/``ToolGate``, a recording fake trail writer (no
DSN), and a fake ``"browser"`` capability (external tier, UNTAGGED — the same taint posture as
the real ``PlaywrightCapability``) standing in for TK-131/TK-132/TK-135's real Playwright session.
The dispatch side is the EXISTING generic ``DispatchApprovedStage`` — zero new dispatch-side code
(Q-114(d)).

  AC1 a producer -> form_submit -> form_dispatch graph parks AWAITING_HUMAN: ``record_proposal``
      landed with the exact URL + every field in the summary, ZERO capability invocations.
  AC2 decision=approve -> exactly ONE ``submit_form`` invocation with the journaled args, then
      ``mark_dispatched`` with a timestamp.
  AC3 decision=reject -> zero invocations, ``mark_cancelled``, the fake browser session untouched.
  AC4(i) a stage WITHOUT ``tool_policy`` dispatching ``browser`` through the gated path raises
      ``TierViolation`` (``DEFAULT_TOOL_POLICY`` has no external tier).
  AC4(ii) THE Q-114(a) RULING: a drive that is ALREADY tainted before ``form_submit`` even parks
      — approving the proposal still gets refused. ``ctx.dispatch_approved`` raises
      ``TierViolation``, zero invocations, ``mark_dispatched`` never called: human approval never
      re-admits the external tier on a tainted drive. Not caught anywhere — the loud failure is
      the designed outcome.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from cogworx.capability.policy import TierViolation, ToolGate
from cogworx.capability.registry import Registry, function_capability
from cogworx.capability.router import dispatch_one
from cogworx.claims.provenance import Artifact, Provenance
from cogworx.loop.graph import StageGraph
from cogworx.loop.pathway import PathwayRegistry
from cogworx.loop.result import StageResult, Transition
from cogworx.loop.stage import StageContext
from cogworx.loop.state import RunStatus
from cogworx.model.registry import ModelRegistry
from cogworx.runtime.engine import Engine
from cogworx.testing.doubles import InMemoryGraphStore, InMemoryJournal, InMemoryLatentStore
from cogworx.testing.fake_model import ReplayModel

from wombat.safety.taint import UNTRUSTED_SOURCE_TAG
from wombat.stages.dispatch_approved import DispatchApprovedStage
from wombat.stages.form_submit import FORM_SUBMIT_REQUEST, FormSubmitStage
from wombat.trail.schema import ActionType

_NOW = datetime(2026, 7, 10, 9, 0, tzinfo=UTC)
_PATHWAY_ID = "form-submit-test"

_BROWSER_CAPABILITY = "browser"
_PROPOSE = "form_submit"
_DISPATCH = "form_dispatch"

_FORM_URL = "https://example.com/contact"
_FORM_FIELDS = [
    {"role": "textbox", "name": "Your name", "value": "Jim"},
    {"role": "textbox", "name": "Message", "value": "Hello there"},
]
_FORM_SUBMIT = {"role": "button", "name": "Send"}


# --------------------------------------------------------------------------------- shared plumbing


def _trigger() -> Artifact:
    return Artifact(
        kind="wombat.form_submit_trigger",
        produced_by="test",
        provenance=Provenance(source="system", confidence=1.0, recorded_at=_NOW),
        data={},
    )


def _build_engine(graph: StageGraph, registry: Registry) -> tuple[Engine, InMemoryJournal]:
    """A REAL cog-worx Engine over a REAL Registry/ToolGate, entirely in-memory (mirrors
    ``tests/safety/test_approval_gate.py``'s ``_build_engine``)."""
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
    """A recording fake trail writer (no DSN) — mirrors ``test_approval_gate.py``'s
    ``_RecordingWriter``."""

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
    ) -> Any:
        self.proposals.append(
            {
                "action_id": action_id,
                "action_type": action_type,
                "human_summary": human_summary,
                "target": target,
                "proposed_at": proposed_at,
            }
        )

    def mark_dispatched(self, action_id: str, dispatched_at: datetime) -> Any:
        self.dispatched.append((action_id, dispatched_at))

    def mark_cancelled(self, action_id: str, cancelled_at: datetime) -> Any:
        self.cancelled.append((action_id, cancelled_at))

    def record_refusal(
        self,
        *,
        action_id: str,
        human_summary: str,
        target: str,
        proposed_at: datetime,
    ) -> Any:
        self.refusals.append(
            {
                "action_id": action_id,
                "human_summary": human_summary,
                "target": target,
                "proposed_at": proposed_at,
            }
        )


def _register_fake_browser(registry: Registry) -> list[dict[str, Any]]:
    """Fake ``browser`` capability — external tier, UNTAGGED (the same taint posture as the real
    ``PlaywrightCapability``: any dispatch of it taints the drive)."""
    calls: list[dict[str, Any]] = []

    async def _browser(
        action: str, url: str, fields: list[dict[str, str]], submit: dict[str, str]
    ) -> dict[str, Any]:
        calls.append({"action": action, "url": url, "fields": fields, "submit": submit})
        return {"ok": True, "snapshot": []}

    registry.register(function_capability(_browser, name=_BROWSER_CAPABILITY, tier="external"))
    return calls


_TAINT_CAPABILITY = "read_untrusted_page"


async def _read_untrusted(source: str) -> str:
    return f"untrusted content from {source}"


def _register_taint_capability(registry: Registry) -> None:
    """A tagged ``untrusted-source`` read-tier capability — dispatching it structurally latches
    ``TaintState`` (the same mechanic ``wombat.safety.taint``/``wombat.stages.ingest_web_page``
    wire for real ingest stages; a fake stand-in here since no upstream taint source is part of
    this ticket's scope)."""
    registry.register(
        function_capability(_read_untrusted, name=_TAINT_CAPABILITY, tier="read"),
        tags=(UNTRUSTED_SOURCE_TAG,),
    )


class _FormRequestProducer:
    """Stands in for whatever real upstream stage decides the target form + fields (out of scope
    for this ticket) — emits the ``FORM_SUBMIT_REQUEST`` artifact
    ``FormSubmitStage.build_proposal`` expects."""

    name = "form_request_producer"
    transitions = (_PROPOSE,)

    async def run(self, ctx: StageContext) -> StageResult:
        return Transition(
            to=_PROPOSE,
            output=Artifact(
                kind=FORM_SUBMIT_REQUEST,
                produced_by=self.name,
                provenance=Provenance(source="system", confidence=1.0, recorded_at=ctx.clock()),
                data={"url": _FORM_URL, "fields": _FORM_FIELDS, "submit": _FORM_SUBMIT},
            ),
        )


class _TaintingFormRequestProducer:
    """The Q-114(a) ruling fixture (AC4(ii)): taints the drive via a REAL gated dispatch of a
    tagged ``untrusted-source`` capability BEFORE emitting the ``FORM_SUBMIT_REQUEST`` artifact —
    the drive is ALREADY tainted by the time ``form_submit`` even parks."""

    name = "tainting_form_request_producer"
    transitions = (_PROPOSE,)

    async def run(self, ctx: StageContext) -> StageResult:
        await ctx.dispatch(_TAINT_CAPABILITY, {"source": "untrusted-page"})
        return Transition(
            to=_PROPOSE,
            output=Artifact(
                kind=FORM_SUBMIT_REQUEST,
                produced_by=self.name,
                provenance=Provenance(source="system", confidence=1.0, recorded_at=ctx.clock()),
                data={"url": _FORM_URL, "fields": _FORM_FIELDS, "submit": _FORM_SUBMIT},
            ),
        )


def _graph(writer: Any, *, producer: Any) -> StageGraph:
    propose = FormSubmitStage(writer=writer, upstream_stage_name=producer.name)
    dispatch = DispatchApprovedStage(
        name=_DISPATCH,
        capability=_BROWSER_CAPABILITY,
        propose_stage_name=_PROPOSE,
        args_from_artifact=lambda art: dict(art.data),
        writer=writer,
    )
    return StageGraph([producer, propose, dispatch], entry=producer.name)


# --------------------------------------------------------------------------------- AC1


async def test_ac1_propose_parks_awaiting_human_with_pending_row_and_zero_dispatch() -> None:
    registry = Registry()
    calls = _register_fake_browser(registry)
    writer = _RecordingWriter()
    engine, _journal = _build_engine(_graph(writer, producer=_FormRequestProducer()), registry)

    final = await engine.run(
        run_id="ac1", session_id="ac1", pathway_id=_PATHWAY_ID, initial=_trigger()
    )

    assert final.status is RunStatus.AWAITING_HUMAN
    assert calls == []  # the fake browser capability sees ZERO calls pre-approval

    assert len(writer.proposals) == 1
    proposal = writer.proposals[0]
    assert proposal["action_id"] == f"ac1:{_PROPOSE}"
    assert proposal["action_type"] is ActionType.FORM_SUBMIT
    assert proposal["target"] == _FORM_URL

    summary = str(proposal["human_summary"])
    assert _FORM_URL in summary
    for field in _FORM_FIELDS:
        assert field["name"] in summary
        assert field["value"] in summary
    assert _FORM_SUBMIT["name"] in summary


# --------------------------------------------------------------------------------- AC2


async def test_ac2_approve_dispatches_exactly_once_with_journaled_args() -> None:
    registry = Registry()
    calls = _register_fake_browser(registry)
    writer = _RecordingWriter()
    engine, _journal = _build_engine(_graph(writer, producer=_FormRequestProducer()), registry)

    parked = await engine.run(
        run_id="ac2", session_id="ac2", pathway_id=_PATHWAY_ID, initial=_trigger()
    )
    assert parked.status is RunStatus.AWAITING_HUMAN

    final = await engine.provide_human_input("ac2", payload={"decision": "approve"})

    assert final.status is RunStatus.COMPLETED
    assert calls == [
        {
            "action": "submit_form",
            "url": _FORM_URL,
            "fields": _FORM_FIELDS,
            "submit": _FORM_SUBMIT,
        }
    ]
    assert len(writer.dispatched) == 1
    assert writer.dispatched[0] == (f"ac2:{_PROPOSE}", _NOW)

    # A second provide_human_input on a completed run is an idempotent no-op — no extra calls.
    again = await engine.provide_human_input("ac2", payload={"decision": "approve"})
    assert again.status is RunStatus.COMPLETED
    assert len(calls) == 1
    assert len(writer.dispatched) == 1


# --------------------------------------------------------------------------------- AC3


async def test_ac3_reject_makes_zero_calls_and_cancels_the_trail_row() -> None:
    registry = Registry()
    calls = _register_fake_browser(registry)
    writer = _RecordingWriter()
    engine, _journal = _build_engine(_graph(writer, producer=_FormRequestProducer()), registry)

    parked = await engine.run(
        run_id="ac3", session_id="ac3", pathway_id=_PATHWAY_ID, initial=_trigger()
    )
    assert parked.status is RunStatus.AWAITING_HUMAN

    final = await engine.provide_human_input("ac3", payload={"decision": "reject"})

    assert final.status is RunStatus.COMPLETED
    assert calls == []  # the fake browser session is never touched
    assert writer.dispatched == []
    assert len(writer.cancelled) == 1
    assert writer.cancelled[0] == (f"ac3:{_PROPOSE}", _NOW)


# ------------------------------------------------------------------------------- AC4(i)


async def test_ac4i_unadmitted_dispatch_of_browser_raises_tier_violation() -> None:
    """A stage WITHOUT ``tool_policy`` (``DEFAULT_TOOL_POLICY``, no external tier) dispatching
    ``browser`` through the gated path is refused with ``TierViolation`` — this is the same
    structural admission FormSubmitStage/DispatchApprovedStage rely on
    ``wombat.safety.tier_policy.bind_external_tier`` for (``tests/safety/test_tier_policy.py``
    already polices the single admitting site generally; this is the focused proof for this
    module's capability name)."""
    registry = Registry()
    calls = _register_fake_browser(registry)

    gate = ToolGate(registry)  # DEFAULT_TOOL_POLICY — no tool_policy bound anywhere

    with pytest.raises(TierViolation):
        await dispatch_one(
            gate,
            registry,
            _BROWSER_CAPABILITY,
            {
                "action": "submit_form",
                "url": _FORM_URL,
                "fields": _FORM_FIELDS,
                "submit": _FORM_SUBMIT,
            },
        )
    assert calls == []


# ------------------------------------------------------------------------------ AC4(ii)


async def test_ac4ii_q114a_approval_never_readmits_external_tier_on_a_tainted_drive() -> None:
    """THE Q-114(a) RULING TEST: the drive is tainted BEFORE ``form_submit`` even parks (a real
    gated dispatch of a tagged ``untrusted-source`` capability in the producer). Approving the
    parked proposal still gets refused — ``ctx.dispatch_approved`` raises ``TierViolation`` from
    inside ``DispatchApprovedStage.run`` (uncaught, per Q-114(a): human approval never re-admits
    the external tier on a tainted drive). Zero capability invocations; ``mark_dispatched`` is
    never called."""
    registry = Registry()
    calls = _register_fake_browser(registry)
    _register_taint_capability(registry)
    writer = _RecordingWriter()
    producer = _TaintingFormRequestProducer()
    engine, _journal = _build_engine(_graph(writer, producer=producer), registry)

    parked = await engine.run(
        run_id="ac4ii", session_id="ac4ii", pathway_id=_PATHWAY_ID, initial=_trigger()
    )
    assert parked.status is RunStatus.AWAITING_HUMAN
    assert calls == []  # taint alone does not touch the browser capability

    with pytest.raises(TierViolation):
        await engine.provide_human_input("ac4ii", payload={"decision": "approve"})

    assert calls == []  # the gated submit_form dispatch never reached invoke
    assert writer.dispatched == []  # mark_dispatched never called
