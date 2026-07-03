"""TK-97 — spy-seam tests for ``BriefTimerStage`` (EP-1, Q-80). No DB, no engine, no model.

Drives ``stage.run(ctx)`` directly over a ``StageContextFake`` (an injected clock; every other ctx
member — crucially ``ctx.journal`` — raises if touched, so a passing run is a runnable proof the
stage never touches the journal). ``fire_brief``/``ran_today``/``mark_ran`` are spy callables.
"""

from __future__ import annotations

import logging
from datetime import datetime, time
from zoneinfo import ZoneInfo

import pytest
from cogworx.loop.result import Wait
from cogworx.loop.state import RunStatus
from cogworx.substrate.journal import RunState

from tests.support.stage_context_fake import StageContextFake
from wombat.domain.brief_schedule import next_fire_at
from wombat.pathways.brief_pathway import BRIEF_TIMER_TICK_KIND
from wombat.stages.brief_timer_stage import BriefTimerStage

_CHI = ZoneInfo("America/Chicago")
_SEVEN_AM = time(7, 0)
_NOW_DUE = datetime(2026, 7, 3, 8, 0, tzinfo=_CHI)  # past 07:00 -> due
_NOW_EARLY = datetime(2026, 7, 3, 6, 0, tzinfo=_CHI)  # before 07:00 -> not due


def _run_state(status: RunStatus) -> RunState:
    return RunState(
        run_id="wombat-brief-2026-07-03",
        session_id="wombat-brief-2026-07-03",
        status=status,
        pathway_id="wombat.brief",
        pathway_version=1,
        pathway_fingerprint="fp",
    )


class _FireSpy:
    """Records each ``fire_brief`` call's ``now`` and returns/raises a configured result."""

    def __init__(
        self, *, result: RunState | None = None, raises: BaseException | None = None
    ) -> None:
        self._result = result
        self._raises = raises
        self.calls: list[datetime] = []

    async def __call__(self, now: datetime) -> RunState:
        self.calls.append(now)
        if self._raises is not None:
            raise self._raises
        assert self._result is not None
        return self._result


class _MarkSpy:
    def __init__(self) -> None:
        self.count = 0

    def __call__(self) -> int:
        self.count += 1
        return self.count


def _stage(
    *, fire: _FireSpy, ran_today: bool, mark: _MarkSpy
) -> BriefTimerStage:
    return BriefTimerStage(
        fire_brief=fire,
        ran_today=lambda: ran_today,
        mark_ran=mark,
        tz=_CHI,
        brief_time=_SEVEN_AM,
    )


def _ctx(now: datetime) -> StageContextFake:
    return StageContextFake(now_fn=lambda: now)


# --- fire-when-due: fires once, marks after COMPLETED, re-parks with the right wake_at ----------


async def test_fires_when_due_marks_after_completed_and_reparks() -> None:
    fire = _FireSpy(result=_run_state(RunStatus.COMPLETED))
    mark = _MarkSpy()
    stage = _stage(fire=fire, ran_today=False, mark=mark)

    result = await stage.run(_ctx(_NOW_DUE))

    assert fire.calls == [_NOW_DUE]  # fired exactly once, with ctx.clock()'s now
    assert mark.count == 1  # marked AFTER a COMPLETED run
    assert isinstance(result, Wait)
    assert result.to == "brief_timer"  # self-park, never the terminal stub
    assert result.wake_at == next_fire_at(_NOW_DUE, _CHI, _SEVEN_AM)  # tomorrow 07:00
    assert result.output.kind == BRIEF_TIMER_TICK_KIND


# --- park-when-early: does NOT fire, re-parks at today's 07:00 ----------------------------------


async def test_parks_when_early_without_firing() -> None:
    fire = _FireSpy(result=_run_state(RunStatus.COMPLETED))
    mark = _MarkSpy()
    stage = _stage(fire=fire, ran_today=False, mark=mark)

    result = await stage.run(_ctx(_NOW_EARLY))

    assert fire.calls == []  # not due yet — never fired
    assert mark.count == 0
    assert isinstance(result, Wait)
    assert result.wake_at == next_fire_at(_NOW_EARLY, _CHI, _SEVEN_AM)  # today 07:00
    assert result.wake_at.astimezone(_CHI) == datetime(2026, 7, 3, 7, 0, tzinfo=_CHI)


# --- AC3: already ran today -> DEBUG skip, no fire, still re-parks ------------------------------


async def test_already_ran_today_skips_with_debug_log_and_reparks(
    caplog: pytest.LogCaptureFixture,
) -> None:
    fire = _FireSpy(result=_run_state(RunStatus.COMPLETED))
    mark = _MarkSpy()
    stage = _stage(fire=fire, ran_today=True, mark=mark)  # already fired today

    with caplog.at_level(logging.DEBUG, logger="wombat.stages.brief_timer_stage"):
        result = await stage.run(_ctx(_NOW_DUE))

    assert fire.calls == []  # AC3: no second brief today
    assert mark.count == 0
    assert isinstance(result, Wait)
    assert result.wake_at == next_fire_at(_NOW_DUE, _CHI, _SEVEN_AM)  # still re-parks (tomorrow)
    assert any(
        record.levelno == logging.DEBUG and "already ran" in record.message.lower()
        for record in caplog.records
    )


# --- a raising fire: loud log, NO mark, NO raise, still re-parks --------------------------------


async def test_raising_fire_does_not_mark_does_not_raise_still_reparks(
    caplog: pytest.LogCaptureFixture,
) -> None:
    fire = _FireSpy(raises=ConnectionError("simulated brief-run crash"))
    mark = _MarkSpy()
    stage = _stage(fire=fire, ran_today=False, mark=mark)

    with caplog.at_level(logging.ERROR, logger="wombat.stages.brief_timer_stage"):
        result = await stage.run(_ctx(_NOW_DUE))  # must NOT raise

    assert fire.calls == [_NOW_DUE]
    assert mark.count == 0  # unmarked -> the next pass re-fires
    assert isinstance(result, Wait)
    assert result.wake_at == next_fire_at(_NOW_DUE, _CHI, _SEVEN_AM)
    assert any(record.levelno == logging.ERROR for record in caplog.records)


# --- a non-COMPLETED fire: NO mark, NO raise, still re-parks ------------------------------------


async def test_non_completed_fire_does_not_mark_still_reparks(
    caplog: pytest.LogCaptureFixture,
) -> None:
    fire = _FireSpy(result=_run_state(RunStatus.FAILED))
    mark = _MarkSpy()
    stage = _stage(fire=fire, ran_today=False, mark=mark)

    with caplog.at_level(logging.ERROR, logger="wombat.stages.brief_timer_stage"):
        result = await stage.run(_ctx(_NOW_DUE))

    assert fire.calls == [_NOW_DUE]
    assert mark.count == 0  # non-COMPLETED -> unmarked, next pass re-fires
    assert isinstance(result, Wait)
    assert any(record.levelno == logging.ERROR for record in caplog.records)


# --- load-bearing: the stage NEVER touches ctx.journal -----------------------------------------


async def test_never_touches_ctx_journal() -> None:
    """``StageContextFake.journal`` raises ``NotImplementedError`` if accessed; a clean run over it
    (through the firing path, the busiest branch) proves the stage touches only ``ctx.clock()``."""
    fire = _FireSpy(result=_run_state(RunStatus.COMPLETED))
    mark = _MarkSpy()
    stage = _stage(fire=fire, ran_today=False, mark=mark)
    ctx = _ctx(_NOW_DUE)

    result = await stage.run(ctx)  # would raise if the stage reached for ctx.journal

    assert isinstance(result, Wait)
    with pytest.raises(NotImplementedError):
        _ = ctx.journal  # documents the fake genuinely guards the journal seam
