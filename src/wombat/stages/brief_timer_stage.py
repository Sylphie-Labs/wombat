"""BriefTimerStage — the durable once-daily brief scheduler stage (TK-97, EP-1, Q-80).

The SINGLE behavioural stage of the ``wombat.brief_schedule`` pathway. Each time it runs (the
boot/crash-miss initial drive, or a ``Sweeper``-fired wake once its ``Wait`` comes due) it decides
whether this wombat-day's brief is owed and, if so, fires it EXACTLY ONCE — then ALWAYS re-parks on
a fresh absolute-``wake_at`` ``Wait`` back onto itself. The engine journals the ``Wait`` (this stage
NEVER touches ``ctx.journal``); the ``Sweeper`` re-drives the parked run when ``wake_at`` passes.

EXACTLY-ONCE FENCE (Q-80): a fire happens only when ``is_due(now) AND NOT ran_today()``. After the
fired brief run reports ``COMPLETED`` the day is marked (``mark_ran``), so a second pass the same
day (a later crash-restart boot drive) reads ``ran_today() -> True``, logs a DEBUG skip (AC3), and
re-parks WITHOUT firing. A fire that RAISES or returns a non-``COMPLETED`` status logs LOUD and does
NOT mark (fails toward silence, never a double-mark) — the unmarked day re-fires on the next pass,
so at most one brief is delivered per day (the delivery side's own day-keyed run-id file marker,
TK-101, absorbs the crash-after-append-before-mark window).

NARROW INJECTED SEAMS (TK-99 precedent): the constructor takes the async ``fire_brief`` closure
(returns the fired run's final ``RunState``), the ``ran_today``/``mark_ran`` callables (the
``BriefRunLedger`` methods), plus ``tz`` + ``brief_time`` for the pure time math — never the whole
ledger or engine. NO model call here. Touches ONLY ``ctx.clock()`` on the ``StageContext``.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, time
from zoneinfo import ZoneInfo

from cogworx.loop.result import StageResult, Wait
from cogworx.loop.stage import StageContext
from cogworx.loop.state import RunStatus
from cogworx.substrate.journal import RunState

from wombat.domain.brief_schedule import is_due, next_fire_at
from wombat.pathways.brief_pathway import brief_timer_tick_artifact

logger = logging.getLogger(__name__)

# The narrow async seam this stage injects instead of the whole Engine: fires ``wombat.brief`` once
# for the given ``now`` and returns the fired run's final ``RunState`` (COMPLETED gates the mark).
FireBrief = Callable[[datetime], Awaitable[RunState]]


class BriefTimerStage:
    """Fires the morning brief at most once per wombat-day, then self-parks on a durable Wait."""

    name: str = "brief_timer"
    # The self-edge PLUS a declared-but-NEVER-taken stub edge: ``run()`` always returns
    # Wait(to="brief_timer"), never routing to the stub. The stub only exists so the single-stage
    # scheduler graph satisfies cog-worx's "the graph can end" construction invariant (Q-80 as
    # amended; the TK-53 _WaitForeverStage/_TerminalStage precedent).
    transitions: tuple[str, ...] = ("brief_timer", "brief_timer_terminal")

    def __init__(
        self,
        *,
        fire_brief: FireBrief,
        ran_today: Callable[[], bool],
        mark_ran: Callable[[], int],
        tz: ZoneInfo,
        brief_time: time,
    ) -> None:
        self._fire_brief = fire_brief
        self._ran_today = ran_today
        self._mark_ran = mark_ran
        self._tz = tz
        self._brief_time = brief_time

    async def run(self, ctx: StageContext) -> StageResult:
        now = ctx.clock()

        if self._ran_today():
            # AC3: the brief already fired this wombat-day — skip and re-park (a later same-day pass
            # is a crash-restart boot drive, not a second brief).
            logger.debug(
                "brief_timer: brief already ran this wombat-day; skipping fire and re-parking"
            )
        elif is_due(now, self._tz, self._brief_time):
            await self._fire_once(now)
        # else: it is not yet today's brief_time — nothing owed, just re-park below.

        return Wait(
            to="brief_timer",
            wake_at=next_fire_at(now, self._tz, self._brief_time),
            output=brief_timer_tick_artifact(now),
        )

    async def _fire_once(self, now: datetime) -> None:
        """Fire the brief once; mark the day ONLY on a ``COMPLETED`` run. Never raises (a failed or
        non-COMPLETED fire logs loud and leaves the day unmarked so the next pass re-fires)."""
        try:
            final = await self._fire_brief(now)
        except Exception:
            logger.error(
                "brief_timer: fire_brief raised; leaving the day unmarked so the next pass "
                "re-fires (no brief delivered this pass)",
                exc_info=True,
            )
            return
        if final.status is RunStatus.COMPLETED:
            self._mark_ran()
        else:
            logger.error(
                "brief_timer: fired brief run ended non-COMPLETED (status=%s); leaving the day "
                "unmarked so the next pass re-fires",
                final.status,
            )

__all__ = ["BriefTimerStage", "FireBrief"]
