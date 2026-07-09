"""wombat.behavior — the append-only behavioral event log (TK-111, EP-21, Q-98).

A durable, motive-free corpus the nightly dream pass (``DreamBehaviorLogStage``, wired into
``pathways/dream_pathway.py``) writes so TK-112's productivity-window detector and EP-14's
``RatingTuner`` have accumulating signal from the FIRST live week. No hot-path writer, no
dashboard/analytics reader (NG-3) — the only ``src/wombat`` importers of
``wombat.behavior.event_log`` are ``pathways/dream_pathway.py``, ``bootstrap.py``, and (TK-112)
``behavior/window_detector.py`` + ``behavior/stages/write_window_summaries.py`` (AC4).

``behavior.window_detector`` (TK-112, Q-99c) is the pure, off-path productivity-window detector
over this same event log; ``behavior.stages.write_window_summaries`` (Q-99e) is the nightly stage
that reads the log, runs the detector, and persists its output.
"""

from __future__ import annotations
