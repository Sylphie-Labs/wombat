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
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from cogworx.claims.provenance import Artifact, Provenance
from cogworx.loop.graph import StageGraph
from cogworx.loop.pathway import PathwayError, PathwayRegistry
from cogworx.loop.result import Done, StageResult, Wait
from cogworx.loop.stage import StageContext
from cogworx.loop.state import RunStatus
from cogworx.model.base import ModelResponse
from cogworx.model.registry import ModelRegistry
from cogworx.runtime.engine import Engine
from cogworx.runtime.sweeper import Sweeper
from cogworx.testing.doubles import InMemoryGraphStore, InMemoryJournal, InMemoryLatentStore

from tests.support.stage_context_fake import FakeModel
from wombat import bootstrap, runtime
from wombat.bootstrap import RuntimeBundle
from wombat.compose.templates import TemplateComposer
from wombat.config import ConfigurationError, WombatConfig
from wombat.domain.daily_ledger import DailyLedger
from wombat.gate.models import ItemKind
from wombat.gate.pending_journal_pg import PgPendingJournal
from wombat.params import load_operating_params
from wombat.pathways.drain_pathway import build_drain_pathway
from wombat.queue import EnqueueResult, QueueItem, WombatQueue
from wombat.sources.presence import PresenceSnapshot, PresenceState
from wombat.sources.registry import SourceRegistry
from wombat.stages.compose import ComposeStage
from wombat.stages.compose_dispatch_router import ComposeDispatchRouter
from wombat.stages.drain_queue import DrainQueueStage
from wombat.stages.gate_stage import GateStage, make_stub_evaluator
from wombat.stages.review_or_speak import ReviewOrSpeakStage

_PATHWAY_ID = "wombat.drain"
_URGENCY_THRESHOLD = 0.5
_STALENESS_CEILING_S = 300.0
_CONFIDENCE_FLOOR = 0.5

# A fake Postgres DSN — every adapter TK-53 wires (WombatQueue/DailyLedger/PgPendingJournal) is
# lazy (no connection at construction), so these unit tests never touch a real Postgres.
_FAKE_DSN = "postgresql://fake-host/fake-db"


def _config() -> WombatConfig:
    return WombatConfig(deepseek_api_key="sk-test", deepseek_base_url="https://api.deepseek.com")


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

    graph = build_drain_pathway(
        drain_queue_stage,
        gate_stage,
        review_or_speak_stage,
        compose_dispatch_router,
        compose_stage,
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


def test_build_brief_deliver_stage_none_path_raises_configuration_error() -> None:
    config = _config()  # wombat_brief_path defaults to None

    with pytest.raises(ConfigurationError):
        bootstrap.build_brief_deliver_stage(config=config)


# --- AC4: assemble_runtime registers the pathway + wires the real PG PendingJournal ------------


def test_ac4_assemble_runtime_registers_drain_pathway_and_wires_pg_pending_journal() -> None:
    op = load_operating_params()
    bundle = bootstrap.assemble_runtime(config=_config(), dsn=_FAKE_DSN, params=op)

    # pathways.get resolves the drain pathway id (raises PathwayError if not registered).
    graph = bundle.pathways.get(bundle.drain_pathway_id)
    assert graph is not None
    assert bundle.drain_pathway_id == "wombat.drain"

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

    bundle = bootstrap.assemble_runtime(config=config, dsn=_FAKE_DSN, params=op)

    assert bundle.brief_pathway_id == "wombat.brief"
    # pathways.get resolves the brief pathway id (raises PathwayError if not registered).
    graph = bundle.pathways.get(bundle.brief_pathway_id)
    assert graph is not None
    assert graph.entry == "brief_gather"


def test_assemble_runtime_blank_brief_path_skips_registration_and_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    op = load_operating_params()
    config = _config()  # wombat_brief_path defaults to None

    with caplog.at_level(logging.WARNING):
        bundle = bootstrap.assemble_runtime(config=config, dsn=_FAKE_DSN, params=op)

    assert bundle.brief_pathway_id is None
    assert "WOMBAT_BRIEF_PATH" in caplog.text
    with pytest.raises(PathwayError):
        bundle.pathways.get("wombat.brief")


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

    bundle = RuntimeBundle(
        engine=engine,
        pathways=pathways,
        journal=journal,
        drain_pathway_id="only",
        source_registry=registry,
        pending_journal=pending_journal,
        queue=queue,
        daily_ledger=daily_ledger,
        compose_stage=compose_stage,
        brief_pathway_id=None,
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
