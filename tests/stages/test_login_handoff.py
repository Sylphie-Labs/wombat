"""TK-136 acceptance criteria — LoginHandoffStage + LoginConfirmStage (Q-114 rulings (f)-(j)):
detect a login page, park AwaitHuman for a human to complete login by hand, and resume on
confirmation.

Harness mirrors ``tests/safety/test_approval_gate.py``/``tests/stages/test_form_submit.py``
exactly: a REAL cog-worx ``Engine`` + ``InMemoryJournal`` driving a REAL ``Registry`` (unused here
— neither stage ever calls ``ctx.dispatch``), a recording fake trail writer (no DSN), and a fake
``PageStateProvider`` standing in for a direct, ungated ``BrowserSession`` read.

  AC1 a fake provider with a login-page snapshot: a 'login required at <domain>' trail row is
      recorded, the run parks AWAITING_HUMAN, and no credential (field VALUE) is ever touched.
  AC2 ``provide_human_input`` decision ``login-complete`` with the provider now password-free:
      Done ``wombat.login_confirmed`` + ``mark_dispatched``. TWIN: the password textbox still
      present -> the run parks AWAITING_HUMAN again (to itself), and a SECOND
      ``login-complete`` answer once the field is finally gone completes it.
  AC4 after the park: run status stays AWAITING_HUMAN, no further step commits, the journal holds
      the AwaitHuman step; a bare ``resume`` (no human input) is a documented no-op — only
      ``provide_human_input`` re-drives.

A detection-negative pass-through test (Done ``wombat.login_check_passed``, zero trail writes) is
also included — the module docstring's other structural guarantee.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from cogworx.capability.registry import Registry
from cogworx.claims.provenance import Artifact, Provenance
from cogworx.loop.graph import StageGraph
from cogworx.loop.pathway import PathwayRegistry
from cogworx.loop.result import AwaitHuman, Done
from cogworx.loop.state import RunStatus
from cogworx.model.registry import ModelRegistry
from cogworx.runtime.engine import Engine
from cogworx.testing.doubles import InMemoryGraphStore, InMemoryJournal, InMemoryLatentStore
from cogworx.testing.fake_model import ReplayModel

from wombat.stages.login_handoff import (
    LOGIN_CHECK_PASSED,
    LOGIN_CONFIRMED,
    LoginConfirmStage,
    LoginConfirmTerminalStage,
    LoginHandoffStage,
)
from wombat.trail.schema import ActionType

_NOW = datetime(2026, 7, 10, 9, 0, tzinfo=UTC)
_PATHWAY_ID = "login-handoff-test"

_PROPOSE = "login_handoff"
_CONFIRM = "login_confirm"

_LOGIN_URL = "https://example.com/login"
_LOGIN_SNAPSHOT: list[Any] = [
    'heading "Sign in" [level=1]',
    'textbox "Username"',
    'textbox "Password"',
    'button "Sign in"',
]
_NO_LOGIN_SNAPSHOT: list[Any] = [
    'heading "Dashboard" [level=1]',
    {"paragraph": "Welcome back"},
]


# --------------------------------------------------------------------------------- shared plumbing


def _trigger() -> Artifact:
    return Artifact(
        kind="wombat.login_handoff_trigger",
        produced_by="test",
        provenance=Provenance(source="system", confidence=1.0, recorded_at=_NOW),
        data={},
    )


def _build_engine(graph: StageGraph) -> tuple[Engine, InMemoryJournal]:
    """A REAL cog-worx Engine, entirely in-memory (mirrors ``test_approval_gate.py``'s
    ``_build_engine``). Neither stage under test ever calls ``ctx.dispatch``, so the Registry is
    empty."""
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
        registry=Registry(),
        clock=lambda: _NOW,
    )
    return engine, journal


class _RecordingWriter:
    """A recording fake trail writer (no DSN) satisfying both ``ProposalWriter`` and
    ``ApprovalTrailWriter`` — mirrors ``test_approval_gate.py``'s ``_RecordingWriter``."""

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


class _FakePageState:
    """A fake ``PageStateProvider`` (an injected async callable) — stands in for a direct,
    ungated ``BrowserSession`` read. ``url``/``snapshot`` are mutable so a test can simulate a
    human completing login (the password textbox disappearing) between calls."""

    def __init__(self, *, url: str, snapshot: Any) -> None:
        self.url = url
        self.snapshot = snapshot
        self.call_count = 0

    async def __call__(self) -> dict[str, Any]:
        self.call_count += 1
        return {"url": self.url, "snapshot": self.snapshot}


def _graph(writer: Any, page_state: _FakePageState) -> StageGraph:
    handoff = LoginHandoffStage(writer=writer, page_state=page_state)
    confirm = LoginConfirmStage(writer=writer, page_state=page_state)
    # LoginConfirmTerminalStage is a never-reached stub required ONLY to satisfy cog-worx's
    # "the graph can end" construction invariant (see login_handoff.py's module docstring) — it
    # is never actually entered by either test below.
    return StageGraph([handoff, confirm, LoginConfirmTerminalStage()], entry=_PROPOSE)


# --------------------------------------------------------------------------------- AC1


async def test_ac1_login_page_parks_awaiting_human_with_trail_row_no_credential_touch() -> None:
    writer = _RecordingWriter()
    page_state = _FakePageState(url=_LOGIN_URL, snapshot=_LOGIN_SNAPSHOT)
    engine, _journal = _build_engine(_graph(writer, page_state))

    final = await engine.run(
        run_id="ac1", session_id="ac1", pathway_id=_PATHWAY_ID, initial=_trigger()
    )

    assert final.status is RunStatus.AWAITING_HUMAN

    assert len(writer.proposals) == 1
    proposal = writer.proposals[0]
    assert proposal["action_id"] == f"ac1:{_PROPOSE}"
    assert proposal["action_type"] is ActionType.LOGIN_HANDOFF
    assert proposal["target"] == "example.com"
    assert proposal["human_summary"] == "login required at example.com"

    # No credential is ever read or stored: the detector only inspects the snapshot's role/name
    # tree, and the recorded proposal carries nothing but the domain — no field value anywhere.
    assert "Username" not in str(proposal)
    assert "Password" not in str(proposal)


# ------------------------------------------------------------ detection-negative pass-through


async def test_detection_negative_passes_through_with_zero_trail_writes() -> None:
    writer = _RecordingWriter()
    page_state = _FakePageState(url="https://example.com/dashboard", snapshot=_NO_LOGIN_SNAPSHOT)
    engine, journal = _build_engine(_graph(writer, page_state))

    final = await engine.run(
        run_id="passthrough", session_id="passthrough", pathway_id=_PATHWAY_ID, initial=_trigger()
    )

    assert final.status is RunStatus.COMPLETED
    assert writer.proposals == []
    assert writer.dispatched == []

    run = await journal.load_run("passthrough")
    assert run is not None
    result = run.steps[-1].result
    assert isinstance(result, Done)
    assert result.output.kind == LOGIN_CHECK_PASSED


# --------------------------------------------------------------------------------- AC2


async def test_ac2_login_complete_with_password_gone_marks_dispatched_and_completes() -> None:
    writer = _RecordingWriter()
    page_state = _FakePageState(url=_LOGIN_URL, snapshot=_LOGIN_SNAPSHOT)
    engine, journal = _build_engine(_graph(writer, page_state))

    parked = await engine.run(
        run_id="ac2", session_id="ac2", pathway_id=_PATHWAY_ID, initial=_trigger()
    )
    assert parked.status is RunStatus.AWAITING_HUMAN

    page_state.snapshot = _NO_LOGIN_SNAPSHOT  # the human completed login

    final = await engine.provide_human_input("ac2", payload={"decision": "login-complete"})

    assert final.status is RunStatus.COMPLETED
    assert writer.dispatched == [(f"ac2:{_PROPOSE}", _NOW)]

    run = await journal.load_run("ac2")
    assert run is not None
    result = run.steps[-1].result
    assert isinstance(result, Done)
    assert result.output.kind == LOGIN_CONFIRMED


async def test_ac2_twin_password_still_present_reparks_then_completes_on_second_confirm() -> None:
    writer = _RecordingWriter()
    page_state = _FakePageState(url=_LOGIN_URL, snapshot=_LOGIN_SNAPSHOT)
    engine, journal = _build_engine(_graph(writer, page_state))

    parked = await engine.run(
        run_id="ac2twin", session_id="ac2twin", pathway_id=_PATHWAY_ID, initial=_trigger()
    )
    assert parked.status is RunStatus.AWAITING_HUMAN

    # The password textbox is STILL present — the human answered too soon.
    reparked = await engine.provide_human_input("ac2twin", payload={"decision": "login-complete"})

    assert reparked.status is RunStatus.AWAITING_HUMAN
    assert writer.dispatched == []

    run = await journal.load_run("ac2twin")
    assert run is not None
    reparked_result = run.steps[-1].result
    assert isinstance(reparked_result, AwaitHuman)
    assert reparked_result.to == _CONFIRM

    # Now the human actually finishes; a SECOND confirm completes it. This exercises the widened
    # (login_handoff OR login_confirm) reverse journal walk — the newest answer lands at the
    # newest park, which by now is login_confirm's OWN step, not login_handoff's.
    page_state.snapshot = _NO_LOGIN_SNAPSHOT
    final = await engine.provide_human_input("ac2twin", payload={"decision": "login-complete"})

    assert final.status is RunStatus.COMPLETED
    assert writer.dispatched == [(f"ac2twin:{_PROPOSE}", _NOW)]


# --------------------------------------------------------------------------------- AC4


async def test_ac4_park_is_stable_and_only_human_input_re_drives() -> None:
    writer = _RecordingWriter()
    page_state = _FakePageState(url=_LOGIN_URL, snapshot=_LOGIN_SNAPSHOT)
    engine, journal = _build_engine(_graph(writer, page_state))

    parked = await engine.run(
        run_id="ac4", session_id="ac4", pathway_id=_PATHWAY_ID, initial=_trigger()
    )
    assert parked.status is RunStatus.AWAITING_HUMAN

    run_before = await journal.load_run("ac4")
    assert run_before is not None
    steps_before = len(run_before.steps)

    # A bare resume (no human input) on an AWAITING_HUMAN run is a documented no-op — no retry
    # path exists that could re-drive it without a human answer.
    resumed = await engine.resume("ac4")
    assert resumed.status is RunStatus.AWAITING_HUMAN

    run_after = await journal.load_run("ac4")
    assert run_after is not None
    assert len(run_after.steps) == steps_before
