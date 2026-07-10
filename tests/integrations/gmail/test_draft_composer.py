"""TK-78 — DraftComposer acceptance criteria (EP-18, Q-92).

AC1/AC2/AC4 use a REAL cogworx ``Registry``/``ToolGate``/``dispatch_one`` IN-PROCESS (mirrors
``tests/safety/test_taint_latch_adversarial.py``'s ``_FakeStageContextForIngest`` pattern) so the
taint-latch ordering claims are proven against the real security machinery, not a mock of it.
AC1's engine-level park half and AC3 use a REAL cog-worx ``Engine`` over in-memory substrate
doubles (mirrors ``tests/safety/test_approval_gate.py``'s ``_build_engine``).
"""

from __future__ import annotations

import asyncio
import base64
import time
from datetime import UTC, datetime
from typing import Any, cast

import pytest
import requests
from cogworx.capability.policy import TierViolation, ToolGate
from cogworx.capability.registry import Registry, function_capability
from cogworx.capability.router import dispatch_one
from cogworx.claims.provenance import Artifact, Provenance
from cogworx.loop.graph import StageGraph
from cogworx.loop.pathway import PathwayRegistry
from cogworx.loop.result import AwaitHuman, StageResult, Transition
from cogworx.loop.stage import StageContext
from cogworx.loop.state import RunStatus
from cogworx.model.base import ModelResponse
from cogworx.model.registry import ModelRegistry
from cogworx.runtime.engine import Engine
from cogworx.testing.doubles import InMemoryGraphStore, InMemoryJournal, InMemoryLatentStore
from cogworx.testing.fake_model import ReplayModel

from tests.support.stage_context_fake import FakeModel
from wombat.gate.models import ItemKind
from wombat.integrations.gmail.draft_composer import (
    DRAFT_CREATE_CAPABILITY,
    DRAFT_PROPOSAL,
    DraftComposer,
    make_drafts_create_capability,
)
from wombat.integrations.gmail.reply_intent import ReplyIntent
from wombat.safety.taint import READ_EMAIL_BODY_CAPABILITY, BodyProvider, register_read_email_body
from wombat.safety.tier_policy import EXTERNAL_DISPATCH_POLICY
from wombat.stages.artifacts import COMPOSE_REQUEST, compose_request_to_artifact_data
from wombat.trail.schema import ActionType

_FIXED_NOW = datetime(2026, 7, 9, 9, 0, tzinfo=UTC)
_PATHWAY_ID = "draft-composer-test"


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
    """A recording fake ``DraftTrailWriter`` — records proposals/refusals and appends to a
    SHARED ``events`` list (with the capability spy below) so tests can assert cross-call
    ORDERING (AC1(b): ``record_proposal`` before ``drafts_create``)."""

    def __init__(self, events: list[str]) -> None:
        self._events = events
        self.proposals: list[dict[str, object]] = []
        self.refusals: list[dict[str, object]] = []

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
    """A fake ``gmail.messages.send`` capability — DraftComposer must NEVER dispatch this
    (CON-5/DEC-19/NG-5, AC1(f)). Registered only to prove zero calls, not because production
    ever registers it."""
    calls: list[tuple[str, str]] = []

    async def _send_email(to: str, body: str) -> str:
        calls.append((to, body))
        return "sent"

    registry.register(function_capability(_send_email, name="gmail.messages.send", tier="external"))
    return calls


def _body_provider_factory(bodies: dict[str, str]) -> BodyProvider:
    async def _provider(message_id: str) -> str:
        return bodies[message_id]

    return _provider


class _FakeDraftContext:
    """A minimal duck-typed StageContext exercising only what ``DraftComposer.run`` touches:
    ``last_output``, ``dispatch`` (via a REAL gate/registry pair, so the dispatch goes through
    cog-worx's real security pipeline), ``model``, and ``run_id`` (mirrors
    ``test_taint_latch_adversarial.py``'s ``_FakeStageContextForIngest``). ``clock`` is included
    for Protocol-shape completeness even though ``DraftComposer`` uses its own ctor-injected
    clock, never ``ctx.clock``."""

    def __init__(
        self,
        gate: ToolGate,
        registry: Registry,
        reply_intent: ReplyIntent,
        model: FakeModel,
        *,
        run_id: str = "run-1",
    ) -> None:
        self._gate = gate
        self._registry = registry
        self._reply_intent = reply_intent
        self.model = model
        self.run_id = run_id

    async def last_output(self, stage_name: str) -> Artifact | None:
        if stage_name != "compose_dispatch":
            return None
        return Artifact(
            kind=COMPOSE_REQUEST,
            produced_by="compose_dispatch",
            provenance=Provenance(source="system", confidence=1.0, recorded_at=_FIXED_NOW),
            data=compose_request_to_artifact_data(
                "item-1", ItemKind.DRAFT, self._reply_intent.to_payload()
            ),
        )

    async def dispatch(self, capability: str, args: dict[str, object]) -> Any:
        return await dispatch_one(self._gate, self._registry, capability, dict(args))

    @property
    def clock(self) -> Any:
        return lambda: _FIXED_NOW


# ------------------------------------------------------------------------ engine-level plumbing


def _trigger() -> Artifact:
    return Artifact(
        kind="wombat.draft_composer_trigger",
        produced_by="test",
        provenance=Provenance(source="system", confidence=1.0, recorded_at=_FIXED_NOW),
        data={},
    )


def _build_engine(graph: StageGraph, registry: Registry) -> tuple[Engine, InMemoryJournal]:
    """A REAL cog-worx Engine over a REAL Registry/ToolGate, entirely in-memory (mirrors
    ``test_approval_gate.py``'s ``_build_engine``)."""
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
    """The entry stub (TK-97 terminal-stub precedent's counterpart): emits the scored DRAFT
    ``wombat.compose_request`` artifact carrying a ``ReplyIntent`` payload — the exact wire
    ``ComposeStage`` (TK-8) also reads, differentiated here by ``item_kind=DRAFT``."""

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


class _RaisingDraftDispatchStub:
    """The TK-97 terminal-stub precedent: a stage that raises if it EVER executes. Named
    ``draft_dispatch`` (TK-78's frozen ``transitions`` target, TK-79's real stage). Proves the
    park holds — it must never run while the drive sits AWAITING_HUMAN."""

    name = "draft_dispatch"
    transitions: tuple[str, ...] = ()

    async def run(self, ctx: StageContext) -> StageResult:
        raise AssertionError("unreachable: draft_dispatch must never execute while parked")


def _stub_graph(writer: _RecordingWriter, reply_intent: ReplyIntent) -> StageGraph:
    entry = _ComposeDispatchStub(reply_intent)
    composer = DraftComposer(writer=writer, clock=lambda: _FIXED_NOW)
    terminal = _RaisingDraftDispatchStub()
    return StageGraph([entry, composer, terminal], entry="compose_dispatch")


# --------------------------------------------------------------------------------- AC1 (gate-level)


async def test_ac1_journal_before_dispatch_then_taint_latches_and_send_never_called() -> None:
    registry = Registry()
    events: list[str] = []
    calls = _register_draft_capability(registry, events)
    send_calls = _register_send_spy(registry)
    writer = _RecordingWriter(events)
    gate = ToolGate(registry, policy=EXTERNAL_DISPATCH_POLICY)

    reply_intent = _reply_intent()
    model = FakeModel(
        response=ModelResponse(
            text="Thanks for the update, I'll follow up.", model_id="m", finish_reason="stop"
        )
    )
    ctx = _FakeDraftContext(gate, registry, reply_intent, model)
    stage = DraftComposer(writer=writer, clock=lambda: _FIXED_NOW)

    # (a) BEFORE this stage dispatches, the drive is untainted (the ISS-3 fresh-drive baseline).
    assert gate.taint.tainted is False

    result = await stage.run(ctx)  # type: ignore[arg-type]

    # (d) the stage returns AwaitHuman(to='draft_dispatch').
    assert isinstance(result, AwaitHuman)
    assert result.to == "draft_dispatch"
    assert result.output is not None
    assert result.output.kind == DRAFT_PROPOSAL
    assert result.output.data["degraded"] is False

    # (b) an ordered event log proves record_proposal fires BEFORE the drafts.create invoke.
    assert events == ["record_proposal", "drafts_create"]

    # (c) drafts.create invoked exactly once with the mouth-phrased body.
    assert calls == [
        ("jane@example.com", "Re: Q3 budget", "Thanks for the update, I'll follow up.")
    ]

    # (e) AFTER: taint latched (the pre-invoke latch, accepted harmless-by-order) AND a further
    # external dispatch on the SAME gate raises TierViolation (taint_drops_external probe).
    assert gate.taint.tainted is True
    with pytest.raises(TierViolation):
        await dispatch_one(
            gate, registry, DRAFT_CREATE_CAPABILITY, {"to": "x", "subject": "y", "body": "z"}
        )

    # (f) gmail.messages.send is never invoked.
    assert send_calls == []


def test_gmail_messages_send_is_never_defined_in_this_module() -> None:
    """Structural regression guard (AC1(f)): the ONE capability-name constant this module
    exports is ``gmail.drafts.create`` — mirrors ``test_taint_latch_adversarial.py``'s AC2
    module-scan style (scoped to ``_CAPABILITY``-suffixed constants, not the prose docstring,
    which legitimately discusses the never-send guarantee)."""
    import wombat.integrations.gmail.draft_composer as draft_composer_module

    capability_name_constants = {
        name: value
        for name, value in vars(draft_composer_module).items()
        if name in draft_composer_module.__all__ and name.endswith("_CAPABILITY")
    }
    assert capability_name_constants == {"DRAFT_CREATE_CAPABILITY": "gmail.drafts.create"}


async def test_ac1_engine_level_park_holds_and_raising_stub_never_executes() -> None:
    registry = Registry()
    events: list[str] = []
    calls = _register_draft_capability(registry, events)
    send_calls = _register_send_spy(registry)
    writer = _RecordingWriter(events)
    reply_intent = _reply_intent()
    engine, _journal = _build_engine(_stub_graph(writer, reply_intent), registry)

    final = await engine.run(
        run_id="ac1e", session_id="ac1e", pathway_id=_PATHWAY_ID, initial=_trigger()
    )

    assert final.status is RunStatus.AWAITING_HUMAN
    assert calls == [("jane@example.com", "Re: Q3 budget", "phrased reply")]
    assert send_calls == []  # the raising terminal stub never ran, and neither did any send


# --------------------------------------------------------------------------------- AC2 (negative)


async def test_ac2_pretainted_drive_refuses_drafts_create_and_records_one_refusal() -> None:
    registry = Registry()
    events: list[str] = []
    calls = _register_draft_capability(registry, events)
    provider = _body_provider_factory({"msg-tainter": "hi team, quick update"})
    register_read_email_body(registry, provider)
    writer = _RecordingWriter(events)
    gate = ToolGate(registry, policy=EXTERNAL_DISPATCH_POLICY)

    # Taint the gate FIRST via the tagged read (the modeled sanitization-bypass scenario).
    await dispatch_one(gate, registry, READ_EMAIL_BODY_CAPABILITY, {"message_id": "msg-tainter"})
    assert gate.taint.tainted is True

    reply_intent = _reply_intent()
    model = FakeModel(
        response=ModelResponse(text="reply body", model_id="m", finish_reason="stop")
    )
    ctx = _FakeDraftContext(gate, registry, reply_intent, model)
    stage = DraftComposer(writer=writer, clock=lambda: _FIXED_NOW)

    with pytest.raises(TierViolation):
        await stage.run(ctx)  # type: ignore[arg-type]

    assert calls == []  # ZERO drafts.create invocations
    assert len(writer.refusals) == 1
    assert writer.refusals[0]["target"] == DRAFT_CREATE_CAPABILITY


# --------------------------------------------------------------------------------- AC3 (park holds)


async def test_ac3_redrive_without_human_input_leaves_run_parked_with_no_further_calls() -> None:
    registry = Registry()
    events: list[str] = []
    calls = _register_draft_capability(registry, events)
    send_calls = _register_send_spy(registry)
    writer = _RecordingWriter(events)
    reply_intent = _reply_intent()
    engine, _journal = _build_engine(_stub_graph(writer, reply_intent), registry)

    parked = await engine.run(
        run_id="ac3", session_id="ac3", pathway_id=_PATHWAY_ID, initial=_trigger()
    )
    assert parked.status is RunStatus.AWAITING_HUMAN
    assert len(calls) == 1

    resumed = await engine.resume("ac3")

    assert resumed.status is RunStatus.AWAITING_HUMAN
    assert len(calls) == 1  # UNCHANGED — zero further Gmail calls across the re-drive
    assert send_calls == []


# --------------------------------------------------------------------------------- AC4 (mouth down)


async def test_ac4_model_error_degrades_to_template_but_still_creates_draft_once() -> None:
    registry = Registry()
    events: list[str] = []
    calls = _register_draft_capability(registry, events)
    writer = _RecordingWriter(events)
    gate = ToolGate(registry, policy=EXTERNAL_DISPATCH_POLICY)
    reply_intent = _reply_intent()
    model = FakeModel(raises=ConnectionError("503 Service Unavailable"))
    ctx = _FakeDraftContext(gate, registry, reply_intent, model)
    stage = DraftComposer(writer=writer, clock=lambda: _FIXED_NOW)

    result = await stage.run(ctx)  # type: ignore[arg-type]

    assert isinstance(result, AwaitHuman)
    assert result.output is not None
    assert result.output.data["degraded"] is True
    assert len(calls) == 1
    template_body = calls[0][2]
    assert "Q3 budget" in template_body  # built from ReplyIntent fields, not the model's text
    assert result.output.data["body"] == template_body


async def test_ac4_model_timeout_degrades_to_template_within_bound() -> None:
    registry = Registry()
    events: list[str] = []
    calls = _register_draft_capability(registry, events)
    writer = _RecordingWriter(events)
    gate = ToolGate(registry, policy=EXTERNAL_DISPATCH_POLICY)
    reply_intent = _reply_intent()
    model = FakeModel(sleep_seconds=5.0)  # far longer than the tiny timeout below
    ctx = _FakeDraftContext(gate, registry, reply_intent, model)
    stage = DraftComposer(writer=writer, clock=lambda: _FIXED_NOW, timeout_seconds=0.05)

    start = time.monotonic()
    result = await stage.run(ctx)  # type: ignore[arg-type]
    elapsed = time.monotonic() - start

    assert elapsed < 1.0  # bounded by wait_for's 0.05s timeout, not the model's 5s sleep
    assert isinstance(result, AwaitHuman)
    assert result.output is not None
    assert result.output.data["degraded"] is True
    assert len(calls) == 1


async def test_ac4_cancelled_error_is_re_raised_not_swallowed() -> None:
    registry = Registry()
    events: list[str] = []
    calls = _register_draft_capability(registry, events)
    writer = _RecordingWriter(events)
    gate = ToolGate(registry, policy=EXTERNAL_DISPATCH_POLICY)
    reply_intent = _reply_intent()
    model = FakeModel(raises=asyncio.CancelledError())
    ctx = _FakeDraftContext(gate, registry, reply_intent, model)
    stage = DraftComposer(writer=writer, clock=lambda: _FIXED_NOW)

    with pytest.raises(asyncio.CancelledError):
        await stage.run(ctx)  # type: ignore[arg-type]

    assert events == []  # never reached record_proposal or drafts.create
    assert calls == []


# ---------------------------------------------------------------- make_drafts_create_capability


class _FakeDraftResponse:
    def __init__(self, json_body: dict[str, object]) -> None:
        self._json_body = json_body

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return self._json_body


class _FakeDraftSession:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], float]] = []

    def post(self, url: str, *, json: dict[str, Any], timeout: float) -> requests.Response:
        self.calls.append((url, json, timeout))
        # _FakeDraftResponse only mimics the Response surface the capability touches
        # (raise_for_status/json); the cast satisfies _GmailDraftSession's Protocol return type
        # without inheriting requests.Response's much larger surface (mirrors test_poller.py).
        return cast("requests.Response", _FakeDraftResponse({"id": "draft-abc"}))


async def test_make_drafts_create_capability_posts_encoded_message_and_returns_draft_id() -> None:
    session = _FakeDraftSession()
    capability = make_drafts_create_capability(session)

    assert capability.name == DRAFT_CREATE_CAPABILITY
    assert capability.tier == "external"

    result = await capability.invoke(
        {"to": "jane@example.com", "subject": "Re: hi", "body": "body text"}
    )

    assert result == "draft-abc"
    assert len(session.calls) == 1
    url, payload, timeout = session.calls[0]
    assert url == "https://gmail.googleapis.com/gmail/v1/users/me/drafts"
    assert timeout == 30.0
    raw = payload["message"]["raw"]
    padded = raw + "=" * (-len(raw) % 4)
    decoded = base64.urlsafe_b64decode(padded).decode("utf-8")
    assert "jane@example.com" in decoded
    assert "body text" in decoded


# ------------------------------------------------------------------------------------ TK-194


async def test_tk194_default_assistant_name_renders_in_system_instruction() -> None:
    registry = Registry()
    events: list[str] = []
    _register_draft_capability(registry, events)
    writer = _RecordingWriter(events)
    gate = ToolGate(registry, policy=EXTERNAL_DISPATCH_POLICY)
    reply_intent = _reply_intent()
    model = FakeModel(
        response=ModelResponse(text="Thanks!", model_id="deepseek-chat", finish_reason="stop")
    )
    ctx = _FakeDraftContext(gate, registry, reply_intent, model)
    stage = DraftComposer(writer=writer, clock=lambda: _FIXED_NOW)

    await stage.run(ctx)  # type: ignore[arg-type]

    system_msg, _user_msg = model.calls[0]
    assert system_msg.content.startswith("You are Steward, a quiet steward")


async def test_tk194_configured_assistant_name_renders_in_system_instruction_only() -> None:
    registry = Registry()
    events: list[str] = []
    _register_draft_capability(registry, events)
    writer = _RecordingWriter(events)
    gate = ToolGate(registry, policy=EXTERNAL_DISPATCH_POLICY)
    reply_intent = _reply_intent()
    model = FakeModel(
        response=ModelResponse(text="Thanks!", model_id="deepseek-chat", finish_reason="stop")
    )
    ctx = _FakeDraftContext(gate, registry, reply_intent, model)
    stage = DraftComposer(writer=writer, clock=lambda: _FIXED_NOW, assistant_name="Marvin")

    await stage.run(ctx)  # type: ignore[arg-type]

    system_msg, user_msg = model.calls[0]
    assert "Marvin" in system_msg.content
    # Structural non-goal: the name is display/persona only -- name-free everywhere else.
    assert "Marvin" not in user_msg.content


# ---------------------------------------------------------------------- structural spot-checks


def test_draft_composer_name_and_transitions_are_frozen_for_tk79() -> None:
    writer = _RecordingWriter([])
    stage = DraftComposer(writer=writer, clock=lambda: _FIXED_NOW)

    assert stage.name == "draft_composer"
    assert stage.transitions == ("draft_dispatch",)


def test_draft_composer_admits_external_tier_without_disabling_taint_drops_external() -> None:
    """DEC-26 binding constraint spot-check (mirrors test_approval_gate.py's equivalent for
    DispatchApprovedStage): the tier admission DraftComposer binds via bind_external_tier must
    keep taint_drops_external True."""
    writer = _RecordingWriter([])
    stage = DraftComposer(writer=writer, clock=lambda: _FIXED_NOW)

    policy = stage.tool_policy
    assert policy.taint_drops_external is True
    assert "external" in policy.allowed_tiers
