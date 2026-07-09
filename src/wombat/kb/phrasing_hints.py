"""extract_phrasing_hints — KB entry to phrase scaffolds for Compose (TK-117, EP-24).

NO model anywhere (NG-4/CON-1): a plain deterministic lookup over already-loaded KB entries. No
I/O, no clock — same inputs always yield the same output. The returned hints are prompt
GUIDANCE for TK-114/TK-118's composer, never output content in their own right (EP-24).

``loader.py`` (TK-115) does not enforce ``pattern_id`` uniqueness, so this module rules
first-match-wins deterministically: the first entry (in ``kb`` order) whose ``pattern_id``
matches is used. The packaged seed KB's ``pattern_id``\\ s are in fact unique today, so this only
matters as a documented tie-break rule.
"""

from __future__ import annotations

from collections.abc import Sequence

from wombat.kb.schema import KBEntry


def extract_phrasing_hints(pattern_id: str, kb: Sequence[KBEntry]) -> list[str]:
    """Return the ``phrasing_hints`` of the first ``kb`` entry whose ``pattern_id`` matches.

    Unknown ``pattern_id`` (no entry matches) returns ``[]`` without raising — TK-114's caller
    then falls back to a safe default prompt. Always returns a NEW list (no shared mutable
    state), with identical contents for identical inputs (pure, deterministic).
    """
    for entry in kb:
        if entry.pattern_id == pattern_id:
            return list(entry.phrasing_hints)
    return []
