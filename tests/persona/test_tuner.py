"""TK-214 — the pure nightly persona-feedback decision function acceptance criteria (EP-35,
DEC-36/DEC-37(h), Q-112 pre-ruled).

  AC1: two same-direction in-window signals for an axis step it exactly once, clamped direction
      matches the lexicon's declared direction, and the returned counts name the driving tally.
  AC2: mixed signals (1 up + 1 down), a single (below-threshold) signal, and an empty window move
      NOTHING; a property test over random seeded event sets proves never more than one decision
      per axis and never a decision without >=2 strictly-consistent signals (no hypothesis — NG-3,
      mirrors ``tests/persona/test_commands.py``'s own stdlib-random idiom).
  AC3 (custody): a pinned axis never steps despite a clear qualifying signal.
  Also: an unmapped/stale phrase (no longer in the lexicon) is silently skipped, never an error.
"""

from __future__ import annotations

import random

from wombat.persona.feedback import FEEDBACK_LEXICON, token_for_phrase
from wombat.persona.tuner import (
    PERSONA_FEEDBACK_WINDOW_HOURS,
    PERSONA_STEP_THRESHOLD,
    AxisStepDecision,
    decide_persona_steps,
)

# --------------------------------------------------------------------------------------- AC1


def test_module_constants() -> None:
    assert PERSONA_FEEDBACK_WINDOW_HOURS == 24.0
    assert PERSONA_STEP_THRESHOLD == 2


def test_two_same_direction_down_signals_step_the_axis_down() -> None:
    # "too chatty" and "too long" both map to brevity/down — two DISTINCT lexicon phrases, same
    # axis+direction, proving the tally is keyed on (axis, direction), not the literal phrase.
    decisions = decide_persona_steps(["too chatty", "too long"], pinned_axes=frozenset())

    assert decisions == (
        AxisStepDecision(axis="brevity", direction=-1, up_count=0, down_count=2),
    )


def test_two_same_direction_up_signals_step_the_axis_up() -> None:
    decisions = decide_persona_steps(["too terse", "too terse"], pinned_axes=frozenset())

    assert decisions == (AxisStepDecision(axis="brevity", direction=1, up_count=2, down_count=0),)


def test_unmapped_phrase_is_silently_skipped() -> None:
    decisions = decide_persona_steps(
        ["not in the lexicon", "too chatty", "too chatty"], pinned_axes=frozenset()
    )

    assert decisions == (
        AxisStepDecision(axis="brevity", direction=-1, up_count=0, down_count=2),
    )


# --------------------------------------------------------------------------------------- AC2


def test_mixed_signals_move_nothing() -> None:
    decisions = decide_persona_steps(["too chatty", "too terse"], pinned_axes=frozenset())

    assert decisions == ()


def test_single_below_threshold_signal_moves_nothing() -> None:
    decisions = decide_persona_steps(["too chatty"], pinned_axes=frozenset())

    assert decisions == ()


def test_empty_window_moves_nothing() -> None:
    assert decide_persona_steps([], pinned_axes=frozenset()) == ()


def test_property_one_step_per_axis_never_without_two_consistent_signals() -> None:
    """Random seeded event sets (stdlib random — no hypothesis, NG-3, mirrors
    ``test_commands.py``'s own property-test idiom): draw a random multiset of lexicon phrases
    (plus noise strings that map to nothing) and a random pinned-axes subset, then verify the
    decision-rule invariants hold structurally."""
    rng = random.Random(0)
    lexicon_phrases = [phrase for phrase, _token in FEEDBACK_LEXICON]
    all_axes = sorted({token.axis for _phrase, token in FEEDBACK_LEXICON})

    for _trial in range(2000):
        window = [rng.choice(lexicon_phrases) for _ in range(rng.randint(0, 12))]
        window += [f"noise-{i}" for i in range(rng.randint(0, 3))]
        rng.shuffle(window)
        pinned = frozenset(rng.sample(all_axes, k=rng.randint(0, len(all_axes))))

        decisions = decide_persona_steps(window, pinned_axes=pinned)

        seen_axes: set[str] = set()
        for decision in decisions:
            assert decision.axis not in seen_axes  # never more than one decision per axis
            seen_axes.add(decision.axis)
            assert decision.axis not in pinned  # never a pinned axis

            # independently recompute the axis's true up/down tally over the SAME window
            up = sum(
                1
                for phrase in window
                if (tok := token_for_phrase(phrase)) is not None
                and tok.axis == decision.axis
                and tok.direction == "up"
            )
            down = sum(
                1
                for phrase in window
                if (tok := token_for_phrase(phrase)) is not None
                and tok.axis == decision.axis
                and tok.direction == "down"
            )
            assert decision.up_count == up
            assert decision.down_count == down

            if decision.direction == 1:
                assert up >= PERSONA_STEP_THRESHOLD
                assert down == 0
            else:
                assert decision.direction == -1
                assert down >= PERSONA_STEP_THRESHOLD
                assert up == 0


# --------------------------------------------------------------------------------------- AC3


def test_pinned_axis_never_steps_despite_a_clear_signal() -> None:
    decisions = decide_persona_steps(
        ["too chatty", "too chatty"], pinned_axes=frozenset({"brevity"})
    )

    assert decisions == ()


def test_pinned_axis_does_not_suppress_other_unpinned_axes() -> None:
    decisions = decide_persona_steps(
        ["too chatty", "too chatty", "too stiff", "too stiff"],
        pinned_axes=frozenset({"brevity"}),
    )

    assert decisions == (AxisStepDecision(axis="warmth", direction=1, up_count=2, down_count=0),)
