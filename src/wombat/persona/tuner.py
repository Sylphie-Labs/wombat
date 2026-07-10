"""wombat.persona.tuner — TK-214 (EP-35, DEC-36/DEC-37(h), Q-112 pre-ruled): the pure nightly
persona-feedback decision function.

A pure, zero-IO module (mirrors ``wombat.persona.commands``/``wombat.persona.feedback``'s own
posture): no clock, no logging, no store access. It only maps a night's stored feedback phrases
(via ``wombat.persona.feedback.token_for_phrase``) into AT MOST ONE clamped-step decision per
UNPINNED axis.

RULED decision rule (conservative by construction): an axis steps IFF one direction has
``>= PERSONA_STEP_THRESHOLD`` in-window signals AND the opposing direction has ZERO — mixed
signals (both directions present) or a below-threshold count on the only direction present moves
NOTHING. A pinned axis (explicitly set by the user within the last ``PERSONA_PIN_DAYS``, see
``wombat.persona.live``) never steps regardless of its signal.

This module NEVER applies a decided step — that is ``wombat.persona.commands.apply``'s job
(``dream_pathway.DreamPersonaStage`` is the caller that wires the two together). No second
clamp/custody mechanism lives here.
"""

from __future__ import annotations

from collections.abc import Iterable
from collections.abc import Set as AbstractSet
from dataclasses import dataclass

from wombat.persona.feedback import token_for_phrase

# The nightly recall window DreamPersonaStage reads over (structural, mirrors dream_pathway.py's
# own _CLAIMS_LIMIT-style constants) — NOT a tunable.
PERSONA_FEEDBACK_WINDOW_HOURS = 24.0

# An axis steps only once its stepping direction reaches this many in-window signals AND the
# opposing direction has zero (RULED, conservative by construction).
PERSONA_STEP_THRESHOLD = 2


@dataclass(frozen=True, slots=True)
class AxisStepDecision:
    """One axis's decided step (TK-214) — at most one per axis per night. Motive-free (CON-6):
    only the axis, the clamp direction (``+1``/``-1``, ``wombat.persona.commands``' own step
    convention), and the up/down counts that drove it (CON-4: counts only, never a why)."""

    axis: str
    direction: int
    up_count: int
    down_count: int


def decide_persona_steps(
    phrases: Iterable[str], pinned_axes: AbstractSet[str]
) -> tuple[AxisStepDecision, ...]:
    """Decide at most one step per UNPINNED axis from a night's stored feedback phrases.

    Each ``phrase`` is looked up via ``token_for_phrase`` (the exact stored ``outcome_label``
    string a persona-feedback row carries verbatim) — a phrase no longer in the closed lexicon
    (e.g. a stale row from a since-shrunk lexicon version) is silently skipped, never an error.
    An axis named in ``pinned_axes`` is skipped entirely, regardless of its in-window signal.

    Returns decisions in a deterministic (sorted-by-axis) order.
    """
    up_counts: dict[str, int] = {}
    down_counts: dict[str, int] = {}

    for phrase in phrases:
        token = token_for_phrase(phrase)
        if token is None:
            continue
        counts = up_counts if token.direction == "up" else down_counts
        counts[token.axis] = counts.get(token.axis, 0) + 1

    decisions: list[AxisStepDecision] = []
    for axis in sorted(set(up_counts) | set(down_counts)):
        if axis in pinned_axes:
            continue
        up = up_counts.get(axis, 0)
        down = down_counts.get(axis, 0)
        if up >= PERSONA_STEP_THRESHOLD and down == 0:
            decisions.append(
                AxisStepDecision(axis=axis, direction=1, up_count=up, down_count=down)
            )
        elif down >= PERSONA_STEP_THRESHOLD and up == 0:
            decisions.append(
                AxisStepDecision(axis=axis, direction=-1, up_count=up, down_count=down)
            )
        # mixed or below-threshold -> no decision for this axis (conservative by construction)

    return tuple(decisions)


__all__ = [
    "PERSONA_FEEDBACK_WINDOW_HOURS",
    "PERSONA_STEP_THRESHOLD",
    "AxisStepDecision",
    "decide_persona_steps",
]
