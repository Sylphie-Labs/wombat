"""TK-53 — runtime process boot acceptance criteria (EP-1, Q-71).

Everything here is IN-MEMORY / fast: no Postgres, no real network. The Sweeper is driven via
``tick()`` directly with an injected clock — ``run_forever`` is never called unbounded, per the
ticket's own testing ruling. DSN-gated, real-Postgres assembly assertions and the full
standing-loop cycle (AC5) live in ``tests/integration/test_serve_boot.py``.

  AC1 an Engine parked on a durable Wait whose wake_at is in the past -> the Sweeper wakes it and
      it resumes (proves armed timers fire only because the Sweeper runs).
  AC2 no pathway parked + empty journal -> the Sweeper idles without inventing work (DEC-8) and
      never calls the injected ``fire`` (so it can never spend a model token).
  AC3 the boot path assembles via ``bootstrap.build_engine``/``build_compose_stage`` (not
      hand-rolled) -> the composition carries a non-None spend ledger and a non-default
      BudgetPolicy (ceilings not None).
  AC4 ``assemble_runtime()`` REGISTERS the drain pathway (``pathways.get`` resolves it) and wires
      the TK-29 PG ``PendingJournal`` (isinstance) into the gate; a unit shutdown test asserts
      ``SourceRegistry.stop()`` is awaited on cancellation.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from importlib.abc import MetaPathFinder
from importlib.machinery import ModuleSpec
from pathlib import Path
from types import ModuleType
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from cogworx.claims.provenance import Artifact, Provenance
from cogworx.knowledge.scopes import ScopeRegistry
from cogworx.loop.graph import StageGraph
from cogworx.loop.pathway import PathwayError, PathwayRegistry
from cogworx.loop.result import Done, StageResult, Wait
from cogworx.loop.stage import StageContext
from cogworx.loop.state import RunStatus
from cogworx.model.base import ModelResponse
from cogworx.model.registry import ModelRegistry
from cogworx.runtime.engine import Engine
from cogworx.runtime.sweeper import Sweeper
from cogworx.testing.doubles import (
    InMemoryEntityKG,
    InMemoryGraphStore,
    InMemoryJournal,
    InMemoryLatentStore,
)
from pydantic import SecretStr

import wombat.integrations.gmail.session as gmail_session_module
from tests.support.stage_context_fake import FakeModel, StageContextFake
from wombat import bootstrap, runtime
from wombat.behavior.event_log import BehaviorEventLog
from wombat.bootstrap import RuntimeBundle
from wombat.compose.templates import TemplateComposer
from wombat.config import ConfigurationError, WombatConfig
from wombat.domain.daily_ledger import DailyLedger
from wombat.gate.models import ItemKind
from wombat.gate.pending_journal_pg import PgPendingJournal
from wombat.params import load_operating_params
from wombat.pathways.drain_pathway import build_drain_pathway
from wombat.queue import EnqueueResult, QueueItem, WombatQueue
from wombat.sinks.speak import SpeakSink
from wombat.sources.presence import PresenceSnapshot, PresenceState
from wombat.sources.registry import SourceRegistry
from wombat.stages.compose import ComposeStage
from wombat.stages.compose_dispatch_router import ComposeDispatchRouter
from wombat.stages.drain_queue import DrainQueueStage
from wombat.stages.gate_stage import GateStage, make_stub_evaluator
from wombat.stages.review_or_speak import ReviewOrSpeakStage
from wombat.trail.writer import ActionTrailWriter
from wombat.user_model.observation_writer import ObservationWriter

_PATHWAY_ID = "wombat.drain"
_URGENCY_THRESHOLD = 0.5
_STALENESS_CEILING_S = 300.0
_CONFIDENCE_FLOOR = 0.5

# A fake Postgres DSN — every adapter TK-53 wires (WombatQueue/DailyLedger/PgPendingJournal) is
# lazy (no connection at construction), so these unit tests never touch a real Postgres.
_FAKE_DSN = "postgresql://fake-host/fake-db"


def _config() -> WombatConfig:
    return WombatConfig(deepseek_api_key="sk-test", deepseek_base_url="https://api.deepseek.com")


@pytest.fixture()
def _no_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """TK-202 (Q-103): chdir off the repo root so pydantic-settings' ``env_file=".env"``
    resolution (relative to CWD) can never pick up a populated operator .env underneath a test
    that constructs ``WombatConfig``/``_config()`` without overriding an optional field —
    mirrors TK-186's ``monkeypatch.chdir(tmp_path)`` precedent (``tests/unit/test_bootstrap.py``).
    Opt-in only (not autouse) — requested by name from the tests that need it."""
    monkeypatch.chdir(tmp_path)


class _BlockedFinder(MetaPathFinder):
    """A meta-path finder that fails the import of one named module (and its submodules)."""

    def __init__(self, blocked: str) -> None:
        self._blocked = blocked

    def find_spec(
        self, fullname: str, path: Sequence[str] | None, target: ModuleType | None = None
    ) -> ModuleSpec | None:
        if fullname == self._blocked or fullname.startswith(f"{self._blocked}."):
            raise ModuleNotFoundError(f"No module named {fullname!r} (simulated absence, TK-202)")
        return None


def _simulate_absent(monkeypatch: pytest.MonkeyPatch, module_name: str) -> None:
    """Simulate ``module_name`` being genuinely not installed, regardless of whether it actually
    is on this machine (TK-202/Q-103): evict any cached import AND install a meta-path finder
    ahead of the real one so any subsequent import raises ``ModuleNotFoundError``."""
    monkeypatch.delitem(sys.modules, module_name, raising=False)
    monkeypatch.setattr(sys, "meta_path", [_BlockedFinder(module_name), *sys.meta_path])


@pytest.fixture(autouse=True)
def _reset_singleton() -> Iterator[None]:
    """``build_engine`` is a process singleton (TK-1 AC3) — reset around every test so
    ``assemble_runtime``'s freshly-built substrate/pathways are what the returned Engine
    actually uses, mirroring ``tests/unit/test_bootstrap.py``'s own fixture."""
    bootstrap.reset_engine()
    yield
    bootstrap.reset_engine()


@dataclass
class _FakeQueue:
    """A minimal in-memory stand-in for ``WombatQueue``: one queued batch per ``drain()`` call.

    Satisfies both ``DrainQueueStage``'s ``_DrainableQueue`` and ``ReviewOrSpeakStage``'s
    ``_AckableQueue`` structural Protocols.
    """

    batches: list[list[QueueItem]]
    acked: list[int] = field(default_factory=list)

    def drain(self, limit: int | None = None) -> list[QueueItem]:
        return self.batches.pop(0) if self.batches else []

    def ack(self, item_id: int) -> None:
        self.acked.append(item_id)


def _build_in_memory_stack(
    queue: _FakeQueue, *, model_factory: object
) -> tuple[Engine, InMemoryJournal]:
    """Assemble a REAL cog-worx Engine over the full drain pathway (stub gate — AC1/AC2 are
    about the Sweeper waking a parked run, not the production gate's scoring), entirely
    in-memory. Mirrors ``tests/integration/test_drain_pathway_e2e.py``'s own construction."""
    drain_queue_stage = DrainQueueStage(queue, batch_size=1, poll_interval_seconds=5.0)
    gate_stage = GateStage(
        evaluate=make_stub_evaluator(
            urgency_threshold=_URGENCY_THRESHOLD,
            staleness_ceiling_s=_STALENESS_CEILING_S,
            confidence_floor=_CONFIDENCE_FLOOR,
        ),
        presence_provider=lambda: PresenceSnapshot(
            state=PresenceState.ACTIVE,
            confidence=1.0,
            idle_ms=0,
            taken_at=datetime.now(UTC).timestamp(),
        ),
    )
    review_or_speak_stage = ReviewOrSpeakStage(queue=queue)
    compose_dispatch_router = ComposeDispatchRouter(composer_by_kind={ItemKind.GENERIC: "compose"})
    compose_stage = ComposeStage(config=_config(), template_composer=TemplateComposer())
    # TK-164 (Q-96): compose now transitions onward to "speak" — voice-off (no adapter) here,
    # this module isn't testing voice, only that the Sweeper/pathway wiring reaches the terminal.
    speak_stage = SpeakSink(voice_enabled=False, adapter=None)

    graph = build_drain_pathway(
        drain_queue_stage,
        gate_stage,
        review_or_speak_stage,
        compose_dispatch_router,
        compose_stage,
        speak_stage,
    )
    journal = InMemoryJournal()
    pathways = PathwayRegistry()
    pathways.register(_PATHWAY_ID, graph)

    models = ModelRegistry()
    models.register_factory("deepseek", model_factory)  # type: ignore[arg-type]

    engine = Engine(
        models=models,
        journal=journal,
        graph_store=InMemoryGraphStore(),
        latent=InMemoryLatentStore(),
        pathways=pathways,
        model_profile="deepseek",
        clock=lambda: datetime.now(UTC),
    )
    return engine, journal


def _initial_artifact() -> Artifact:
    return Artifact(
        kind="drain-tick",
        produced_by="test",
        provenance=Provenance(source="system", confidence=1.0, recorded_at=datetime.now(UTC)),
        data={},
    )


# --- AC1: the Sweeper wakes a parked pathway and it resumes -----------------------------------


async def test_ac1_sweeper_wakes_parked_pathway_and_it_resumes() -> None:
    # A genuine SUCCESS model: proves the resumed drive really reaches compose and completes,
    # not merely that the timer flipped a status.
    success_model = lambda guard: FakeModel(  # noqa: E731
        response=ModelResponse(text="You have a new alert.", model_id="fake", finish_reason="stop")
    )
    queue = _FakeQueue(batches=[[]])  # first drive sees an empty queue -> self-parks WAITING
    engine, journal = _build_in_memory_stack(queue, model_factory=success_model)

    run_id = "run-ac1"
    parked = await engine.run(
        run_id=run_id, session_id=run_id, pathway_id=_PATHWAY_ID, initial=_initial_artifact()
    )
    assert parked.status is RunStatus.WAITING
    assert len(parked.steps) == 1  # the fresh drain_queue Wait, nothing else ran yet

    # Now that the run is parked, an item arrives — proving the NEXT drive (the resume) is a
    # real re-execution of the stage, not merely a timer flip.
    queue.batches.append(
        [
            QueueItem(
                idempotency_key="ac1-item",
                payload={"item_kind": "generic", "stub_urgency": "high", "subject": "hi"},
                item_id=1,
            )
        ]
    )

    past_wake = datetime.now(UTC) + timedelta(hours=1)  # comfortably past the 5s poll interval
    sweeper = Sweeper(journal=journal, fire=engine.fire_timer, clock=lambda: past_wake)
    fired = await sweeper.tick(past_wake, lease_ttl=timedelta(seconds=60))

    assert fired == 1  # exactly the one due timer, leased and fired
    resumed = await journal.load_run(run_id)
    assert resumed is not None
    # The resume re-ran drain_queue (drained the new item) and drove it all the way through the
    # REAL pathway to a genuine mouth call — proving the pathway advanced because the Sweeper
    # fired it, not a no-op.
    assert resumed.status is RunStatus.COMPLETED
    assert queue.acked == [1]
    compose_steps = [s for s in resumed.steps if s.stage_name == "compose"]
    assert len(compose_steps) == 1


# --- AC2: the Sweeper idles quietly when nothing is due (DEC-8) -------------------------------


async def test_ac2_sweeper_idles_without_inventing_work() -> None:
    journal = InMemoryJournal()  # no run, no timer ever armed
    fire_calls: list[str] = []

    async def fire(run_id: str) -> None:
        fire_calls.append(run_id)  # pragma: no cover - must never be reached
        raise AssertionError("fire must never be called when no timer is due")

    sweeper = Sweeper(journal=journal, fire=fire, clock=lambda: datetime.now(UTC))
    fired = await sweeper.tick(datetime.now(UTC), lease_ttl=timedelta(seconds=60))

    assert fired == 0
    assert fire_calls == []  # no invented work, no possibility of a model call


# --- AC3: the boot path assembles via build_engine/build_compose_stage, not hand-rolled --------


def test_ac3_build_engine_carries_a_non_default_real_budget_policy() -> None:
    op = load_operating_params()
    engine = bootstrap.build_engine(config=_config(), params=op)

    assert engine._budget_policy.max_usd_per_drive is not None
    assert engine._budget_policy.max_calls_per_drive is not None
    assert engine._budget_policy.max_usd_per_drive == op.mouth_max_usd_per_drive
    assert engine._budget_policy.max_calls_per_drive == op.mouth_max_calls_per_drive


def test_ac3_build_compose_stage_carries_a_non_none_spend_ledger() -> None:
    op = load_operating_params()
    compose_stage = bootstrap.build_compose_stage(config=_config(), dsn=_FAKE_DSN, params=op)

    assert compose_stage._spend_ledger is not None


# --- TK-100: build_brief_compose_stage mirrors build_compose_stage's budget-live wiring ---------


def test_build_brief_compose_stage_carries_a_non_none_spend_ledger_and_same_ceiling() -> None:
    op = load_operating_params()
    brief_compose_stage = bootstrap.build_brief_compose_stage(
        config=_config(), dsn=_FAKE_DSN, params=op
    )

    assert brief_compose_stage._spend_ledger is not None
    assert brief_compose_stage._daily_token_ceiling == op.mouth_daily_token_ceiling


# --- TK-101: build_brief_deliver_stage wiring ----------------------------------------------------


def _config_with_brief_path(path: str) -> WombatConfig:
    return WombatConfig(
        deepseek_api_key="sk-test",
        deepseek_base_url="https://api.deepseek.com",
        wombat_brief_path=path,
    )


def test_build_brief_deliver_stage_with_configured_path_returns_a_stage(tmp_path: Path) -> None:
    sink = tmp_path / "brief.txt"
    config = _config_with_brief_path(str(sink))

    stage = bootstrap.build_brief_deliver_stage(config=config)

    assert stage.name == "brief_deliver"
    assert stage.transitions == ()


def test_build_brief_deliver_stage_blank_path_raises_configuration_error() -> None:
    config = _config_with_brief_path("")

    with pytest.raises(ConfigurationError):
        bootstrap.build_brief_deliver_stage(config=config)


def test_build_brief_deliver_stage_none_path_raises_configuration_error(
    _no_env_file: None,
) -> None:
    config = _config()  # wombat_brief_path defaults to None

    with pytest.raises(ConfigurationError):
        bootstrap.build_brief_deliver_stage(config=config)


# --- TK-164: build_speak_sink / make_speak_callable wiring (Q-96) -------------------------------


def _config_voice_enabled() -> WombatConfig:
    return WombatConfig(
        deepseek_api_key="sk-test",
        deepseek_base_url="https://api.deepseek.com",
        wombat_voice_enabled=True,
    )


def test_build_speak_sink_voice_disabled_by_default_carries_no_adapter(
    _no_env_file: None,
) -> None:
    stage = bootstrap.build_speak_sink(_config())

    assert stage.name == "speak"
    assert stage.transitions == ()
    assert stage._voice_enabled is False
    assert stage._adapter is None


def test_build_speak_sink_voice_enabled_but_pyttsx3_absent_degrades_to_no_adapter(
    _no_env_file: None,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Lesion proof (AC4): pyttsx3 rides the optional 'voice' extra, simulated absent here
    (TK-202/Q-103 — a dev/operator checkout MAY have it installed anyway) — construction must
    not raise, only loud-skip to adapter=None. ``_no_env_file`` (TK-193) keeps this test's
    provider selection at the 'local' default regardless of the operator's own populated .env
    (which may configure a real cloud provider, e.g. Jim's Fish voice)."""
    _simulate_absent(monkeypatch, "pyttsx3")
    with caplog.at_level(logging.WARNING):
        stage = bootstrap.build_speak_sink(_config_voice_enabled())

    assert stage._voice_enabled is True
    assert stage._adapter is None
    assert "voice" in caplog.text.lower()


def test_make_speak_callable_returns_none_when_voice_disabled(_no_env_file: None) -> None:
    assert bootstrap.make_speak_callable(_config()) is None


def test_make_speak_callable_returns_none_when_pyttsx3_absent_even_if_voice_enabled(
    _no_env_file: None,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``_no_env_file`` (TK-193) keeps this test's provider selection at the 'local' default
    regardless of the operator's own populated .env."""
    _simulate_absent(monkeypatch, "pyttsx3")
    with caplog.at_level(logging.WARNING):
        speak = bootstrap.make_speak_callable(_config_voice_enabled())

    assert speak is None


# --- AC4: assemble_runtime registers the pathway + wires the real PG PendingJournal ------------


def test_ac4_assemble_runtime_registers_drain_pathway_and_wires_pg_pending_journal() -> None:
    op = load_operating_params()
    bundle = bootstrap.assemble_runtime(
        config=_config(), dsn=_FAKE_DSN, params=op, replay_pending=False
    )

    # pathways.get resolves the drain pathway id (raises PathwayError if not registered).
    graph = bundle.pathways.get(bundle.drain_pathway_id)
    assert graph is not None
    assert bundle.drain_pathway_id == "wombat.drain"
    # TK-164 (Q-96): the drain graph now carries the "speak" terminal, reachable from "compose".
    assert "speak" in graph.names()
    assert graph.is_terminal("speak")

    # The pending journal wired into the gate IS the TK-29 PG adapter (Q-70/RISK-5).
    assert isinstance(bundle.pending_journal, PgPendingJournal)

    # The engine/compose stage this bundle carries are the REAL budgeted composition, not a
    # hand-rolled one (AC3, reinforced at the assemble_runtime level).
    assert bundle.engine._budget_policy.max_usd_per_drive is not None
    assert bundle.compose_stage._spend_ledger is not None


# --- TK-96: assemble_runtime's CONDITIONAL wombat.brief registration ---------------------------


def test_assemble_runtime_with_brief_path_registers_wombat_brief(tmp_path: Path) -> None:
    op = load_operating_params()
    config = _config_with_brief_path(str(tmp_path / "brief.txt"))

    bundle = bootstrap.assemble_runtime(
        config=config, dsn=_FAKE_DSN, params=op, replay_pending=False
    )

    assert bundle.brief_pathway_id == "wombat.brief"
    # pathways.get resolves the brief pathway id (raises PathwayError if not registered).
    graph = bundle.pathways.get(bundle.brief_pathway_id)
    assert graph is not None
    assert graph.entry == "brief_gather"


def test_assemble_runtime_blank_brief_path_skips_registration_and_warns(
    _no_env_file: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    op = load_operating_params()
    config = _config()  # wombat_brief_path defaults to None

    with caplog.at_level(logging.WARNING):
        bundle = bootstrap.assemble_runtime(
            config=config, dsn=_FAKE_DSN, params=op, replay_pending=False
        )

    assert bundle.brief_pathway_id is None
    assert "WOMBAT_BRIEF_PATH" in caplog.text
    with pytest.raises(PathwayError):
        bundle.pathways.get("wombat.brief")


# --- TK-97: assemble_runtime's CONDITIONAL wombat.brief_schedule registration -------------------


def test_build_brief_schedule_pathway_constructs_without_stage_graph_error() -> None:
    """Locks in the Q-80-as-amended fix: a lone self-only-edge timer stage would trip cog-worx's
    "the graph can end" invariant, so the builder pairs it with a never-reached terminal stub."""
    from datetime import time

    from wombat.pathways.brief_pathway import build_brief_schedule_pathway
    from wombat.stages.brief_timer_stage import BriefTimerStage

    async def _never_fires(now: object) -> object:  # pragma: no cover - never called here
        raise AssertionError("construction must not fire the brief")

    timer = BriefTimerStage(
        fire_brief=_never_fires,  # type: ignore[arg-type]
        ran_today=lambda: False,
        mark_ran=lambda: 1,
        tz=ZoneInfo("UTC"),
        brief_time=time(7, 0),
    )

    graph = build_brief_schedule_pathway(timer)  # must NOT raise StageGraphError

    assert graph.entry == "brief_timer"
    assert set(graph.names()) == {"brief_timer", "brief_timer_terminal"}
    assert graph.is_terminal("brief_timer_terminal")
    assert not graph.is_terminal("brief_timer")  # the timer self-parks, never terminal


async def test_brief_timer_terminal_stub_raises_if_ever_entered() -> None:
    from wombat.pathways.brief_pathway import BriefTimerTerminalStage

    ctx = StageContextFake(now_fn=lambda: datetime.now(UTC))
    with pytest.raises(RuntimeError):
        await BriefTimerTerminalStage().run(ctx)


def test_assemble_runtime_with_brief_path_registers_schedule(tmp_path: Path) -> None:
    op = load_operating_params()
    config = _config_with_brief_path(str(tmp_path / "brief.txt"))

    bundle = bootstrap.assemble_runtime(
        config=config, dsn=_FAKE_DSN, params=op, replay_pending=False
    )

    assert bundle.brief_schedule_pathway_id == "wombat.brief_schedule"
    graph = bundle.pathways.get(bundle.brief_schedule_pathway_id)
    assert graph is not None
    assert graph.entry == "brief_timer"


def test_assemble_runtime_blank_brief_path_skips_schedule(
    _no_env_file: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    op = load_operating_params()
    config = _config()  # wombat_brief_path defaults to None

    with caplog.at_level(logging.WARNING):
        bundle = bootstrap.assemble_runtime(
            config=config, dsn=_FAKE_DSN, params=op, replay_pending=False
        )

    # BOTH brief and schedule are skipped together (one conditional, no crash).
    assert bundle.brief_schedule_pathway_id is None
    with pytest.raises(PathwayError):
        bundle.pathways.get("wombat.brief_schedule")


class _NullEnqueuer:
    """An ``Enqueuer`` that is never expected to be called (no sources are registered below)."""

    def enqueue(self, item: QueueItem) -> EnqueueResult:  # pragma: no cover - never reached
        raise AssertionError("no source should ever enqueue in the shutdown test")


class _RecordingSourceRegistry(SourceRegistry):
    """A real ``SourceRegistry`` (zero sources registered) whose ``stop()`` records that it was
    awaited — a genuine subclass (not a mock) so ``RuntimeBundle``'s typed field stays honest."""

    def __init__(self) -> None:
        super().__init__(_NullEnqueuer())
        self.stop_calls = 0

    async def stop(self) -> None:
        self.stop_calls += 1
        await super().stop()


@dataclass
class _WaitForeverStage:
    """A tiny stage that self-parks on a far-future Wait every time it runs — enough to exercise
    ``serve``'s start -> initial-drive -> Sweeper.run_forever shape without ever firing."""

    name: str = "only"
    transitions: tuple[str, ...] = ("only", "terminal")

    async def run(self, ctx: StageContext) -> StageResult:
        return Wait(
            to="only",
            wake_at=ctx.clock() + timedelta(hours=1),
            output=Artifact(
                kind="noop",
                produced_by=self.name,
                provenance=Provenance(source="system", confidence=1.0, recorded_at=ctx.clock()),
                data={},
            ),
        )


@dataclass
class _TerminalStage:
    """Never reached — exists only so the graph has a terminal stage (construction requirement)."""

    name: str = "terminal"
    transitions: tuple[str, ...] = ()

    async def run(self, ctx: StageContext) -> StageResult:  # pragma: no cover - never reached
        return Done(
            output=Artifact(
                kind="noop",
                produced_by=self.name,
                provenance=Provenance(source="system", confidence=1.0, recorded_at=ctx.clock()),
                data={},
            )
        )


async def test_ac4_shutdown_awaits_registry_stop_on_cancellation() -> None:
    """``_drive_and_serve`` (``serve()``'s inner start/drive/stop loop) stops the SourceRegistry
    and closes the queue/daily-ledger/pending-journal on cancellation (AC4, Q-71 ruling 7).

    Uses a tiny self-parking pathway (never fires) so this stays a pure orchestration test — the
    point is runtime.py's shutdown wiring, not gate/pathway behavior (covered by AC1/AC2/AC5).
    """
    journal = InMemoryJournal()
    pathways = PathwayRegistry()
    graph = StageGraph([_WaitForeverStage(), _TerminalStage()], entry="only")
    pathways.register("only", graph)

    models = ModelRegistry()
    models.register_factory(
        "deepseek",
        lambda guard: FakeModel(raises=AssertionError("the mouth must never be called")),
    )
    engine = Engine(
        models=models,
        journal=journal,
        graph_store=InMemoryGraphStore(),
        latent=InMemoryLatentStore(),
        pathways=pathways,
        model_profile="deepseek",
    )

    registry = _RecordingSourceRegistry()
    # Real adapters (lazy — no adapter here ever actually connects) so shutdown's close() calls
    # exercise the genuine types RuntimeBundle carries.
    queue = WombatQueue(_FAKE_DSN, max_size=10)
    daily_ledger = DailyLedger(_FAKE_DSN, tz=ZoneInfo("UTC"))
    pending_journal = PgPendingJournal(_FAKE_DSN)
    compose_stage = ComposeStage(config=_config(), template_composer=TemplateComposer())
    # TK-176: additive RuntimeBundle fields — a mechanical hand-rolled-construction update
    # (TK-46/TK-52 precedent), not this test's own concern.
    entity_kg = InMemoryEntityKG()
    observation_writer = ObservationWriter(
        entity_kg=entity_kg, scope_registry=ScopeRegistry(), user_id="test-user"
    )

    bundle = RuntimeBundle(
        engine=engine,
        pathways=pathways,
        journal=journal,
        drain_pathway_id="only",
        dream_pathway_id="only",
        dream_schedule_pathway_id=None,
        source_registry=registry,
        pending_journal=pending_journal,
        queue=queue,
        daily_ledger=daily_ledger,
        compose_stage=compose_stage,
        brief_pathway_id=None,
        brief_schedule_pathway_id=None,
        entity_kg=entity_kg,
        observation_writer=observation_writer,
        behavior_event_log=BehaviorEventLog(_FAKE_DSN),
    )
    op = load_operating_params().model_copy(
        update={"sweeper_interval_seconds": 0.01, "sweeper_lease_ttl_seconds": 1.0}
    )

    task: asyncio.Task[None] = asyncio.ensure_future(runtime._drive_and_serve(bundle, params=op))
    # Let it start the registry, run the initial drive (parks WAITING), and enter run_forever's
    # sleep — the only real suspension point in the whole chain (everything else is in-memory).
    for _ in range(50):
        await asyncio.sleep(0)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert registry.stop_calls == 1


# --- TK-97: serve() fires the SECOND initial drive on the schedule pathway when registered -------


@dataclass
class _ScheduleSpyStage:
    """A schedule-pathway entry stage that records each drive and self-parks far in the future —
    stands in for ``BriefTimerStage`` so this stays a pure serve()-wiring test (the fire/fence
    behavior is covered by the stage + e2e tests)."""

    ran: list[int] = field(default_factory=list)
    name: str = "sched_only"
    transitions: tuple[str, ...] = ("sched_only", "sched_terminal")

    async def run(self, ctx: StageContext) -> StageResult:
        self.ran.append(1)
        return Wait(
            to="sched_only",
            wake_at=ctx.clock() + timedelta(hours=1),
            output=Artifact(
                kind="noop",
                produced_by=self.name,
                provenance=Provenance(source="system", confidence=1.0, recorded_at=ctx.clock()),
                data={},
            ),
        )


@dataclass
class _ScheduleTerminalStage:
    name: str = "sched_terminal"
    transitions: tuple[str, ...] = ()

    async def run(self, ctx: StageContext) -> StageResult:  # pragma: no cover - never reached
        return Done(
            output=Artifact(
                kind="noop",
                produced_by=self.name,
                provenance=Provenance(source="system", confidence=1.0, recorded_at=ctx.clock()),
                data={},
            )
        )


def _serve_bundle(
    *, schedule_spy: _ScheduleSpyStage | None, schedule_pathway_id: str | None
) -> tuple[RuntimeBundle, _RecordingSourceRegistry]:
    """Assemble a minimal in-memory ``RuntimeBundle`` with the drain pathway ``only`` plus, when
    ``schedule_spy`` is given, a schedule pathway ``sched`` — so a serve() run can prove the second
    initial drive fires (or is skipped) purely from the ``brief_schedule_pathway_id`` field."""
    journal = InMemoryJournal()
    pathways = PathwayRegistry()
    pathways.register("only", StageGraph([_WaitForeverStage(), _TerminalStage()], entry="only"))
    if schedule_spy is not None:
        pathways.register(
            "sched",
            StageGraph([schedule_spy, _ScheduleTerminalStage()], entry="sched_only"),
        )

    models = ModelRegistry()
    models.register_factory(
        "deepseek",
        lambda guard: FakeModel(raises=AssertionError("the mouth must never be called")),
    )
    engine = Engine(
        models=models,
        journal=journal,
        graph_store=InMemoryGraphStore(),
        latent=InMemoryLatentStore(),
        pathways=pathways,
        model_profile="deepseek",
    )
    registry = _RecordingSourceRegistry()
    # TK-176: additive RuntimeBundle fields — a mechanical hand-rolled-construction update
    # (TK-46/TK-52 precedent), not this test's own concern.
    entity_kg = InMemoryEntityKG()
    observation_writer = ObservationWriter(
        entity_kg=entity_kg, scope_registry=ScopeRegistry(), user_id="test-user"
    )
    bundle = RuntimeBundle(
        engine=engine,
        pathways=pathways,
        journal=journal,
        drain_pathway_id="only",
        dream_pathway_id="only",
        dream_schedule_pathway_id=None,
        source_registry=registry,
        pending_journal=PgPendingJournal(_FAKE_DSN),
        queue=WombatQueue(_FAKE_DSN, max_size=10),
        daily_ledger=DailyLedger(_FAKE_DSN, tz=ZoneInfo("UTC")),
        compose_stage=ComposeStage(config=_config(), template_composer=TemplateComposer()),
        brief_pathway_id=None,
        brief_schedule_pathway_id=schedule_pathway_id,
        entity_kg=entity_kg,
        observation_writer=observation_writer,
        behavior_event_log=BehaviorEventLog(_FAKE_DSN),
    )
    return bundle, registry


async def _run_serve_briefly(bundle: RuntimeBundle) -> None:
    op = load_operating_params().model_copy(
        update={"sweeper_interval_seconds": 0.01, "sweeper_lease_ttl_seconds": 1.0}
    )
    task: asyncio.Task[None] = asyncio.ensure_future(runtime._drive_and_serve(bundle, params=op))
    for _ in range(50):
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_serve_fires_second_initial_drive_on_schedule_when_registered() -> None:
    spy = _ScheduleSpyStage()
    bundle, _registry = _serve_bundle(schedule_spy=spy, schedule_pathway_id="sched")

    await _run_serve_briefly(bundle)

    assert spy.ran == [1]  # the schedule pathway was driven exactly once at boot (timer armed)


async def test_serve_skips_schedule_drive_when_none_and_does_not_crash() -> None:
    # A schedule pathway IS registered, but brief_schedule_pathway_id is None -> serve() must not
    # drive it (and must boot cleanly regardless).
    spy = _ScheduleSpyStage()
    bundle, registry = _serve_bundle(schedule_spy=spy, schedule_pathway_id=None)

    await _run_serve_briefly(bundle)

    assert spy.ran == []  # never driven when the field is None
    assert registry.stop_calls == 1  # still shut down cleanly


# --- TK-173 (CR-15): every DailyLedger constructed during assembly is closed on teardown -------


async def test_daily_ledger_lifecycle_every_constructed_instance_closed_after_teardown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``assemble_runtime`` constructs a ``DailyLedger`` at up to three call sites
    (``build_compose_stage``, ``build_brief_compose_stage``, and the ceiling/day-rollover
    instance) -- every instance ACTUALLY constructed during assembly must be closed once the
    runtime's teardown path runs, not just whichever one ``RuntimeBundle`` happens to expose as
    ``daily_ledger``. Proven by monkeypatching the constructor/``close`` to record every instance,
    then driving the real ``_drive_and_serve`` teardown to completion."""
    constructed: list[DailyLedger] = []
    closed: list[DailyLedger] = []
    real_init = DailyLedger.__init__
    real_close = DailyLedger.close

    def _tracking_init(self: DailyLedger, *args: Any, **kwargs: Any) -> None:
        real_init(self, *args, **kwargs)
        constructed.append(self)

    def _tracking_close(self: DailyLedger) -> None:
        closed.append(self)
        real_close(self)

    monkeypatch.setattr(DailyLedger, "__init__", _tracking_init)
    monkeypatch.setattr(DailyLedger, "close", _tracking_close)

    op = load_operating_params()
    config = _config_with_brief_path(str(tmp_path / "brief.txt"))
    bundle = bootstrap.assemble_runtime(
        config=config, dsn=_FAKE_DSN, params=op, replay_pending=False
    )

    assert len(constructed) >= 1  # sanity: assembly actually built at least one

    # Swap in a trivial self-parking pathway/engine (mirrors the AC4 shutdown test above) so
    # _drive_and_serve's finally teardown runs without ever touching a real Postgres -- the
    # point of this test is the close() lifecycle, not gate/pathway behavior. The real
    # queue/daily_ledger/pending_journal/compose_stage assemble_runtime built are kept as-is.
    journal = InMemoryJournal()
    pathways = PathwayRegistry()
    pathways.register("only", StageGraph([_WaitForeverStage(), _TerminalStage()], entry="only"))
    models = ModelRegistry()
    models.register_factory(
        "deepseek",
        lambda guard: FakeModel(raises=AssertionError("the mouth must never be called")),
    )
    engine = Engine(
        models=models,
        journal=journal,
        graph_store=InMemoryGraphStore(),
        latent=InMemoryLatentStore(),
        pathways=pathways,
        model_profile="deepseek",
    )
    registry = _RecordingSourceRegistry()
    test_bundle = replace(
        bundle,
        engine=engine,
        pathways=pathways,
        journal=journal,
        drain_pathway_id="only",
        source_registry=registry,
        brief_schedule_pathway_id=None,  # skip the second initial drive (its own pathway/engine)
        dream_schedule_pathway_id=None,  # skip the third initial drive (its own pathway/engine)
    )

    run_op = op.model_copy(
        update={"sweeper_interval_seconds": 0.01, "sweeper_lease_ttl_seconds": 1.0}
    )
    task: asyncio.Task[None] = asyncio.ensure_future(
        runtime._drive_and_serve(test_bundle, params=run_op)
    )
    for _ in range(50):
        await asyncio.sleep(0)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert {id(x) for x in constructed} == {id(x) for x in closed}


# --- TK-184 (CR2-10): RuntimeBundle.action_trail_writer is closed on teardown when present -----


class _FakeGmailCredentials:
    """A sentinel standing in for a real ``google.oauth2.credentials.Credentials`` (mirrors
    ``tests/integration/test_outbound_wiring_e2e.py``'s own fake)."""


class _FakeGmailAuth:
    def __init__(self, *, config: WombatConfig, token_store: Any = None) -> None:
        pass

    def get_credentials(self) -> _FakeGmailCredentials:
        return _FakeGmailCredentials()


class _FakeGmailTokenStore:
    def __init__(self, *, initial: str | None = None) -> None:
        self._value = initial

    def load(self) -> str | None:
        return self._value

    def save(self, token: str) -> None:
        self._value = token

    def clear(self) -> None:
        self._value = None


def _config_with_google() -> WombatConfig:
    return WombatConfig(
        deepseek_api_key="sk-test",
        deepseek_base_url="https://api.deepseek.com",
        google_oauth_client_id="test-client-id",
        google_oauth_client_secret=SecretStr("test-client-secret"),
    )


def test_assemble_runtime_with_google_creds_and_token_exposes_action_trail_writer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``assemble_runtime``'s ActionTrailWriter (constructed only when Google client creds AND a
    stored Gmail token are both present, WIRE 2/3) is exposed on ``RuntimeBundle`` (CR2-10) --
    previously constructed but never returned, so nothing could ever close it."""
    monkeypatch.setattr(gmail_session_module, "GmailAuth", _FakeGmailAuth)
    monkeypatch.setattr(gmail_session_module, "AuthorizedSession", lambda creds: object())

    op = load_operating_params()
    bundle = bootstrap.assemble_runtime(
        config=_config_with_google(),
        dsn=_FAKE_DSN,
        params=op,
        replay_pending=False,
        gmail_token_store=_FakeGmailTokenStore(initial="fake-stored-token"),
    )

    assert bundle.action_trail_writer is not None
    assert isinstance(bundle.action_trail_writer, ActionTrailWriter)


def test_assemble_runtime_google_less_boot_action_trail_writer_is_none(
    _no_env_file: None,
) -> None:
    """A Google-less boot (no client creds) never constructs the writer -- the field stays None
    (CR2-10's other half: runtime's teardown must be a no-op for this seam in that case)."""
    op = load_operating_params()
    bundle = bootstrap.assemble_runtime(
        config=_config(), dsn=_FAKE_DSN, params=op, replay_pending=False
    )

    assert bundle.action_trail_writer is None


async def test_action_trail_writer_closed_on_teardown_when_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_drive_and_serve``'s teardown closes ``RuntimeBundle.action_trail_writer`` when present
    -- the exact leak class TK-173/CR-15 closed for ``DailyLedger`` (CR2-10). Mirrors the AC4
    shutdown test's hand-rolled bundle construction (self-parking pathway, in-memory journal) and
    the TK-173 lifecycle test's tracking-``close`` pattern."""
    close_calls: list[ActionTrailWriter] = []
    real_close = ActionTrailWriter.close

    def _tracking_close(self: ActionTrailWriter) -> None:
        close_calls.append(self)
        real_close(self)

    monkeypatch.setattr(ActionTrailWriter, "close", _tracking_close)

    journal = InMemoryJournal()
    pathways = PathwayRegistry()
    pathways.register("only", StageGraph([_WaitForeverStage(), _TerminalStage()], entry="only"))

    models = ModelRegistry()
    models.register_factory(
        "deepseek",
        lambda guard: FakeModel(raises=AssertionError("the mouth must never be called")),
    )
    engine = Engine(
        models=models,
        journal=journal,
        graph_store=InMemoryGraphStore(),
        latent=InMemoryLatentStore(),
        pathways=pathways,
        model_profile="deepseek",
    )

    registry = _RecordingSourceRegistry()
    queue = WombatQueue(_FAKE_DSN, max_size=10)
    daily_ledger = DailyLedger(_FAKE_DSN, tz=ZoneInfo("UTC"))
    pending_journal = PgPendingJournal(_FAKE_DSN)
    compose_stage = ComposeStage(config=_config(), template_composer=TemplateComposer())
    entity_kg = InMemoryEntityKG()
    observation_writer = ObservationWriter(
        entity_kg=entity_kg, scope_registry=ScopeRegistry(), user_id="test-user"
    )
    writer = ActionTrailWriter(_FAKE_DSN)  # lazy -- no connection at construction

    bundle = RuntimeBundle(
        engine=engine,
        pathways=pathways,
        journal=journal,
        drain_pathway_id="only",
        dream_pathway_id="only",
        dream_schedule_pathway_id=None,
        source_registry=registry,
        pending_journal=pending_journal,
        queue=queue,
        daily_ledger=daily_ledger,
        compose_stage=compose_stage,
        brief_pathway_id=None,
        brief_schedule_pathway_id=None,
        entity_kg=entity_kg,
        observation_writer=observation_writer,
        behavior_event_log=BehaviorEventLog(_FAKE_DSN),
        action_trail_writer=writer,
    )
    op = load_operating_params().model_copy(
        update={"sweeper_interval_seconds": 0.01, "sweeper_lease_ttl_seconds": 1.0}
    )

    task: asyncio.Task[None] = asyncio.ensure_future(runtime._drive_and_serve(bundle, params=op))
    for _ in range(50):
        await asyncio.sleep(0)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert close_calls == [writer]


# --- TK-111 (Q-98): RuntimeBundle.behavior_event_log is closed on teardown ----------------------


async def test_behavior_event_log_closed_on_teardown(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_drive_and_serve``'s teardown closes ``RuntimeBundle.behavior_event_log`` — the SAME
    TK-184 lifecycle pattern as ``action_trail_writer``/``daily_ledger``/``pending_journal``/
    ``queue``, but UNCONDITIONAL (this field is never ``None``). Mirrors the AC4 shutdown test's
    hand-rolled bundle construction and the ActionTrailWriter lifecycle test's tracking-``close``
    pattern."""
    close_calls: list[BehaviorEventLog] = []
    real_close = BehaviorEventLog.close

    def _tracking_close(self: BehaviorEventLog) -> None:
        close_calls.append(self)
        real_close(self)

    monkeypatch.setattr(BehaviorEventLog, "close", _tracking_close)

    journal = InMemoryJournal()
    pathways = PathwayRegistry()
    pathways.register("only", StageGraph([_WaitForeverStage(), _TerminalStage()], entry="only"))

    models = ModelRegistry()
    models.register_factory(
        "deepseek",
        lambda guard: FakeModel(raises=AssertionError("the mouth must never be called")),
    )
    engine = Engine(
        models=models,
        journal=journal,
        graph_store=InMemoryGraphStore(),
        latent=InMemoryLatentStore(),
        pathways=pathways,
        model_profile="deepseek",
    )

    registry = _RecordingSourceRegistry()
    queue = WombatQueue(_FAKE_DSN, max_size=10)
    daily_ledger = DailyLedger(_FAKE_DSN, tz=ZoneInfo("UTC"))
    pending_journal = PgPendingJournal(_FAKE_DSN)
    compose_stage = ComposeStage(config=_config(), template_composer=TemplateComposer())
    entity_kg = InMemoryEntityKG()
    observation_writer = ObservationWriter(
        entity_kg=entity_kg, scope_registry=ScopeRegistry(), user_id="test-user"
    )
    behavior_event_log = BehaviorEventLog(_FAKE_DSN)  # lazy -- no connection at construction

    bundle = RuntimeBundle(
        engine=engine,
        pathways=pathways,
        journal=journal,
        drain_pathway_id="only",
        dream_pathway_id="only",
        dream_schedule_pathway_id=None,
        source_registry=registry,
        pending_journal=pending_journal,
        queue=queue,
        daily_ledger=daily_ledger,
        compose_stage=compose_stage,
        brief_pathway_id=None,
        brief_schedule_pathway_id=None,
        entity_kg=entity_kg,
        observation_writer=observation_writer,
        behavior_event_log=behavior_event_log,
    )
    op = load_operating_params().model_copy(
        update={"sweeper_interval_seconds": 0.01, "sweeper_lease_ttl_seconds": 1.0}
    )

    task: asyncio.Task[None] = asyncio.ensure_future(runtime._drive_and_serve(bundle, params=op))
    for _ in range(50):
        await asyncio.sleep(0)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert close_calls == [behavior_event_log]
