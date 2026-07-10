"""TK-52 — DSN-gated once-per-night dream trigger + idempotency fence acceptance criteria
(EP-13, Q-85).

A REAL cog-worx ``Engine`` drives BOTH ``wombat.dream`` (the TK-46 no-op scaffold pathway) and
``wombat.dream_schedule`` (the ``DreamTimerStage`` self-parking scheduler, TK-52) over a REAL
``DailyLedger``/``DreamRunLedger`` on a throwaway Postgres — mirrors ``test_brief_schedule_e2e.py``
exactly (the mouth is never reachable here since neither pathway ever calls the model). ALL tests
require a real Postgres and are gated on ``WOMBAT_TEST_PG_DSN`` (skipped LOUDLY at collection
otherwise):

    docker run --rm -d -p 5588:5432 -e POSTGRES_PASSWORD=wombat postgres:16
    WOMBAT_TEST_PG_DSN=postgresql://postgres:wombat@localhost:5588/postgres

The dream scaffold has no observable external side effect (unlike the brief's file sink), so
"was it fired" is witnessed by a ``_CountingDreamStage``/``_RaisingDreamStage`` double standing in
for ``DreamScaffoldStage`` in the ``wombat.dream`` pathway — never the production stage itself
(mirrors ``test_dream_pathway_e2e.py``'s own ``_RaisingDreamStage`` injection pattern). A single
mutable ``now`` holder is shared by the engine clock, the ``DailyLedger`` clock, and the timer's
due-check, so a "clock jump" (sleep/crash) is modelled by mutating it. The timer is driven via
``engine.run``/``Sweeper.tick``/``stage.run`` directly — ``run_forever`` is NEVER called unbounded.

  AC1 recurrence over cog-worx's non-recurring Wait: the timer ALWAYS re-arms on the next night's
      Wait, regardless of whether the fire succeeded (marks) or raised (does not mark).
  AC2 once per night, real pg: (a) a same-night double-drive fires exactly once (fence-read skip,
      no double-mark) + the Engine's own run_id guard independently dedupes a direct double-fire
      on the same night-keyed run_id (replay, not re-run); (b) a crash mid-fire (raising dream
      stage) leaves the night unmarked, and a restart-equivalent fresh assembly over the SAME DSN
      re-fires exactly once for that night.
  AC3 sleep-jump: parked early (before the target), the clock jumps past it, ONE Sweeper.tick
      catches the miss and fires exactly once (mirrors TK-97's proven sleep-jump test).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import psycopg
import pytest
from cogworx.claims.provenance import Artifact, Provenance
from cogworx.loop.pathway import PathwayRegistry
from cogworx.loop.result import Done, StageResult, Transition, Wait
from cogworx.loop.stage import Stage, StageContext
from cogworx.loop.state import RunStatus
from cogworx.model.registry import ModelRegistry
from cogworx.runtime.engine import Engine
from cogworx.runtime.sweeper import Sweeper
from cogworx.substrate.journal import RunState
from cogworx.testing.doubles import InMemoryGraphStore, InMemoryJournal, InMemoryLatentStore

from tests.support.stage_context_fake import FakeModel, StageContextFake
from wombat.domain.brief_schedule import next_fire_at
from wombat.domain.daily_ledger import DailyLedger, wombat_today
from wombat.domain.daily_ledger import ensure_schema as ensure_daily_ledger_schema
from wombat.pathways.dream_pathway import (
    DREAM_PATHWAY_ID,
    DREAM_REPORT_KIND,
    build_dream_pathway,
    dream_trigger_artifact,
)
from wombat.pathways.dream_trigger import (
    DREAM_SCHEDULE_PATHWAY_ID,
    DreamRunLedger,
    DreamTimerStage,
    build_dream_schedule_pathway,
    dream_timer_tick_artifact,
)

_DSN = os.environ.get("WOMBAT_TEST_PG_DSN")

if not _DSN:
    pytest.skip(
        "WOMBAT_TEST_PG_DSN is not set — skipping the TK-52 dream-schedule e2e battery, which "
        "requires a real throwaway Postgres. Start one with:\n"
        "  docker run --rm -d -p 5588:5432 -e POSTGRES_PASSWORD=wombat postgres:16\n"
        "then export WOMBAT_TEST_PG_DSN=postgresql://postgres:wombat@localhost:5588/postgres",
        allow_module_level=True,
    )

_TZ = ZoneInfo("America/Chicago")
_DREAM_TIME = time(2, 0)
_NIGHT = (2026, 7, 3)  # a normal (non-DST) wombat-night
_NIGHT_2 = (2026, 7, 4)  # a distinct night — a fresh, unmarked ledger row


def _at(day: tuple[int, int, int], hour: int, minute: int = 0) -> datetime:
    return datetime(*day, hour, minute, tzinfo=_TZ)


def _night_keyed_run_id(now: datetime) -> str:
    return f"wombat-dream-{wombat_today(now, _TZ).isoformat()}"


@dataclass
class _PassthroughConsolidateStage:
    """TK-47 mechanical reshape (flagged per the ticket's own sanction): ``wombat.dream``'s entry
    is now ``dream_consolidate`` -> ``dream_outcome`` -> ... -> ``dream_pattern`` -> ``dream_run``,
    so this suite's own
    doubles (below) — which stand in for the TERMINAL ``DreamScaffoldStage``, the ONLY stage this
    suite's fire-count witnesses care about — need a real entry stage ahead of them. This trivial
    double always transitions straight onward; it carries none of ``DreamConsolidationStage``'s
    reconciler/extractor drain behavior (TK-47 owns that, out of scope for the TK-52 timer/fence
    suite this file tests)."""

    name: str = "dream_consolidate"
    transitions: tuple[str, ...] = ("dream_outcome",)

    async def run(self, ctx: StageContext) -> StageResult:
        return Transition(
            to="dream_outcome",
            output=Artifact(
                kind=DREAM_REPORT_KIND,
                produced_by=self.name,
                provenance=Provenance(source="system", confidence=1.0, recorded_at=ctx.clock()),
                data={},
            ),
        )


@dataclass
class _PassthroughOutcomeStage:
    """TK-175 mechanical reshape: ``wombat.dream``'s second stage — always transitions straight
    onward; it carries none of ``DreamOutcomeStage``'s collect/infer/label behavior (TK-175 owns
    that, out of scope for the TK-52 timer/fence suite this file tests)."""

    name: str = "dream_outcome"
    transitions: tuple[str, ...] = ("dream_tune",)

    async def run(self, ctx: StageContext) -> StageResult:
        return Transition(
            to="dream_tune",
            output=Artifact(
                kind=DREAM_REPORT_KIND,
                produced_by=self.name,
                provenance=Provenance(source="system", confidence=1.0, recorded_at=ctx.clock()),
                data={},
            ),
        )


@dataclass
class _PassthroughTuneStage:
    """TK-49 mechanical reshape: ``wombat.dream``'s third stage — always transitions straight
    onward; it carries none of ``DreamTuneStage``'s ``RatingTuner`` invocation (TK-49 owns that,
    out of scope for the TK-52 timer/fence suite this file tests)."""

    name: str = "dream_tune"
    transitions: tuple[str, ...] = ("dream_persona",)

    async def run(self, ctx: StageContext) -> StageResult:
        return Transition(
            to="dream_persona",
            output=Artifact(
                kind=DREAM_REPORT_KIND,
                produced_by=self.name,
                provenance=Provenance(source="system", confidence=1.0, recorded_at=ctx.clock()),
                data={},
            ),
        )


@dataclass
class _PassthroughPersonaStage:
    """TK-214 mechanical reshape (flagged per the ticket's own sanction, EP-35):
    ``wombat.dream``'s new fourth stage — always transitions straight onward; it carries none of
    ``DreamPersonaStage``'s feedback-tuning behavior (TK-214 owns that, out of scope for the
    TK-52 timer/fence suite this file tests)."""

    name: str = "dream_persona"
    transitions: tuple[str, ...] = ("dream_behavior_log",)

    async def run(self, ctx: StageContext) -> StageResult:
        return Transition(
            to="dream_behavior_log",
            output=Artifact(
                kind=DREAM_REPORT_KIND,
                produced_by=self.name,
                provenance=Provenance(source="system", confidence=1.0, recorded_at=ctx.clock()),
                data={},
            ),
        )


@dataclass
class _PassthroughBehaviorLogStage:
    """TK-111 mechanical reshape (flagged per the ticket's own sanction, Q-98): ``wombat.dream``'s
    fifth stage (post-TK-214) — always transitions straight onward; it carries none of
    ``DreamBehaviorLogStage``'s ``BehaviorEventLog`` write behavior (TK-111 owns that, out of
    scope for the TK-52 timer/fence suite this file tests)."""

    name: str = "dream_behavior_log"
    transitions: tuple[str, ...] = ("dream_window",)

    async def run(self, ctx: StageContext) -> StageResult:
        return Transition(
            to="dream_window",
            output=Artifact(
                kind=DREAM_REPORT_KIND,
                produced_by=self.name,
                provenance=Provenance(source="system", confidence=1.0, recorded_at=ctx.clock()),
                data={},
            ),
        )


@dataclass
class _PassthroughWindowStage:
    """TK-112 mechanical reshape (flagged per the ticket's own sanction, Q-99e):
    ``wombat.dream``'s sixth stage (post-TK-214) — always transitions straight onward; it carries
    none of
    ``WriteWindowSummariesStage``'s detect/write behavior (TK-112 owns that, out of scope for the
    TK-52 timer/fence suite this file tests)."""

    name: str = "dream_window"
    transitions: tuple[str, ...] = ("dream_pattern",)

    async def run(self, ctx: StageContext) -> StageResult:
        return Transition(
            to="dream_pattern",
            output=Artifact(
                kind=DREAM_REPORT_KIND,
                produced_by=self.name,
                provenance=Provenance(source="system", confidence=1.0, recorded_at=ctx.clock()),
                data={},
            ),
        )


@dataclass
class _PassthroughPatternStage:
    """TK-113 mechanical reshape (flagged per the ticket's own sanction, Q-99f):
    ``wombat.dream``'s seventh stage (post-TK-214) — always transitions straight onward; it
    carries none of
    ``PatternDetectorStage``'s read/match/enqueue behavior (TK-113 owns that, out of scope for the
    TK-52 timer/fence suite this file tests)."""

    name: str = "dream_pattern"
    transitions: tuple[str, ...] = ("dream_run",)

    async def run(self, ctx: StageContext) -> StageResult:
        return Transition(
            to="dream_run",
            output=Artifact(
                kind=DREAM_REPORT_KIND,
                produced_by=self.name,
                provenance=Provenance(source="system", confidence=1.0, recorded_at=ctx.clock()),
                data={},
            ),
        )


@dataclass
class _CountingDreamStage:
    """A ``wombat.dream`` terminal stage double that RECORDS each invocation (mirrors
    ``DreamScaffoldStage``'s shape — ``Done``, no model call, contentless system-provenanced
    output — but with a counter no production stage carries, the fire-count witness AC1/AC2/AC3
    need since the scaffold has no other observable side effect)."""

    fired: list[datetime] = field(default_factory=list)
    name: str = "dream_run"
    transitions: tuple[str, ...] = ()

    async def run(self, ctx: StageContext) -> StageResult:
        self.fired.append(ctx.clock())
        return Done(
            output=Artifact(
                kind=DREAM_REPORT_KIND,
                produced_by=self.name,
                provenance=Provenance(source="system", confidence=1.0, recorded_at=ctx.clock()),
                data={"changes": 0, "scaffold": True},
            )
        )


@dataclass
class _RaisingDreamStage:
    """A ``wombat.dream`` terminal stage double that ALWAYS raises — the AC1/AC2(b) crash-mid-fire
    injection seam (mirrors ``test_dream_pathway_e2e.py``'s own ``_RaisingDreamStage``)."""

    name: str = "dream_run"
    transitions: tuple[str, ...] = ()

    async def run(self, ctx: StageContext) -> StageResult:
        raise ConnectionError("simulated dream crash mid-fire")


@dataclass
class _Scheduler:
    """A fully-wired real scheduler stack over docker pg — everything a test drives."""

    engine: Engine
    journal: InMemoryJournal
    daily_ledger: DailyLedger
    dream_run_ledger: DreamRunLedger
    timer_stage: DreamTimerStage
    dream_stage: Stage
    schedule_pathway_id: str


def _build_scheduler(*, now_holder: list[datetime], dream_stage: Stage | None = None) -> _Scheduler:
    """Assemble a REAL Engine over ``wombat.dream`` + ``wombat.dream_schedule`` with a real pg
    ``DailyLedger``/``DreamRunLedger`` — the composition mirror of ``bootstrap.assemble_runtime``'s
    TK-52 wiring (fire_dream built after the engine, night-keyed run_id, schedule registered)."""
    assert _DSN is not None
    clock = lambda: now_holder[0]  # noqa: E731 - the single shared mutable clock

    stage: Stage = dream_stage if dream_stage is not None else _CountingDreamStage()
    pathways = PathwayRegistry()
    pathways.register(
        DREAM_PATHWAY_ID,
        build_dream_pathway(
            _PassthroughConsolidateStage(),
            _PassthroughOutcomeStage(),
            _PassthroughTuneStage(),
            _PassthroughPersonaStage(),
            _PassthroughBehaviorLogStage(),
            _PassthroughWindowStage(),
            _PassthroughPatternStage(),
            terminal=stage,
        ),
    )

    journal = InMemoryJournal()
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
        clock=clock,
    )

    async def fire_dream(now: datetime) -> RunState:
        run_id = _night_keyed_run_id(now)
        return await engine.run(
            run_id=run_id,
            session_id=run_id,
            pathway_id=DREAM_PATHWAY_ID,
            initial=dream_trigger_artifact(now),
        )

    daily_ledger = DailyLedger(_DSN, tz=_TZ, clock=clock)
    dream_run_ledger = DreamRunLedger(daily_ledger)
    timer_stage = DreamTimerStage(
        fire_dream=fire_dream,
        ran_tonight=dream_run_ledger.ran_tonight,
        mark_ran=dream_run_ledger.mark_ran,
        tz=_TZ,
        dream_time=_DREAM_TIME,
    )
    pathways.register(DREAM_SCHEDULE_PATHWAY_ID, build_dream_schedule_pathway(timer_stage))

    return _Scheduler(
        engine=engine,
        journal=journal,
        daily_ledger=daily_ledger,
        dream_run_ledger=dream_run_ledger,
        timer_stage=timer_stage,
        dream_stage=stage,
        schedule_pathway_id=DREAM_SCHEDULE_PATHWAY_ID,
    )


@pytest.fixture
def clean_ledger() -> None:
    """Ensure the ``daily_ledger`` schema exists and is empty (mirrors the TK-97 convention)."""
    assert _DSN is not None
    with psycopg.connect(_DSN) as conn:
        ensure_daily_ledger_schema(conn)
        with conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE daily_ledger")
        conn.commit()


# --- AC1: recurrence — re-arms regardless of whether the fire succeeded or errored --------------


async def test_ac1_recurrence_re_arms_on_success(clean_ledger: None) -> None:
    now_holder = [_at(_NIGHT, 3, 0)]  # 03:00, past tonight's 02:00 target
    stage = _CountingDreamStage()
    sched = _build_scheduler(now_holder=now_holder, dream_stage=stage)
    try:
        ctx = StageContextFake(now_fn=lambda: now_holder[0])
        result = await sched.timer_stage.run(ctx)

        assert isinstance(result, Wait)
        assert result.to == "dream_timer"  # self-parks, never a one-shot terminal
        assert result.wake_at == next_fire_at(now_holder[0], _TZ, _DREAM_TIME)  # next NIGHT's Wait
        assert len(stage.fired) == 1  # fired exactly once
        assert sched.dream_run_ledger.ran_tonight() is True
    finally:
        sched.daily_ledger.close()


async def test_ac1_recurrence_re_arms_on_a_raising_fire_too(clean_ledger: None) -> None:
    now_holder = [_at(_NIGHT_2, 3, 0)]  # a DISTINCT night — a fresh, unmarked ledger row
    sched = _build_scheduler(now_holder=now_holder, dream_stage=_RaisingDreamStage())
    try:
        ctx = StageContextFake(now_fn=lambda: now_holder[0])
        result = await sched.timer_stage.run(ctx)

        assert isinstance(result, Wait)
        assert result.to == "dream_timer"  # STILL re-parks — the re-arm is unconditional
        assert result.wake_at == next_fire_at(now_holder[0], _TZ, _DREAM_TIME)
        assert sched.dream_run_ledger.ran_tonight() is False  # unmarked (the fire raised)
    finally:
        sched.daily_ledger.close()


# --- AC2(a): same-night double-drive fires exactly once; the Engine's own run_id guard dedupes --


async def test_ac2a_same_night_double_drive_fires_once_no_double_mark(clean_ledger: None) -> None:
    now_holder = [_at(_NIGHT, 3, 0)]
    stage = _CountingDreamStage()
    sched = _build_scheduler(now_holder=now_holder, dream_stage=stage)
    try:
        ctx = StageContextFake(now_fn=lambda: now_holder[0])
        first = await sched.timer_stage.run(ctx)
        assert isinstance(first, Wait)
        assert len(stage.fired) == 1
        assert sched.daily_ledger.current_row("dream:run").value == 1

        # A second pass the SAME night (e.g. a crash-restart boot drive): the fence-read skips.
        second = await sched.timer_stage.run(ctx)
        assert isinstance(second, Wait)
        assert len(stage.fired) == 1  # NOT fired again
        assert sched.daily_ledger.current_row("dream:run").value == 1  # NOT double-marked
    finally:
        sched.daily_ledger.close()


async def test_ac2a_engine_run_id_guard_dedupes_a_direct_double_fire(clean_ledger: None) -> None:
    """The SECOND idempotency layer (Q-85): even bypassing the ledger fence entirely, calling
    ``engine.run`` twice with the SAME night-keyed run_id fires the dream stage only ONCE — the
    second call replays the already-committed ``Done`` step rather than re-running it."""
    now_holder = [_at(_NIGHT, 3, 0)]
    stage = _CountingDreamStage()
    sched = _build_scheduler(now_holder=now_holder, dream_stage=stage)
    try:
        run_id = _night_keyed_run_id(now_holder[0])

        first_state = await sched.engine.run(
            run_id=run_id,
            session_id=run_id,
            pathway_id=DREAM_PATHWAY_ID,
            initial=dream_trigger_artifact(now_holder[0]),
        )
        assert first_state.status is RunStatus.COMPLETED
        assert len(stage.fired) == 1

        second_state = await sched.engine.run(
            run_id=run_id,
            session_id=run_id,
            pathway_id=DREAM_PATHWAY_ID,
            initial=dream_trigger_artifact(now_holder[0]),
        )
        assert second_state.status is RunStatus.COMPLETED
        assert len(stage.fired) == 1  # replay, NOT a second execution
    finally:
        sched.daily_ledger.close()


# --- AC2(b): crash mid-fire -> unmarked; a restart-equivalent fresh assembly re-fires once -------


async def test_ac2b_crash_mid_fire_unmarked_then_restart_refires_exactly_once(
    clean_ledger: None,
) -> None:
    now_holder = [_at(_NIGHT, 3, 0)]
    crashing = _RaisingDreamStage()
    sched = _build_scheduler(now_holder=now_holder, dream_stage=crashing)
    try:
        ctx = StageContextFake(now_fn=lambda: now_holder[0])
        result = await sched.timer_stage.run(ctx)

        assert isinstance(result, Wait)  # the crash is swallowed; the timer re-parks
        assert sched.daily_ledger.current_row("dream:run").value == 0  # unmarked
    finally:
        sched.daily_ledger.close()

    # Restart-equivalent: a FRESH scheduler (new Engine/journal, new night-keyed fire_dream
    # closure) over the SAME DSN/night/clock, with a real (succeeding) dream stage — mirrors a
    # process restart after the crash.
    stage2 = _CountingDreamStage()
    restarted = _build_scheduler(now_holder=now_holder, dream_stage=stage2)
    try:
        ctx2 = StageContextFake(now_fn=lambda: now_holder[0])
        result2 = await restarted.timer_stage.run(ctx2)

        assert isinstance(result2, Wait)
        assert len(stage2.fired) == 1  # exactly one successful fire for the night
        assert restarted.daily_ledger.current_row("dream:run").value == 1  # now marked
    finally:
        restarted.daily_ledger.close()


# --- AC3: sleep-jump — parked early, clock jumps past the target, ONE Sweeper.tick catches it ----


async def test_ac3_sleep_jump_single_tick_catches_the_miss(clean_ledger: None) -> None:
    now_holder = [_at(_NIGHT, 0, 30)]  # 00:30, BEFORE the 02:00 target
    stage = _CountingDreamStage()
    sched = _build_scheduler(now_holder=now_holder, dream_stage=stage)
    try:
        run_id = "sched-ac3"
        parked = await sched.engine.run(
            run_id=run_id,
            session_id=run_id,
            pathway_id=sched.schedule_pathway_id,
            initial=dream_timer_tick_artifact(now_holder[0]),
        )
        assert parked.status is RunStatus.WAITING
        assert len(stage.fired) == 0  # nothing fired yet (early)
        assert sched.daily_ledger.current_row("dream:run").value == 0

        # The laptop slept across the target; the clock jumps to 05:00 and ONE Sweeper.tick claims
        # the now-overdue Wait and re-drives the parked run through fire_timer.
        now_holder[0] = _at(_NIGHT, 5, 0)
        sweeper = Sweeper(
            journal=sched.journal, fire=sched.engine.fire_timer, clock=lambda: now_holder[0]
        )
        fired = await sweeper.tick(now_holder[0], lease_ttl=timedelta(seconds=60))

        assert fired == 1  # exactly the one overdue timer
        assert len(stage.fired) == 1  # fired exactly once on the catch
        assert sched.daily_ledger.current_row("dream:run").value == 1
    finally:
        sched.daily_ledger.close()
