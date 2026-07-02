"""build_drain_pathway — assembles injected Stages into a cog-worx StageGraph (TK-5, Q-47).

A PARTIAL-pathway builder: TK-5 wires only ``DrainQueueStage``'s own declared transitions. It
ships NO placeholder/sink stage in ``src`` (that would be throwaway production code that TK-7's
real assembly would then have to unwind) — a caller supplies whichever Stages it needs, and a
test-local terminal stage in the test module satisfies ``StageGraph``'s termination-by-construction
requirement until TK-7 performs the full real assembly.
"""

from __future__ import annotations

from cogworx.loop.graph import StageGraph
from cogworx.loop.stage import Stage


def build_drain_pathway(*stages: Stage) -> StageGraph:
    """Assemble ``stages`` into a ``StageGraph``, entered at the first stage passed."""
    return StageGraph(list(stages), entry=stages[0].name)


__all__ = ["build_drain_pathway"]
