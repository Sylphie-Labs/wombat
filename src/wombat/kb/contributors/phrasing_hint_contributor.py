"""PhrasingHintContributor — KB phrasing hints as reflection-prompt slot chunks (TK-118, EP-24).

Implements the cog-worx ``ContextContributor`` Protocol (``cogworx.context.contributor`` /
``cogworx.context.types`` — verified seam, Q-102a ruling binds). Bound to a single
``pattern_id`` at construction time — ``ContextRequest`` is frozen with no ``pattern_id`` field,
so the pattern travels by construction, not by request.

``contribute`` looks up hints via ``extract_phrasing_hints`` (``wombat.kb.phrasing_hints``,
TK-117) and returns one ``SlotChunk`` per hint. Zero model calls, zero I/O, no clock — a pure
wrapper over an already-pure lookup. The contributor NEVER raises (cog-worx S8): an unknown
``pattern_id`` or an empty KB is a legitimate ``status="empty"`` outcome, and any unexpected
exception (e.g. a malformed ``kb`` argument) is caught and reported as ``status="degraded"``.

Scope (EP-24/NG-2/CON-6): this module injects KB-authored guidance text only, never clinical or
motive language of its own, and is never registered globally — TK-114's per-turn reflection
assembler owns registration. Whether hints ever leak verbatim into rendered model output is
proven by TK-114's suite, not here (see ``tests/kb/test_phrasing_hint_contributor.py``).
"""

from __future__ import annotations

from collections.abc import Sequence

from cogworx.context.types import ContextRequest, SlotAllocation, SlotChunk, SlotContent

from wombat.kb.phrasing_hints import extract_phrasing_hints
from wombat.kb.schema import KBEntry

__all__ = ["PhrasingHintContributor"]


class PhrasingHintContributor:
    """Renders ``pattern_id``'s KB phrasing hints as ``reflection_hints`` slot chunks.

    Args:
        pattern_id: The pattern this contributor renders hints for (bound at construction).
        kb:         The loaded KB entries to search (typically ``load_psychology_kb()``).
    """

    def __init__(self, pattern_id: str, kb: Sequence[KBEntry]) -> None:
        self._pattern_id = pattern_id
        self._kb = kb

    async def contribute(
        self,
        request: ContextRequest,
        allocation: SlotAllocation,
    ) -> SlotContent:
        """Return one ``SlotChunk`` per phrasing hint for this contributor's ``pattern_id``.

        ``request``/``allocation`` are accepted (Protocol conformance) but unused — hints are
        tiny and carry no truncation logic (mvp). An unknown ``pattern_id`` or empty KB yields
        ``status="empty"`` with zero chunks; any unexpected exception is caught and reported as
        ``status="degraded"`` — this method never raises (cog-worx S8).
        """
        try:
            hints = extract_phrasing_hints(self._pattern_id, self._kb)
        except Exception as exc:  # contributor contract: never raise (S8)
            return SlotContent(status="degraded", detail=f"{type(exc).__name__}: {exc}")

        if not hints:
            return SlotContent(status="empty")

        chunks = tuple(
            SlotChunk(text=hint, key=f"reflection_hints:{i}", source_slot="reflection_hints")
            for i, hint in enumerate(hints)
        )
        return SlotContent(chunks=chunks, status="ok")
