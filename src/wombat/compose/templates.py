"""TemplateComposer — the deterministic terse-line degrade fallback (TK-8, Q-50).

Pure: no model, no I/O, no clock. Renders the SAME payload fields the ``ComposeStage`` prompt
uses (``format_payload_fields``), so the model path and the degrade path speak from one source
of user-facing content — never gate/queue internals, which never reach this module either.

TK-216 (DEC-37(e), Q-107(b)): the deterministic degrade path honors ONLY the two axes a template
can honestly express — ``Brevity`` (wrapper variants) and ``Warmth`` (the brief's greeting line,
``wombat.compose.brief_template``; this module has no warmth-bearing content of its own).
``Directness``/``Humor`` have NO degrade variant BY RULING — a template cannot honestly hedge or
joke — so ``render`` never reads either axis, at any level (pinned by
``tests/persona/test_degrade_variants.py``). An OPTIONAL ``live_persona`` constructor arg reads
the CURRENT matrix fresh on every ``render`` call (hot-apply parity with the four mouth call
sites, TK-209); ``None`` or ``brevity=TERSE`` renders TODAY'S EXACT one-line bytes.
"""

from __future__ import annotations

from typing import Any

from wombat.gate.models import ItemKind
from wombat.persona.live import LivePersona
from wombat.persona.matrix import DEFAULT_MATRIX, Brevity


def format_payload_fields(payload: dict[str, Any]) -> str:
    """Render a payload dict as deterministic ``key: value`` text, sorted by key.

    Sorted (rather than insertion order) so the SAME payload always renders identically
    regardless of how it was constructed or round-tripped (dict/JSON preserve insertion order,
    but sorting removes even that degree of freedom — pure/deterministic per TK-8's ruling).
    """
    if not payload:
        return "(no content)"
    return "; ".join(f"{key}: {value}" for key, value in sorted(payload.items()))


def _sorted_field_lines(payload: dict[str, Any]) -> list[str]:
    """The SAME sorted ``key: value`` pairs as ``format_payload_fields``, one per list entry
    instead of ``"; "``-joined — feeds the BALANCED/EXPANSIVE brevity wrapper variants below."""
    if not payload:
        return ["(no content)"]
    return [f"{key}: {value}" for key, value in sorted(payload.items())]


# TK-216 fixed wrapper-variant strings — module-level so they're pinned by test, not buried
# inline. EXPANSIVE is BALANCED's header + this ONE closing line, appended.
_EXPANSIVE_CLOSING_LINE = "That's everything for this item."


class TemplateComposer:
    """Deterministic terse-line renderer — the ``ComposeStage`` degrade fallback (AC2).

    TK-216: an OPTIONAL ``live_persona`` reads the CURRENT ``PersonaMatrix.brevity`` fresh on
    every ``render`` call. ``None`` (the default) or ``brevity=TERSE`` renders the ORIGINAL
    one-line bytes, byte-identical to every pre-TK-216 caller/test. ``BALANCED`` renders a
    kind-header line followed by one ``key: value`` line per (sorted) payload field. ``EXPANSIVE``
    is the BALANCED layout plus ``_EXPANSIVE_CLOSING_LINE`` appended. ``Directness``/``Humor`` are
    never read here — see the module docstring.
    """

    def __init__(self, live_persona: LivePersona | None = None) -> None:
        self._live_persona = live_persona

    def render(self, item_kind: ItemKind, payload: dict[str, Any]) -> str:
        """A rendering of ``payload`` for ``item_kind`` shaped by the CURRENT brevity level. Pure
        — same input and current matrix, same output, every time; no model call, no I/O."""
        matrix = self._live_persona.matrix if self._live_persona is not None else DEFAULT_MATRIX

        if matrix.brevity is Brevity.TERSE:
            return f"[{item_kind.value}] {format_payload_fields(payload)}"

        lines = [f"[{item_kind.value}]", *_sorted_field_lines(payload)]
        if matrix.brevity is Brevity.EXPANSIVE:
            lines.append(_EXPANSIVE_CLOSING_LINE)
        return "\n".join(lines)


__all__ = ["TemplateComposer", "format_payload_fields"]
