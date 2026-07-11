"""TK-1 — wombat composition root acceptance criteria."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import UTC, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from cogworx.claims.provenance import Artifact, Provenance
from cogworx.loop.graph import StageGraph
from cogworx.loop.pathway import PathwayRegistry
from cogworx.loop.result import StageResult, Transition
from cogworx.loop.state import RunStatus
from cogworx.model.registry import ModelRegistry
from cogworx.runtime.engine import Engine
from cogworx.testing.doubles import InMemoryGraphStore, InMemoryJournal, InMemoryLatentStore

from tests.support.stage_context_fake import FakeModel
from wombat import bootstrap
from wombat.bootstrap import (
    _ENGINE_MAX_STEPS,
    MODEL_PROFILE,
    _log_engine_event,
    build_engine,
    reset_engine,
)
from wombat.config import ConfigurationError, WombatConfig, load_config
from wombat.external_store import ExternalItemStore
from wombat.gate.pending_set import InMemoryPendingJournal, PendingSet
from wombat.params import load_operating_params
from wombat.pathways.brief_pathway import brief_timer_tick_artifact, build_brief_schedule_pathway
from wombat.scratchpad import ScratchpadStore
from wombat.stages.brief_timer_stage import BriefTimerStage
from wombat.substrate import cold_boot_bundle

# The ten seams the Engine must carry after composition (4 required substrate + 6 optional).
_ENGINE_SEAMS = (
    "_models",
    "_journal",
    "_graph_store",
    "_latent",
    "_pathways",
    "_budget_policy",
    "_registry",
    "_recall_stack",
    "_personality",
    "_rules",
)


@pytest.fixture(autouse=True)
def _reset_singleton() -> Iterator[None]:
    reset_engine()
    yield
    reset_engine()


def _config() -> WombatConfig:
    return WombatConfig(deepseek_api_key="sk-test", deepseek_base_url="https://api.deepseek.com")


def test_ac1_cold_launch_returns_engine_with_all_ten_seams() -> None:
    engine = build_engine(cold_boot_bundle(), config=_config())
    for seam in _ENGINE_SEAMS:
        assert getattr(engine, seam) is not None, f"seam {seam} is None"
    assert engine._model_profile == MODEL_PROFILE


def test_ac2_missing_api_key_raises_configuration_error_naming_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # TK-186: chdir off the repo root so a real developer .env (if any) can't supply the
    # missing key out from under this test -- pydantic-settings resolves env_file=".env"
    # relative to CWD.
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    with pytest.raises(ConfigurationError, match="DEEPSEEK_API_KEY"):
        load_config()


def test_ac2_missing_base_url_raises_configuration_error_naming_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.delenv("DEEPSEEK_BASE_URL", raising=False)
    with pytest.raises(ConfigurationError, match="DEEPSEEK_BASE_URL"):
        load_config()


def test_ac3_second_call_returns_same_singleton_no_duplicate() -> None:
    first = build_engine(cold_boot_bundle(), config=_config())
    second = build_engine(cold_boot_bundle(), config=_config())
    assert first is second


def test_deepseek_profile_registered_as_spec_no_model_built() -> None:
    # The model is a descriptor only — composition stays model-silent (registry resolves the spec).
    engine = build_engine(cold_boot_bundle(), config=_config())
    registry = engine._models
    assert registry.resolve_spec(MODEL_PROFILE) is not None


def test_module_exposes_build_engine() -> None:
    assert callable(bootstrap.build_engine)


# --- TK-101: WOMBAT_BRIEF_PATH / WOMBAT_VOICE_ENABLED are OPTIONAL -------------------------------


def test_wombat_config_boots_without_brief_path_or_voice_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # TK-202 (CR3-4, Q-103): chdir off the repo root so a populated operator .env can't supply
    # WOMBAT_BRIEF_PATH/WOMBAT_VOICE_ENABLED out from under this test -- delenv only clears the
    # process env var, and pydantic-settings resolves env_file=".env" relative to CWD (mirrors
    # TK-186's test_ac2_missing_api_key_raises... precedent above).
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WOMBAT_BRIEF_PATH", raising=False)
    monkeypatch.delenv("WOMBAT_VOICE_ENABLED", raising=False)
    config = _config()  # must not raise -- neither is in REQUIRED_ENV
    assert config.wombat_brief_path is None
    assert config.wombat_voice_enabled is False


# --- TK-172 (CR-10): the mid-batch-surface/whole-batch-ack coupling guard -----------------------


def test_guard_drain_batch_size_raises_for_non_one() -> None:
    with pytest.raises(ValueError, match="mid-batch"):
        bootstrap._guard_drain_batch_size(2)


def test_guard_drain_batch_size_noop_for_one() -> None:
    bootstrap._guard_drain_batch_size(1)  # must not raise


def test_assemble_runtime_still_succeeds_at_current_batch_size_of_one() -> None:
    """AC1: the guard is a no-op at the current composition (_DRAIN_BATCH_SIZE == 1) -- assembly
    is byte-identical, no new raise on the real boot path."""
    op = load_operating_params()
    # A fake Postgres DSN -- every adapter assemble_runtime wires is lazy (no connection at
    # construction) with replay_pending=False, so this never touches a real Postgres (mirrors
    # tests/unit/test_runtime.py).
    bundle = bootstrap.assemble_runtime(
        config=_config(),
        dsn="postgresql://fake-host/fake-db",
        params=op,
        replay_pending=False,
        tz=ZoneInfo("UTC"),
    )
    assert bundle.drain_pathway_id == bootstrap.DRAIN_PATHWAY_ID


# --- TK-166 (CR-1, Q-83): replay_pending is the ONE eager-read boot-replay flag -----------------


def test_assemble_runtime_default_replay_pending_calls_rebuild_from_journal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The DEFAULT (``replay_pending=True``, the ``serve()`` production posture) routes the
    gate's pending set through ``PendingSet.rebuild_from_journal`` -- proven via a spy that
    returns a COLD ``PendingSet`` so no real I/O ever happens against the fake DSN."""
    op = load_operating_params()
    calls: list[object] = []
    cold = PendingSet(journal=InMemoryPendingJournal(), max_pending=op.max_pending)

    def spy_rebuild(journal: object, *, max_pending: int) -> PendingSet:
        calls.append(journal)
        return cold

    monkeypatch.setattr(PendingSet, "rebuild_from_journal", spy_rebuild)
    # TK-203 (Q-104): the schema pre-flight also runs unconditionally on this replay_pending=True
    # posture, ahead of rebuild_from_journal -- stubbed out here (a real, separate connection
    # attempt against the fake DSN) so this test stays about ONE thing: rebuild_from_journal
    # routing. Real pg-backed pre-flight coverage lives in tests/unit/test_schema_preflight.py.
    monkeypatch.setattr(bootstrap, "ensure_all_schemas", lambda dsn: None)

    bundle = bootstrap.assemble_runtime(
        config=_config(), dsn="postgresql://fake-host/fake-db", params=op, tz=ZoneInfo("UTC")
    )

    assert len(calls) == 1  # the default path calls rebuild_from_journal exactly once
    assert bundle.drain_pathway_id == bootstrap.DRAIN_PATHWAY_ID


def test_assemble_runtime_replay_pending_false_never_calls_rebuild_from_journal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``replay_pending=False`` never touches ``PendingSet.rebuild_from_journal`` -- the cold
    constructor stands, so a fake/unreachable DSN stays connection-free."""
    op = load_operating_params()
    calls: list[object] = []
    real_rebuild = PendingSet.rebuild_from_journal

    def spy_rebuild(journal: object, *, max_pending: int) -> PendingSet:
        calls.append(journal)
        return real_rebuild(journal, max_pending=max_pending)  # type: ignore[arg-type]

    monkeypatch.setattr(PendingSet, "rebuild_from_journal", spy_rebuild)

    bundle = bootstrap.assemble_runtime(
        config=_config(),
        dsn="postgresql://fake-host/fake-db",
        params=op,
        replay_pending=False,
        tz=ZoneInfo("UTC"),
    )

    assert calls == []  # never called -- the opted-out path stays connection-free
    assert bundle.drain_pathway_id == bootstrap.DRAIN_PATHWAY_ID


# --- TK-46 (Q-85): wombat.dream registers UNCONDITIONALLY, connection-free -----------------------


# --- TK-245 (ruling v2.68 r5): assemble_runtime ALWAYS constructs ExternalItemStore(dsn) ------


def test_assemble_runtime_exposes_a_real_external_item_store() -> None:
    op = load_operating_params()
    bundle = bootstrap.assemble_runtime(
        config=_config(),
        dsn="postgresql://fake-host/fake-db",
        params=op,
        replay_pending=False,
        tz=ZoneInfo("UTC"),
    )
    assert isinstance(bundle.external_item_store, ExternalItemStore)


# --- TK-247 (ruling v2.68 r5): assemble_runtime ALWAYS constructs ScratchpadStore(dsn) ---------


def test_assemble_runtime_exposes_a_real_scratchpad_store() -> None:
    op = load_operating_params()
    bundle = bootstrap.assemble_runtime(
        config=_config(),
        dsn="postgresql://fake-host/fake-db",
        params=op,
        replay_pending=False,
        tz=ZoneInfo("UTC"),
    )
    assert isinstance(bundle.scratchpad_store, ScratchpadStore)


def test_assemble_runtime_registers_dream_pathway_unconditionally() -> None:
    """The TK-166 connection-free assembly pattern (``replay_pending=False``, a fake DSN) proves
    ``wombat.dream`` is registered on the SAME resolvable pathway registry the drain pathway is —
    no ``WOMBAT_BRIEF_PATH``-style conditional gates it (Q-85)."""
    op = load_operating_params()
    bundle = bootstrap.assemble_runtime(
        config=_config(),
        dsn="postgresql://fake-host/fake-db",
        params=op,
        replay_pending=False,
        tz=ZoneInfo("UTC"),
    )
    assert bundle.dream_pathway_id == "wombat.dream"
    assert bundle.pathways.get(bundle.dream_pathway_id) is not None


# --- TK-114 (EP-22, Q-102b-f): the reflection-render leg registers UNCONDITIONALLY ----------------


def test_assemble_runtime_registers_reflection_compose_in_drain_graph() -> None:
    op = load_operating_params()
    bundle = bootstrap.assemble_runtime(
        config=_config(),
        dsn="postgresql://fake-host/fake-db",
        params=op,
        replay_pending=False,
        tz=ZoneInfo("UTC"),
    )
    graph = bundle.pathways.get(bundle.drain_pathway_id)

    assert "reflection_compose" in graph.names()
    # ComposeDispatchRouter's own declared edges cover the injected composer_by_kind map (Q-51) —
    # this proves ItemKind.REFLECTION routes to "reflection_compose" structurally.
    assert "reflection_compose" in graph.transitions_from("compose_dispatch")
    stage = graph.get("reflection_compose")
    assert stage.transitions == ()  # TERMINAL by ruling (Q-102c)


def test_assemble_runtime_reflection_kb_load_failure_boots_with_empty_kb_and_loud_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """CON-3: a psychology-KB load failure never fails the whole boot — ReflectionComposeStage
    is constructed with an empty kb and ONE loud warning is logged."""

    def _raise(path: Path | None = None) -> list[object]:
        raise FileNotFoundError("kb missing")

    monkeypatch.setattr(bootstrap, "load_psychology_kb", _raise)
    op = load_operating_params()

    with caplog.at_level("WARNING"):
        bundle = bootstrap.assemble_runtime(
            config=_config(),
            dsn="postgresql://fake-host/fake-db",
            params=op,
            replay_pending=False,
            tz=ZoneInfo("UTC"),
        )

    graph = bundle.pathways.get(bundle.drain_pathway_id)
    stage = graph.get("reflection_compose")
    assert stage._kb == []  # type: ignore[attr-defined]

    matching = [
        r
        for r in caplog.records
        if "ReflectionComposeStage boots with an empty KB" in r.getMessage()
    ]
    assert len(matching) == 1
    assert matching[0].levelname == "WARNING"


# --- CRF-3 (DEC-41(e)): build_engine pins max_steps=100_000 + a logging event_sink --------------
# so a run's terminal RUN_FAILED (e.g. the max_steps ceiling tripping) is never dropped into a
# None sink and dies silent.


def test_ac1_build_engine_pins_max_steps_and_wombat_event_sink_never_none() -> None:
    engine = build_engine(cold_boot_bundle(), config=_config())
    assert engine._max_steps == 100_000 == _ENGINE_MAX_STEPS
    assert engine._event_sink is _log_engine_event
    assert engine._event_sink is not None


class _LooperStage:
    """A trivial self-looping stage (AC3 harness): every visit is a plain ``Transition`` back to
    itself, never a ``Wait`` — so ``seq`` climbs by one on every drive iteration until the
    engine's ``max_steps`` ceiling trips. ``looper_terminal`` is a declared-but-never-taken stub,
    mirroring ``BriefTimerTerminalStage``'s precedent for satisfying the "graph can end"
    structural invariant without changing runtime behavior.
    """

    name = "looper"
    transitions: tuple[str, ...] = ("looper", "looper_terminal")

    async def run(self, ctx: object) -> StageResult:
        return Transition(
            to="looper",
            output=Artifact(
                kind="tick",
                produced_by="looper",
                provenance=_system_provenance(),
                data={},
            ),
        )


class _LooperTerminalStage:
    name = "looper_terminal"
    transitions: tuple[str, ...] = ()

    async def run(self, ctx: object) -> StageResult:  # pragma: no cover - never reached
        raise RuntimeError("looper_terminal must never be entered")


def _looper_pathway() -> StageGraph:
    return StageGraph([_LooperStage(), _LooperTerminalStage()], entry="looper")


def _system_provenance() -> Provenance:
    return Provenance(source="system", confidence=1.0, recorded_at=datetime.now(UTC))


async def test_ac3_max_steps_ceiling_trip_logs_error_naming_run_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A harness Engine built with a tiny ``max_steps`` and the SAME wombat sink
    (``bootstrap._log_engine_event``): when the ceiling trips, the run flips FAILED and the sink
    logs an ERROR record naming the run_id — never a silent death."""
    pathways = PathwayRegistry()
    pathways.register("test.looper", _looper_pathway())
    models = ModelRegistry()
    # The looper stage never touches ctx.model(), but context assembly eagerly assembles ONE
    # regardless — a factory slot satisfies that eager assembly without ever being called.
    models.register_factory("default", lambda guard: FakeModel())
    engine = Engine(
        models=models,
        journal=InMemoryJournal(),
        graph_store=InMemoryGraphStore(),
        latent=InMemoryLatentStore(),
        pathways=pathways,
        max_steps=3,
        event_sink=_log_engine_event,
    )
    run_id = "ceiling-trip"

    with caplog.at_level(logging.ERROR):
        state = await engine.run(
            run_id=run_id,
            session_id=run_id,
            pathway_id="test.looper",
            initial=Artifact(
                kind="tick",
                produced_by="test",
                provenance=_system_provenance(),
                data={},
            ),
        )

    assert state.status is RunStatus.FAILED
    matching = [
        r for r in caplog.records if r.levelname == "ERROR" and run_id in r.getMessage()
    ]
    assert len(matching) == 1


async def test_ac2_brief_timer_shaped_self_park_survives_2000_wakes_never_fails() -> None:
    """AC2: an engine built with ``build_engine``'s kwargs (``max_steps=100_000`` + the wombat
    sink) lets a ``BriefTimerStage``-shaped eternal ``Wait(to=self)`` run (the TK-97/TK-52 shape)
    survive far past cog-worx's 1000-step default — driven past 2000 wakes via
    ``engine.fire_timer``, the run status stays WAITING, never FAILED."""

    async def _never_called_fire_brief(now: datetime) -> object:  # pragma: no cover
        raise AssertionError("fire_brief must never be called -- ran_today() is always True")

    timer_stage = BriefTimerStage(
        fire_brief=_never_called_fire_brief,  # type: ignore[arg-type]
        ran_today=lambda: True,  # always "already ran" -- pure re-park, no fire, every wake
        mark_ran=lambda: 0,
        tz=ZoneInfo("UTC"),
        brief_time=time(7, 0),
    )
    bundle = cold_boot_bundle()
    bundle.pathways.register("test.brief_schedule", build_brief_schedule_pathway(timer_stage))
    engine = build_engine(bundle, config=_config())

    run_id = "self-park-run"
    now = datetime(2026, 1, 1, tzinfo=UTC)
    state = await engine.run(
        run_id=run_id,
        session_id=run_id,
        pathway_id="test.brief_schedule",
        initial=brief_timer_tick_artifact(now),
    )
    assert state.status is RunStatus.WAITING

    for _ in range(2000):
        state = await engine.fire_timer(run_id)
        assert state.status is RunStatus.WAITING  # never FAILED, across every one of 2000 wakes
