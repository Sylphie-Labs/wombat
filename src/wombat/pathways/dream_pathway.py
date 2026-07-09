"""build_dream_pathway — the wombat.dream pathway SCAFFOLD (TK-46, Q-33/Q-85, DEC-23).

MIRRORS ``brief_pathway.py`` exactly: pure graph assembly, no bootstrap import (avoids an import
cycle — ``bootstrap.py`` imports this module, not the reverse). A no-op, off-path (S1/S11)
consolidation run: ONE terminal stage, no tuner, no reconciler/extractor (TK-47), no recurrence/
fence (TK-52), no model call. Those land on later tickets once TK-150 (residency predicate)
unblocks the reconciler/extractor cluster (Q-33).
"""

from __future__ import annotations

from datetime import datetime

from cogworx.claims.provenance import Artifact, Provenance
from cogworx.loop.graph import StageGraph
from cogworx.loop.result import Done, StageResult
from cogworx.loop.stage import Stage, StageContext

DREAM_PATHWAY_ID = "wombat.dream"

# The seed artifact's kind (mirrors brief_pathway.py's own BRIEF_TRIGGER_KIND convention).
DREAM_TRIGGER_KIND = "wombat.dream_trigger"

# DreamScaffoldStage's committed output kind — a contentless, provenance-bearing proof that the
# off-path run happened, nothing more (no tuner/reconciler/extractor payload, TK-47).
DREAM_REPORT_KIND = "wombat.dream_report"


class DreamScaffoldStage:
    """The terminal, no-op ``wombat.dream`` stage (TK-46 scaffold). It IS the reachable terminal
    (``transitions=()``) — unlike the Q-80 timer graphs, no separate stub stage is needed here.

    Does NOT call the model and NEVER touches ``ctx.journal`` directly (DEC-12/DEC-23 — model
    inference is admitted only in TK-47's later sweepers, not this scaffold). Provenance is
    ``source="system"`` (the as-built control-plane convention, mirrors ``brief_trigger_artifact``).
    """

    name: str = "dream_run"
    transitions: tuple[str, ...] = ()

    async def run(self, ctx: StageContext) -> StageResult:
        return Done(
            output=Artifact(
                kind=DREAM_REPORT_KIND,
                produced_by=self.name,
                provenance=Provenance(source="system", confidence=1.0, recorded_at=ctx.clock()),
                data={"changes": 0, "scaffold": True},
            )
        )


def build_dream_pathway(stage: Stage | None = None) -> StageGraph:
    """Assemble the ``wombat.dream`` ``StageGraph``, entered at ``stage.name``.

    ``stage`` defaults to ``DreamScaffoldStage()``; the injectable seam lets a caller (a test)
    substitute a different terminal stage — e.g. an always-raising double — to prove off-path
    error isolation (AC2) without touching the production scaffold.
    """
    dream_stage = stage if stage is not None else DreamScaffoldStage()
    return StageGraph([dream_stage], entry=dream_stage.name)


def dream_trigger_artifact(now: datetime) -> Artifact:
    """The initial drive's input for ``wombat.dream`` — a system-provenanced, contentless trigger
    (mirrors ``brief_trigger_artifact``). ``DreamScaffoldStage`` does not read this artifact's
    ``data``; it only satisfies the engine's ``initial: Artifact`` requirement to start a run.
    """
    return Artifact(
        kind=DREAM_TRIGGER_KIND,
        produced_by="wombat.runtime",
        provenance=Provenance(source="system", confidence=1.0, recorded_at=now),
        data={},
    )


__all__ = [
    "DREAM_PATHWAY_ID",
    "DREAM_REPORT_KIND",
    "DREAM_TRIGGER_KIND",
    "DreamScaffoldStage",
    "build_dream_pathway",
    "dream_trigger_artifact",
]
