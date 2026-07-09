"""wombat.dream_schedule — the durable nightly dream trigger + once-per-night fence (TK-52, EP-13,
Q-85).

Q-85 RULING (binds): TK-52's contract prose ("self-rescheduling Wait" / "start_run idempotent")
predates the as-built TK-97 scheduler precedent — cog-worx has NO cron primitive and NO
``start_run``. This module reproduces the PROVEN TK-97 shape VERBATIM for the dream pathway:

  * ``DreamRunLedger`` — the once-per-wombat-night fence, a THIN wrapper over the SAME composed
    ``DailyLedger`` the brief/spend/ceiling ledgers ride (DEC-21 boundary), under the fixed row
    name ``"dream:run"`` (mirrors ``BriefRunLedger``'s ``"brief:run"`` exactly — see
    ``domain/brief_schedule.py``). It does NOT construct a second ``DailyLedger``; the caller
    (``bootstrap.assemble_runtime``) hands in the bundle's ONE shared instance (the TK-173/CR-15
    lesson).

  * ``DreamTimerStage`` — the SINGLE behavioural stage of the ``wombat.dream_schedule`` pathway.
    Each drive (the boot/crash-miss initial drive, or a ``Sweeper``-fired wake) decides whether
    tonight's dream run is owed and, if so, fires it EXACTLY ONCE, then ALWAYS re-parks on a
    fresh absolute-``wake_at`` ``Wait`` back onto itself — regardless of whether the fire
    succeeded, failed, or was skipped (AC1's "re-armed regardless of outcome", verbatim). The
    engine journals the ``Wait`` (this stage NEVER touches ``ctx.journal``); the ``Sweeper``
    re-drives the parked run once ``wake_at`` passes (AC3's sleep-catch). Never calls the model.

  * ``DreamTimerTerminalStage`` + ``build_dream_schedule_pathway`` — a never-reached raising
    terminal stub closes the two-stage ``StageGraph`` (``dream_timer`` -> ``dream_timer_
    terminal``), satisfying cog-worx's "the graph can end" construction invariant exactly as
    ``BriefTimerTerminalStage``/``build_brief_schedule_pathway`` do (the Q-80-as-amended shape).

TIME MATH: ``is_due``/``next_fire_at`` are REUSED from ``domain/brief_schedule.py`` as-is — their
signatures (``now, tz, brief_time``) already take the fire-time as a parameter, so they are pure
generic tz-aware fire-instant math with no brief-specific behaviour; reproducing them here would
be an unrequested fork of proven code (the briefing's own reuse-if-generic instruction).

FENCE + RUN-KEY: ``run_id = f"wombat-dream-{wombat_date}"`` (night-keyed) is the caller
(``bootstrap.fire_dream``)'s concern, not this module's — mirrors ``fire_brief``'s day-keyed
``run_id`` living in ``bootstrap.py``, not ``brief_timer_stage.py``. The Engine's own run_id
double-drive guard (verified as-built at TK-53) is the second idempotency layer the contract's
"start_run idempotent" prose means (Q-85).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, time
from zoneinfo import ZoneInfo

from cogworx.claims.provenance import Artifact, Provenance
from cogworx.loop.graph import StageGraph
from cogworx.loop.result import StageResult, Wait
from cogworx.loop.stage import Stage, StageContext
from cogworx.loop.state import RunStatus
from cogworx.substrate.journal import RunState

from wombat.domain.brief_schedule import is_due, next_fire_at
from wombat.domain.daily_ledger import DailyLedger

logger = logging.getLogger(__name__)

DREAM_SCHEDULE_PATHWAY_ID = "wombat.dream_schedule"

# Mirrors BriefRunLedger.LEDGER_NAME ("brief:run") — a distinct row on the SAME daily_ledger
# table (no new table, no new migration); the composite (ledger_name, wombat_date) PK means the
# two fences never collide.
LEDGER_NAME = "dream:run"

# The dream timer's self-park heartbeat kind (mirrors BRIEF_TIMER_TICK_KIND).
DREAM_TIMER_TICK_KIND = "wombat.dream_timer_tick"

# The narrow async seam this stage injects instead of the whole Engine: fires ``wombat.dream``
# once for the given ``now`` and returns the fired run's final ``RunState`` (COMPLETED gates the
# mark) — mirrors ``brief_timer_stage.FireBrief`` exactly.
FireDream = Callable[[datetime], Awaitable[RunState]]


class DreamRunLedger:
    """The once-per-wombat-night dream fence, riding the shared ``DailyLedger`` (mirrors
    ``BriefRunLedger`` verbatim, under the ``"dream:run"`` row instead of ``"brief:run"``)."""

    def __init__(self, ledger: DailyLedger) -> None:
        self._ledger = ledger

    def ran_tonight(self) -> bool:
        """True iff the dream run has already fired this wombat-night (today's ``dream:run``
        row >= 1). Creates today's row at ``0`` if it doesn't exist yet (``current_row``
        semantics) — a fresh wombat-day is a distinct ``(ledger_name, wombat_date)`` key, so this
        is ``False`` again the night after a fire with no reset logic here (TK-28 precedent).
        """
        return self._ledger.current_row(LEDGER_NAME).value >= 1

    def mark_ran(self) -> int:
        """Record that the dream run fired tonight; returns the post-increment count."""
        return self._ledger.increment(LEDGER_NAME).value


def dream_timer_tick_artifact(now: datetime) -> Artifact:
    """The ``dream_timer`` stage's self-park ``Wait.output`` (and the schedule pathway's initial
    drive input) — a system-provenanced, contentless heartbeat (mirrors
    ``brief_timer_tick_artifact``). ``DreamTimerStage`` does not read this artifact's ``data``; it
    only satisfies the ``Wait``/``initial`` Artifact requirement.
    """
    return Artifact(
        kind=DREAM_TIMER_TICK_KIND,
        produced_by="dream_timer",
        provenance=Provenance(source="system", confidence=1.0, recorded_at=now),
        data={},
    )


class DreamTimerStage:
    """Fires the nightly dream run at most once per wombat-night, then self-parks on a durable
    Wait (mirrors ``BriefTimerStage`` verbatim — see that module's docstring for the full
    exactly-once-fence rationale, reproduced here for the dream pathway per Q-85)."""

    name: str = "dream_timer"
    # The self-edge PLUS a declared-but-NEVER-taken stub edge: ``run()`` always returns
    # Wait(to="dream_timer"), never routing to the stub — mirrors BriefTimerStage.transitions.
    transitions: tuple[str, ...] = ("dream_timer", "dream_timer_terminal")

    def __init__(
        self,
        *,
        fire_dream: FireDream,
        ran_tonight: Callable[[], bool],
        mark_ran: Callable[[], int],
        tz: ZoneInfo,
        dream_time: time,
    ) -> None:
        self._fire_dream = fire_dream
        self._ran_tonight = ran_tonight
        self._mark_ran = mark_ran
        self._tz = tz
        self._dream_time = dream_time

    async def run(self, ctx: StageContext) -> StageResult:
        now = ctx.clock()

        if self._ran_tonight():
            # AC (mirrors AC3 of TK-97): tonight's dream run already fired — skip and re-park (a
            # later same-night pass is a crash-restart boot drive, not a second dream run).
            logger.debug(
                "dream_timer: dream already ran this wombat-night; skipping fire and re-parking"
            )
        elif is_due(now, self._tz, self._dream_time):
            await self._fire_once(now)
        # else: it is not yet tonight's dream_time — nothing owed, just re-park below.

        return Wait(
            to="dream_timer",
            wake_at=next_fire_at(now, self._tz, self._dream_time),
            output=dream_timer_tick_artifact(now),
        )

    async def _fire_once(self, now: datetime) -> None:
        """Fire the dream run once; mark the night ONLY on a ``COMPLETED`` run. Never raises (a
        failed or non-``COMPLETED`` fire logs loud and leaves the night unmarked so the next pass
        re-fires) — this is what makes the re-arm unconditional (AC1)."""
        try:
            final = await self._fire_dream(now)
        except Exception:
            logger.error(
                "dream_timer: fire_dream raised; leaving the night unmarked so the next pass "
                "re-fires (no dream run completed this pass)",
                exc_info=True,
            )
            return
        if final.status is RunStatus.COMPLETED:
            self._mark_ran()
        else:
            logger.error(
                "dream_timer: fired dream run ended non-COMPLETED (status=%s); leaving the "
                "night unmarked so the next pass re-fires",
                final.status,
            )


class DreamTimerTerminalStage:
    """A never-reached terminal stub that exists ONLY to satisfy cog-worx's structural invariant
    that every ``StageGraph`` has a reachable terminal stage (mirrors ``BriefTimerTerminalStage``
    verbatim, Q-80 as amended / Q-85).

    ``dream_timer``'s ``run()`` ALWAYS returns ``Wait(to="dream_timer", ...)`` — it never routes
    to this stub — so this graph loops forever at runtime exactly like a true eternal self-park;
    the stub's declared edge (``dream_timer -> dream_timer_terminal``) is a purely STRUCTURAL edge
    that closes the graph. Entering it is a wiring bug, so it raises.
    """

    name: str = "dream_timer_terminal"
    transitions: tuple[str, ...] = ()

    async def run(self, ctx: StageContext) -> StageResult:  # pragma: no cover - never reached
        msg = "dream_timer_terminal must never be entered; dream_timer always re-parks on a Wait"
        raise RuntimeError(msg)


def build_dream_schedule_pathway(timer_stage: Stage) -> StageGraph:
    """Assemble the once-nightly scheduler ``StageGraph`` (TK-52, Q-85 — mirrors
    ``build_brief_schedule_pathway`` verbatim).

    Two stages internally: the caller-supplied ``timer_stage`` (entry; self-parks on a ``Wait``
    forever) plus a ``DreamTimerTerminalStage`` stub that is declared-but-never-taken. The stub
    satisfies cog-worx's "the graph can end" construction invariant without changing runtime
    behaviour — the timer never routes to it.
    """
    return StageGraph([timer_stage, DreamTimerTerminalStage()], entry=timer_stage.name)


__all__ = [
    "DREAM_SCHEDULE_PATHWAY_ID",
    "DREAM_TIMER_TICK_KIND",
    "LEDGER_NAME",
    "DreamRunLedger",
    "DreamTimerStage",
    "DreamTimerTerminalStage",
    "FireDream",
    "build_dream_schedule_pathway",
    "dream_timer_tick_artifact",
]
