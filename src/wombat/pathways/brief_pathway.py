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
from cogworx.loop.stage import Stage

BRIEF_PATHWAY_ID = "wombat.brief"

# The seed artifact's kind (mirrors runtime.py's own "drain-tick" heartbeat convention).
BRIEF_TRIGGER_KIND = "wombat.brief_trigger"


def build_brief_pathway(
    gather: Stage, force_flush: Stage, compose: Stage, deliver: Stage
) -> StageGraph:
    """Assemble the four brief stages into a ``StageGraph``, entered at ``gather.name``."""
    return StageGraph([gather, force_flush, compose, deliver], entry=gather.name)


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
    "BRIEF_TRIGGER_KIND",
    "brief_trigger_artifact",
    "build_brief_pathway",
]
