"""wombat.behavior.stages — nightly dream-graph stages over the behavioral event log (TK-112,
EP-21).

``write_window_summaries.WriteWindowSummariesStage`` (Q-99e) is the ``dream_window`` stage wired
into ``pathways/dream_pathway.py``, between ``dream_behavior_log`` and the ``dream_run`` terminal.
"""

from __future__ import annotations
