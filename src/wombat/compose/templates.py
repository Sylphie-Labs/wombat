"""TemplateComposer — the deterministic terse-line degrade fallback (TK-8, Q-50).

Pure: no model, no I/O, no clock. Renders the SAME payload fields the ``ComposeStage`` prompt
uses (``format_payload_fields``), so the model path and the degrade path speak from one source
of user-facing content — never gate/queue internals, which never reach this module either.
"""

from __future__ import annotations

from typing import Any

from wombat.gate.models import ItemKind


def format_payload_fields(payload: dict[str, Any]) -> str:
    """Render a payload dict as deterministic ``key: value`` text, sorted by key.

    Sorted (rather than insertion order) so the SAME payload always renders identically
    regardless of how it was constructed or round-tripped (dict/JSON preserve insertion order,
    but sorting removes even that degree of freedom — pure/deterministic per TK-8's ruling).
    """
    if not payload:
        return "(no content)"
    return "; ".join(f"{key}: {value}" for key, value in sorted(payload.items()))


class TemplateComposer:
    """Deterministic terse-line renderer — the ``ComposeStage`` degrade fallback (AC2)."""

    def render(self, item_kind: ItemKind, payload: dict[str, Any]) -> str:
        """A terse one-line rendering of ``payload`` for ``item_kind``. Pure — same input,
        same output, every time; no model call, no I/O."""
        return f"[{item_kind.value}] {format_payload_fields(payload)}"


__all__ = ["TemplateComposer", "format_payload_fields"]
