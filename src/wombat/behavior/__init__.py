"""wombat.behavior — the append-only behavioral event log (TK-111, EP-21, Q-98).

A durable, motive-free corpus the nightly dream pass (``DreamBehaviorLogStage``, wired into
``pathways/dream_pathway.py``) writes so TK-112's productivity-window detector and EP-14's
``RatingTuner`` have accumulating signal from the FIRST live week. No hot-path writer, no
dashboard/analytics reader (NG-3) — the only ``src/wombat`` importers of
``wombat.behavior.event_log`` are ``pathways/dream_pathway.py`` and ``bootstrap.py`` (AC4).
"""

from __future__ import annotations
