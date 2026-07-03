"""build_brief_pathway — assembles the four already-built brief Stages into a cog-worx
StageGraph (TK-96).

MIRRORS ``drain_pathway.py`` exactly: pure graph assembly, no bootstrap import (avoids an import
cycle — ``bootstrap.py`` imports this module, not the reverse). A caller (``assemble_runtime``)
supplies the four brief stages (``brief_gather`` -> ``brief_force_flush`` -> ``brief_compose`` ->
``brief_deliver``, terminal); their transitions are already frozen on the stage classes
themselves, so this module does no routing decisions of its own.
"""

from __future__ import annotations

from datetime import datetime

from cogworx.claims.provenance import Artifact, Provenance
from cogworx.loop.graph import StageGraph
from cogworx.loop.result import StageResult
from cogworx.loop.stage import Stage, StageContext

BRIEF_PATHWAY_ID = "wombat.brief"

# TK-97: the once-daily scheduler pathway id — a SINGLE-behavioural-stage graph (the ``brief_timer``
# self-parking Wait stage plus a never-reached terminal stub, see below) that drives
# ``wombat.brief`` once each morning via the injected ``fire_brief`` closure.
BRIEF_SCHEDULE_PATHWAY_ID = "wombat.brief_schedule"

# The seed artifact's kind (mirrors runtime.py's own "drain-tick" heartbeat convention).
BRIEF_TRIGGER_KIND = "wombat.brief_trigger"

# TK-97: the ``brief_timer`` stage's self-park heartbeat kind (mirrors ``DRAIN_HEARTBEAT``).
BRIEF_TIMER_TICK_KIND = "wombat.brief_timer_tick"


def build_brief_pathway(
    gather: Stage, force_flush: Stage, compose: Stage, deliver: Stage
) -> StageGraph:
    """Assemble the four brief stages into a ``StageGraph``, entered at ``gather.name``."""
    return StageGraph([gather, force_flush, compose, deliver], entry=gather.name)


def brief_timer_tick_artifact(now: datetime) -> Artifact:
    """The ``brief_timer`` stage's self-park ``Wait.output`` (and the schedule pathway's initial
    drive input) — a system-provenanced, contentless heartbeat (mirrors ``brief_trigger_artifact``
    / ``DRAIN_HEARTBEAT``). ``BriefTimerStage`` does not read this artifact's ``data``; it only
    satisfies the ``Wait``/``initial`` Artifact requirement.
    """
    return Artifact(
        kind=BRIEF_TIMER_TICK_KIND,
        produced_by="brief_timer",
        provenance=Provenance(source="system", confidence=1.0, recorded_at=now),
        data={},
    )


class BriefTimerTerminalStage:
    """A never-reached terminal stub that exists ONLY to satisfy cog-worx's structural invariant
    that every ``StageGraph`` has a reachable terminal stage (Q-80 as amended).

    The ``brief_timer`` stage's ``run()`` ALWAYS returns ``Wait(to="brief_timer", ...)`` — it never
    routes to this stub — so this graph loops forever at runtime exactly like a true eternal
    self-park; the stub's declared edge (``brief_timer -> brief_timer_terminal``) is a purely
    STRUCTURAL edge that closes the graph. Mirrors the TK-53
    ``_WaitForeverStage``/``_TerminalStage`` precedent. Entering it is a wiring bug, so it raises.
    """

    name: str = "brief_timer_terminal"
    transitions: tuple[str, ...] = ()

    async def run(self, ctx: StageContext) -> StageResult:  # pragma: no cover - never reached
        msg = "brief_timer_terminal must never be entered; brief_timer always re-parks on a Wait"
        raise RuntimeError(msg)


def build_brief_schedule_pathway(timer_stage: Stage) -> StageGraph:
    """Assemble the once-daily scheduler ``StageGraph`` (TK-97, Q-80 as amended).

    Two stages internally: the caller-supplied ``timer_stage`` (entry; self-parks on a ``Wait``
    forever) plus a ``BriefTimerTerminalStage`` stub that is declared-but-never-taken. The stub
    satisfies cog-worx's "the graph can end" construction invariant (a single self-only-edge stage
    raises ``StageGraphError``) without changing runtime behaviour — the timer never routes to it.
    """
    return StageGraph([timer_stage, BriefTimerTerminalStage()], entry=timer_stage.name)


def brief_trigger_artifact(now: datetime) -> Artifact:
    """The initial drive's input for ``wombat.brief`` — a system-provenanced, contentless
    trigger (mirrors ``wombat.runtime``'s own drain-tick heartbeat convention).

    ``BriefGatherStage`` does NOT read this artifact's ``data`` — it gathers via its own
    injected ``fetch_calendar``/``fetch_gmail`` callables and ``ctx.clock()``; this artifact only
    satisfies the engine's ``initial: Artifact`` requirement to start a run.
    """
    return Artifact(
        kind=BRIEF_TRIGGER_KIND,
        produced_by="wombat.runtime",
        provenance=Provenance(source="system", confidence=1.0, recorded_at=now),
        data={},
    )


__all__ = [
    "BRIEF_PATHWAY_ID",
    "BRIEF_SCHEDULE_PATHWAY_ID",
    "BRIEF_TIMER_TICK_KIND",
    "BRIEF_TRIGGER_KIND",
    "BriefTimerTerminalStage",
    "brief_timer_tick_artifact",
    "brief_trigger_artifact",
    "build_brief_pathway",
    "build_brief_schedule_pathway",
]
