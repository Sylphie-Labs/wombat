"""KBEntry schema for the psychology KB (TK-115, EP-23, Q-99a).

NO pydantic (Q-99a ruling): pydantic is not a declared runtime dependency of this project (the
symbols other modules import — ``wombat.config``, ``wombat.params``, ``wombat.integrations.
gmail.triage`` — ride in transitively via ``cog-worx[providers]``), and the Q-46/Q-72
clean-checkout bar forbids adding a core dependency for one loader. The schema is a plain frozen
dataclass, ``KBEntry``, plus this module-local ``ValidationError`` (a ``ValueError`` subclass —
no pydantic ``ValidationError`` involved).

Both the gate_condition ``metric`` and ``operator`` are drawn from CLOSED vocabularies (Q-99b —
the cross-ticket seam TK-115/TK-116/TK-113 share). ``loader.py`` validates every entry against
these vocabularies at load time, so downstream readers (TK-116's gate-conditioning function) may
assume every ``KBEntry`` it ever sees already satisfies them.
"""

from __future__ import annotations

from dataclasses import dataclass

# Closed v1 metric vocabulary (Q-99b). TK-113 derives EXACTLY these three keys from a day's
# WindowSummaries; TK-116 may assume a gate_condition.metric is always one of these.
METRIC_VOCABULARY = frozenset({"switch_rate", "window_count", "event_count"})

# Closed comparison-operator vocabulary (Q-99b). TK-116 may assume no other operator ever
# appears on a loaded KBEntry.
OPERATOR_VOCABULARY = frozenset({">", ">=", "<", "<=", "=="})


class ValidationError(ValueError):
    """Raised when the psychology KB YAML, or one of its entries, fails schema validation."""


@dataclass(frozen=True, slots=True)
class GateCondition:
    """The single threshold condition a KB entry's pattern warrants a nudge on (TK-116)."""

    metric: str
    operator: str
    threshold: float


@dataclass(frozen=True, slots=True)
class KBEntry:
    """One validated psychology KB entry (TK-115 AC1).

    Never model-authored (TECH-13/CON-6), never clinical (NG-2), never recited to the user
    (EP-24) — see ``psychology_kb.yaml``'s header for the human-authorship rules of the road.
    """

    pattern_id: str
    description: str
    gate_condition: GateCondition
    phrasing_hints: tuple[str, ...]
    autonomy_level: str
    evidence_tag: str
    version: int
