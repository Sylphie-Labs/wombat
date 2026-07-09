"""wombat.kb.contributors — ContextContributor implementations backed by the psychology KB.

Scoped narrowly: these contributors are registered LOCALLY per assembly (TK-114 owns the
reflection-turn assembler that does the registering), never globally on any shared assembler
(EP-24, Q-102a). See ``phrasing_hint_contributor.py`` (TK-118) for the first implementation.
"""

from __future__ import annotations
