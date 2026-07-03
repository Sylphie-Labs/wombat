"""TK-97 — DSN-gated once-daily brief-timer acceptance criteria (EP-1, Q-80, Q-46).

A REAL cog-worx ``Engine`` drives BOTH ``wombat.brief`` (the four-stage brief pathway) and
``wombat.brief_schedule`` (the ``BriefTimerStage`` self-parking scheduler) over a REAL
``DailyLedger``/``BriefRunLedger`` on a throwaway Postgres (the ONE pg-touching seam — the gate
rides an in-memory pending journal, mirroring ``test_brief_pathway_e2e.py``). ALL tests require a
real Postgres and are gated on ``WOMBAT_TEST_PG_DSN`` (skipped LOUDLY at collection otherwise):

    docker run --rm -d -p 5588:5432 -e POSTGRES_PASSWORD=wombat postgres:16
    WOMBAT_TEST_PG_DSN=postgresql://postgres:wombat@localhost:5588/postgres

The mouth is UNREACHABLE (Q-77 degrade), so each delivered brief is the deterministic
``render_brief_lines`` rendering — a stable place to count ``[run=...]`` headers. A single mutable
``now`` holder is shared by the engine clock, the ``DailyLedger`` clock, and the timer's due-check,
so a "clock jump" (sleep/crash) is modelled by mutating it. The timer is driven via
``engine.run``/``Sweeper.tick``/``stage.run`` directly — ``run_forever`` is NEVER called unbounded.

  AC1 one boot drive fires the brief exactly once + the ledger records today (value 1); re-parks.
  AC2 miss-catch, both variants: (a) parked-early then a clock jump past the target + one
      ``Sweeper.tick`` fires it once; (b) a cold boot LATE in the morning fires the missed brief
      immediately. Both mark the day.
  AC3 a second pass the same day (a crash-restart boot after the brief already ran) is SKIPPED
      with a DEBUG log — no second brief, ledger still 1.
  AC4 exactly-once DELIVERY, both windows: (a) crash-before-Deliver (a fire that raised, unmarked)
      -> the next pass re-fires and delivers exactly ONE brief; (b) crash-after-append-before-mark
      -> the same day-keyed run_id hits the TK-101 file marker (replay=True), no second append,
      then the mark lands.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg
import pytest
from cogworx.loop.pathway import PathwayRegistry
from cogworx.loop.result import Wait
from cogworx.loop.state import RunStatus
from cogworx.model.registry import ModelRegistry
from cogworx.runtime.engine import Engine
from cogworx.runtime.sweeper import Sweeper
from cogworx.substrate.journal import RunState
from cogworx.testing.doubles import (
    InMemoryEntityKG,
    InMemoryGraphStore,
    InMemoryJournal,
    InMemoryLatentStore,
)

from tests.support.stage_context_fake import FakeModel, StageContextFake
from wombat.calendar.models import CalendarEvent
from wombat.config import WombatConfig
from wombat.domain.brief_schedule import BriefRunLedger
from wombat.domain.daily_ledger import DailyLedger, wombat_today
from wombat.domain.daily_ledger import ensure_schema as ensure_daily_ledger_schema
from wombat.gate.decay import LedgerReset
from wombat.gate.pending_set import InMemoryPendingJournal, PendingSet
from wombat.gate.pipeline import Gate
from wombat.integrations.gmail.models import GmailMessageItem
from wombat.integrations.gmail.triage import load_triage_rules
from wombat.pathways.brief_pathway import (
    BRIEF_PATHWAY_ID,
    BRIEF_SCHEDULE_PATHWAY_ID,
    brief_timer_tick_artifact,
    brief_trigger_artifact,
    build_brief_pathway,
    build_brief_schedule_pathway,
)
from wombat.stages.artifacts import brief_delivered_from_artifact_data
from wombat.stages.brief_compose_stage import BriefComposeStage
from wombat.stages.brief_deliver_stage import BriefDeliverStage
from wombat.stages.brief_force_flush_stage import BriefForceFlushStage
from wombat.stages.brief_gather_stage import BriefGatherStage
from wombat.stages.brief_timer_stage import BriefTimerStage
from wombat.user_model.user_model import UserModel

_DSN = os.environ.get("WOMBAT_TEST_PG_DSN")

if not _DSN:
    pytest.skip(
        "WOMBAT_TEST_PG_DSN is not set — skipping the TK-97 brief-timer e2e battery, which "
        "requires a real throwaway Postgres. Start one with:\n"
        "  docker run --rm -d -p 5588:5432 -e POSTGRES_PASSWORD=wombat postgres:16\n"
        "then export WOMBAT_TEST_PG_DSN=postgresql://postgres:wombat@localhost:5588/postgres",
        allow_module_level=True,
    )

_TZ = ZoneInfo("America/Chicago")
_SEVEN_AM = time(7, 0)
_DAY = (2026, 7, 3)  # a normal (non-DST) wombat-day


def _at(hour: int, minute: int = 0) -> datetime:
    return datetime(*_DAY, hour, minute, tzinfo=_TZ)


def _day_keyed_run_id(now: datetime) -> str:
    return f"wombat-brief-{wombat_today(now, _TZ).isoformat()}"


def _config() -> WombatConfig:
    return WombatConfig(deepseek_api_key="sk-test", deepseek_base_url="https://api.deepseek.com")


def _one_event() -> CalendarEvent:
    start = _at(9, 0)
    return CalendarEvent(
        event_id="evt-1", title="Dentist", start=start, end=_at(10, 0), all_day=False
    )


def _one_message() -> GmailMessageItem:
    return GmailMessageItem(
        message_id="m1",
        subject="Invoice due",
        sender="billing@example.com",
        received_at=_at(6, 0),
        body_text="irrelevant — brief_gather never reads this field",
    )


class _NoOpRollover:
    def check(self) -> LedgerReset | None:
        return None


class _UntouchedCeiling:
    def allow(self, event_class: object) -> bool:  # pragma: no cover - never reached
        raise AssertionError("select_items must never read the ceiling")

    def record(self, event_class: object) -> None:  # pragma: no cover - never reached
        raise AssertionError("select_items must never record the ceiling")


@dataclass
class _Scheduler:
    """A fully-wired real scheduler stack over docker pg — everything a test drives."""

    engine: Engine
    journal: InMemoryJournal
    daily_ledger: DailyLedger
    brief_run_ledger: BriefRunLedger
    timer_stage: BriefTimerStage
    schedule_pathway_id: str
    sink: Path


def _build_scheduler(
    *,
    now_holder: list[datetime],
    sink: Path,
    model_factory: object,
    fetch_calendar: object = None,
    fetch_gmail: object = None,
) -> _Scheduler:
    """Assemble a REAL Engine over ``wombat.brief`` + ``wombat.brief_schedule`` with a real pg
    ``DailyLedger``/``BriefRunLedger`` — the composition mirror of ``bootstrap.assemble_runtime``'s
    TK-97 wiring (fire_brief built after the engine, day-keyed run_id, schedule registered)."""
    assert _DSN is not None
    clock = lambda: now_holder[0]  # noqa: E731 - the single shared mutable clock

    gate = Gate(
        user_model=UserModel(entity_kg=InMemoryEntityKG(), user_id="brief-timer-e2e"),
        pending_set=PendingSet(journal=InMemoryPendingJournal(), max_pending=100),
        ceiling=_UntouchedCeiling(),
        urgency_threshold=-1.0,  # every gathered item clears -> tests scheduling, not scoring
        load_flush_threshold=10.0,
        flush_min_age_seconds=300.0,
        decay_ttl_seconds=float("inf"),
        day_rollover=_NoOpRollover(),
        clock=lambda: now_holder[0].timestamp(),
    )
    gather = BriefGatherStage(
        fetch_calendar=fetch_calendar or (lambda: [_one_event()]),  # type: ignore[arg-type]
        fetch_gmail=fetch_gmail or (lambda: [_one_message()]),  # type: ignore[arg-type]
        triage_rules=load_triage_rules(),
        clock=clock,
    )
    force_flush = BriefForceFlushStage(select_items=gate.select_items, tz=_TZ)
    compose = BriefComposeStage(config=_config(), tz=_TZ)
    deliver = BriefDeliverStage(sink_path=sink, tz=_TZ, voice_enabled=False)

    pathways = PathwayRegistry()
    pathways.register(BRIEF_PATHWAY_ID, build_brief_pathway(gather, force_flush, compose, deliver))

    journal = InMemoryJournal()
    models = ModelRegistry()
    models.register_factory("deepseek", model_factory)  # type: ignore[arg-type]
    engine = Engine(
        models=models,
        journal=journal,
        graph_store=InMemoryGraphStore(),
        latent=InMemoryLatentStore(),
        pathways=pathways,
        model_profile="deepseek",
        clock=clock,
    )

    async def fire_brief(now: datetime) -> RunState:
        run_id = _day_keyed_run_id(now)
        return await engine.run(
            run_id=run_id,
            session_id=run_id,
            pathway_id=BRIEF_PATHWAY_ID,
            initial=brief_trigger_artifact(now),
        )

    daily_ledger = DailyLedger(_DSN, tz=_TZ, clock=clock)
    brief_run_ledger = BriefRunLedger(daily_ledger)
    timer_stage = BriefTimerStage(
        fire_brief=fire_brief,
        ran_today=brief_run_ledger.ran_today,
        mark_ran=brief_run_ledger.mark_ran,
        tz=_TZ,
        brief_time=_SEVEN_AM,
    )
    pathways.register(BRIEF_SCHEDULE_PATHWAY_ID, build_brief_schedule_pathway(timer_stage))

    return _Scheduler(
        engine=engine,
        journal=journal,
        daily_ledger=daily_ledger,
        brief_run_ledger=brief_run_ledger,
        timer_stage=timer_stage,
        schedule_pathway_id=BRIEF_SCHEDULE_PATHWAY_ID,
        sink=sink,
    )


def _unreachable_model(guard: object) -> FakeModel:
    return FakeModel(raises=ConnectionError("simulated DeepSeek outage — brief degrades cleanly"))


def _header_count(sink: Path, run_id: str) -> int:
    if not sink.exists():
        return 0
    return sink.read_text(encoding="utf-8").count(f"[run={run_id}]")


@pytest.fixture
def clean_ledger() -> None:
    """Ensure the ``daily_ledger`` schema exists and is empty (mirrors the drain e2e convention)."""
    assert _DSN is not None
    with psycopg.connect(_DSN) as conn:
        ensure_daily_ledger_schema(conn)
        with conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE daily_ledger")
        conn.commit()


async def _drive_schedule(sched: _Scheduler, run_id: str) -> RunState:
    """One boot/initial drive of the schedule pathway (the timer runs once, then self-parks)."""
    # The initial artifact is not read by the timer stage (it gathers via ctx.clock()); any
    # system-provenanced tick satisfies the engine's ``initial: Artifact`` requirement.
    return await sched.engine.run(
        run_id=run_id,
        session_id=run_id,
        pathway_id=sched.schedule_pathway_id,
        initial=brief_timer_tick_artifact(datetime.now(UTC)),
    )


# --- AC1: one boot drive fires exactly once + the ledger records today --------------------------


async def test_ac1_one_boot_drive_fires_once_and_records_today(
    clean_ledger: None, tmp_path: Path
) -> None:
    sink = tmp_path / "brief.txt"
    now_holder = [_at(8, 0)]  # 08:00, past the 07:00 target
    sched = _build_scheduler(now_holder=now_holder, sink=sink, model_factory=_unreachable_model)
    try:
        parked = await _drive_schedule(sched, "sched-ac1")

        assert parked.status is RunStatus.WAITING  # the timer re-parked after firing
        run_id = _day_keyed_run_id(now_holder[0])
        assert _header_count(sink, run_id) == 1  # exactly ONE brief delivered
        assert sched.brief_run_ledger.ran_today() is True
        assert sched.daily_ledger.current_row("brief:run").value == 1
    finally:
        sched.daily_ledger.close()


# --- AC2 (a): parked early, clock jumps past the target, ONE Sweeper.tick fires it --------------


async def test_ac2_sleep_jump_single_tick_catches_the_miss(
    clean_ledger: None, tmp_path: Path
) -> None:
    sink = tmp_path / "brief.txt"
    now_holder = [_at(5, 0)]  # 05:00, BEFORE the 07:00 target
    sched = _build_scheduler(now_holder=now_holder, sink=sink, model_factory=_unreachable_model)
    try:
        parked = await _drive_schedule(sched, "sched-ac2a")
        assert parked.status is RunStatus.WAITING
        run_id = _day_keyed_run_id(_at(9, 0))
        assert _header_count(sink, run_id) == 0  # nothing fired yet (early)
        assert sched.daily_ledger.current_row("brief:run").value == 0

        # The laptop slept across the target; the clock jumps to 09:00 and ONE Sweeper.tick claims
        # the now-overdue Wait and re-drives the parked run through fire_timer.
        now_holder[0] = _at(9, 0)
        sweeper = Sweeper(
            journal=sched.journal, fire=sched.engine.fire_timer, clock=lambda: now_holder[0]
        )
        fired = await sweeper.tick(now_holder[0], lease_ttl=timedelta(seconds=60))

        assert fired == 1  # exactly the one overdue timer
        assert _header_count(sink, run_id) == 1  # fired exactly once on the catch
        assert sched.daily_ledger.current_row("brief:run").value == 1
    finally:
        sched.daily_ledger.close()


# --- AC2 (b): a cold boot LATE in the morning fires the missed brief immediately ----------------


async def test_ac2_cold_boot_late_morning_fires_immediately(
    clean_ledger: None, tmp_path: Path
) -> None:
    sink = tmp_path / "brief.txt"
    now_holder = [_at(9, 30)]  # boot at 09:30, well past 07:00, brief never ran today
    sched = _build_scheduler(now_holder=now_holder, sink=sink, model_factory=_unreachable_model)
    try:
        parked = await _drive_schedule(sched, "sched-ac2b")

        assert parked.status is RunStatus.WAITING
        run_id = _day_keyed_run_id(now_holder[0])
        assert _header_count(sink, run_id) == 1  # the missed brief fired on cold boot
        assert sched.daily_ledger.current_row("brief:run").value == 1
    finally:
        sched.daily_ledger.close()


# --- AC3: a second pass the same day is SKIPPED (DEBUG log), no second brief --------------------


async def test_ac3_second_pass_same_day_is_skipped_no_second_brief(
    clean_ledger: None, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    sink = tmp_path / "brief.txt"
    now_holder = [_at(10, 0)]  # a crash-restart boot LATER the same day
    sched = _build_scheduler(now_holder=now_holder, sink=sink, model_factory=_unreachable_model)
    try:
        # The brief already ran earlier today (the durable pg ledger survived the restart).
        sched.brief_run_ledger.mark_ran()
        assert sched.daily_ledger.current_row("brief:run").value == 1

        with caplog.at_level(logging.DEBUG, logger="wombat.stages.brief_timer_stage"):
            parked = await _drive_schedule(sched, "sched-ac3")

        assert parked.status is RunStatus.WAITING  # still re-parks for tomorrow
        assert _header_count(sink, _day_keyed_run_id(now_holder[0])) == 0  # NO second brief
        assert sched.daily_ledger.current_row("brief:run").value == 1  # ledger unchanged
        assert any(
            record.levelno == logging.DEBUG and "already ran" in record.message.lower()
            for record in caplog.records
        )
    finally:
        sched.daily_ledger.close()


# --- AC4 (a): crash-before-Deliver (a raising fire, unmarked) -> next pass delivers exactly one --


async def test_ac4_crash_before_deliver_unmarked_then_refire_delivers_one(
    clean_ledger: None, tmp_path: Path
) -> None:
    sink = tmp_path / "brief.txt"
    now_holder = [_at(8, 0)]
    sched = _build_scheduler(now_holder=now_holder, sink=sink, model_factory=_unreachable_model)
    run_id = _day_keyed_run_id(now_holder[0])
    try:
        # A crash before delivery: fire_brief raises, so nothing is delivered and the day stays
        # unmarked (the timer swallows the crash, logs loud, re-parks).
        async def _crashing_fire(now: datetime) -> RunState:
            raise ConnectionError("crash before the brief reached deliver")

        crashing_timer = BriefTimerStage(
            fire_brief=_crashing_fire,
            ran_today=sched.brief_run_ledger.ran_today,
            mark_ran=sched.brief_run_ledger.mark_ran,
            tz=_TZ,
            brief_time=_SEVEN_AM,
        )
        result = await crashing_timer.run(StageContextFake(now_fn=lambda: now_holder[0]))
        assert isinstance(result, Wait)
        assert result.to == "brief_timer"  # re-parked, did not raise
        assert _header_count(sink, run_id) == 0  # nothing delivered
        assert sched.daily_ledger.current_row("brief:run").value == 0  # unmarked

        # The next pass (the real timer) re-fires and delivers exactly ONE brief, then marks.
        recovered = await sched.timer_stage.run(StageContextFake(now_fn=lambda: now_holder[0]))
        assert isinstance(recovered, Wait)
        assert recovered.to == "brief_timer"
        assert _header_count(sink, run_id) == 1  # exactly one brief across the crash + recovery
        assert sched.daily_ledger.current_row("brief:run").value == 1
    finally:
        sched.daily_ledger.close()


# --- AC4 (b): crash-after-append-before-mark -> same run_id hits the file marker (replay) --------


async def test_ac4_crash_after_append_before_mark_refire_is_replay_no_double(
    clean_ledger: None, tmp_path: Path
) -> None:
    sink = tmp_path / "brief.txt"
    now_holder = [_at(8, 0)]
    run_id = _day_keyed_run_id(now_holder[0])

    # A prior attempt appended the brief (its [run=...] header is in the sink) but crashed BEFORE
    # committing Done / marking the day — modelled by pre-seeding the sink with that exact marker
    # and starting from a FRESH journal + an unmarked ledger (a cold-boot restart).
    sink.write_text(
        f"[run={run_id}] delivered_at=2026-07-03T08:00:00-05:00\nMorning brief.\n\n",
        encoding="utf-8",
    )
    assert _header_count(sink, run_id) == 1

    sched = _build_scheduler(now_holder=now_holder, sink=sink, model_factory=_unreachable_model)
    try:
        assert sched.daily_ledger.current_row("brief:run").value == 0  # unmarked

        # The real timer re-fires: fire_brief drives wombat.brief under the SAME day-keyed run_id,
        # the deliver stage finds the pre-seeded marker -> replay=True, no second append -> the run
        # COMPLETES -> the timer marks the day.
        result = await sched.timer_stage.run(StageContextFake(now_fn=lambda: now_holder[0]))
        assert isinstance(result, Wait)
        assert result.to == "brief_timer"

        assert _header_count(sink, run_id) == 1  # NO second append (the marker absorbed the refire)
        assert sched.daily_ledger.current_row("brief:run").value == 1  # the mark landed

        # The brief run's deliver step reports replay=True (the file-marker exactly-once path).
        state = await sched.journal.load_run(run_id)
        assert state is not None
        assert state.status is RunStatus.COMPLETED
        deliver_step = next(s for s in state.steps if s.stage_name == "brief_deliver")
        assert deliver_step.result.output is not None
        _delivered_at, _voice, replay = brief_delivered_from_artifact_data(
            deliver_step.result.output.data
        )
        assert replay is True
    finally:
        sched.daily_ledger.close()
