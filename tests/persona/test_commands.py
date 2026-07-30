"""TK-211 — closed persona voice-command grammar acceptance criteria (EP-34, DEC-35).

  AC1 every grammar-table utterance (plus casing/punctuation variants, e.g. "Be more brief!",
      "BE WARMER.") parses to its documented command, and ``apply()`` moves exactly one axis by
      one step (or resets), clamped at both ends (explicit saturation cases included).
  AC2 near-misses and ordinary speech return ``None``; plus a property test over a few thousand
      seeded stdlib-random printable strings that NEVER yields a command (no hypothesis — NG-3).
  AC3 structural: the module imports nothing but stdlib + ``wombat.persona.matrix``; no IO, no
      model call.
"""

from __future__ import annotations

import ast
import random
import string
import sys
from enum import StrEnum
from pathlib import Path

import pytest

from wombat.persona import commands
from wombat.persona.commands import (
    GRAMMAR,
    GRAMMAR_VERSION,
    PersonaCommand,
    apply,
    parse_persona_command,
)
from wombat.persona.matrix import (
    DEFAULT_MATRIX,
    Brevity,
    Directness,
    Humor,
    PersonaMatrix,
    Proactivity,
    Warmth,
)

_AXES = ("brevity", "warmth", "directness", "humor", "proactivity")

# --------------------------------------------------------------------------------------- AC1


def test_grammar_version_is_1() -> None:
    assert GRAMMAR_VERSION == 1


def test_grammar_has_up_and_down_step_per_axis() -> None:
    for axis in _AXES:
        steps = {
            command.step for utterance, command in GRAMMAR if command.axis == axis and command.step
        }
        assert steps == {1, -1}, f"axis {axis!r} missing an up or down step utterance"


def test_grammar_has_one_set_level_utterance_per_declared_level() -> None:
    enums: dict[str, type[StrEnum]] = {
        "brevity": Brevity,
        "warmth": Warmth,
        "directness": Directness,
        "humor": Humor,
        "proactivity": Proactivity,
    }
    for axis, enum_cls in enums.items():
        set_levels = {
            command.set_level
            for utterance, command in GRAMMAR
            if command.axis == axis and command.set_level is not None
        }
        assert set_levels == {member.value for member in enum_cls}


def test_grammar_has_a_reset() -> None:
    resets = [command for utterance, command in GRAMMAR if command.reset]
    assert len(resets) == 1


@pytest.mark.parametrize("utterance,command", GRAMMAR)
def test_every_grammar_utterance_parses_to_its_command(
    utterance: str, command: PersonaCommand
) -> None:
    assert parse_persona_command(utterance) == command


@pytest.mark.parametrize(
    "canonical,variant",
    [
        ("be more brief", "Be more brief!"),
        ("be more brief", "  BE MORE BRIEF  "),
        ("be warmer", "Be warmer."),
        ("be warmer", "BE WARMER."),
        ("be gentler", "Be, gentler?"),
        ("reset persona", "Reset Persona!"),
        ("set brevity to balanced", "Set Brevity To Balanced."),
        ("no jokes", "No Jokes!!"),
    ],
)
def test_casing_and_punctuation_variants_parse_to_the_same_command(
    canonical: str, variant: str
) -> None:
    expected = parse_persona_command(canonical)
    assert expected is not None
    assert parse_persona_command(variant) == expected


def test_apply_step_moves_exactly_one_axis_from_default() -> None:
    for utterance, command in GRAMMAR:
        if command.step is None:
            continue
        after = apply(DEFAULT_MATRIX, command)
        changed = [axis for axis in _AXES if getattr(DEFAULT_MATRIX, axis) != getattr(after, axis)]
        assert changed in ([], [command.axis]), f"{utterance!r} changed {changed}"


def test_apply_set_level_sets_exactly_that_axis() -> None:
    for _utterance, command in GRAMMAR:
        if command.set_level is None:
            continue
        assert command.axis is not None
        after = apply(DEFAULT_MATRIX, command)
        assert getattr(after, command.axis).value == command.set_level
        for axis in _AXES:
            if axis != command.axis:
                assert getattr(after, axis) == getattr(DEFAULT_MATRIX, axis)


def test_apply_reset_returns_default_matrix_regardless_of_input() -> None:
    far_matrix = PersonaMatrix(
        brevity=Brevity.EXPANSIVE,
        warmth=Warmth.WARM,
        directness=Directness.BLUNT,
        humor=Humor.DRY,
        proactivity=Proactivity.FORWARD,
    )
    reset_command = parse_persona_command("reset persona")
    assert reset_command is not None
    assert apply(far_matrix, reset_command) == DEFAULT_MATRIX
    assert apply(DEFAULT_MATRIX, reset_command) == DEFAULT_MATRIX


def test_saturation_brevity_already_terse_be_more_brief_stays_terse() -> None:
    command = parse_persona_command("be more brief")
    assert command is not None
    result = apply(DEFAULT_MATRIX, command)
    assert result.brevity == Brevity.TERSE
    assert result == DEFAULT_MATRIX


def test_saturation_at_low_end_for_every_axis() -> None:
    low_matrix = PersonaMatrix(
        brevity=Brevity.TERSE,
        warmth=Warmth.RESERVED,
        directness=Directness.GENTLE,
        humor=Humor.NONE,
        proactivity=Proactivity.MINIMAL,
    )
    for _utterance, command in GRAMMAR:
        if command.step == -1:
            assert command.axis is not None
            after = apply(low_matrix, command)
            assert getattr(after, command.axis) == getattr(low_matrix, command.axis)
            assert after == low_matrix


def test_saturation_at_high_end_for_every_axis() -> None:
    high_matrix = PersonaMatrix(
        brevity=Brevity.EXHAUSTIVE,
        warmth=Warmth.AFFECTIONATE,
        directness=Directness.BLUNT,
        humor=Humor.COMEDIAN,
        proactivity=Proactivity.EAGER,
    )
    for _utterance, command in GRAMMAR:
        if command.step == 1:
            assert command.axis is not None
            after = apply(high_matrix, command)
            assert getattr(after, command.axis) == getattr(high_matrix, command.axis)
            assert after == high_matrix


def test_apply_never_raises_for_any_grammar_command_from_any_matrix() -> None:
    matrices = [
        DEFAULT_MATRIX,
        PersonaMatrix(
            brevity=Brevity.EXPANSIVE,
            warmth=Warmth.WARM,
            directness=Directness.BLUNT,
            humor=Humor.DRY,
            proactivity=Proactivity.FORWARD,
        ),
        PersonaMatrix(
            brevity=Brevity.BALANCED,
            warmth=Warmth.NEUTRAL,
            directness=Directness.PLAIN,
            humor=Humor.NONE,
            proactivity=Proactivity.BALANCED,
        ),
    ]
    for matrix in matrices:
        for _utterance, command in GRAMMAR:
            apply(matrix, command)  # must not raise


# --------------------------------------------------------------------------------------- AC2


@pytest.mark.parametrize(
    "transcript",
    [
        "be brief about the weather",
        "brief me",
        "warm the milk",
        "",
        "   ",
        "be more briefer",
        "reset personas",
        "set brevity to extreme",
        "brevity",
        "be more brief please",
        "i said be more brief",
    ],
)
def test_near_misses_and_ordinary_speech_return_none(transcript: str) -> None:
    assert parse_persona_command(transcript) is None


def test_random_printable_text_never_yields_a_command() -> None:
    rng = random.Random(0)
    alphabet = string.printable
    for _ in range(5000):
        length = rng.randint(0, 40)
        candidate = "".join(rng.choice(alphabet) for _ in range(length))
        assert parse_persona_command(candidate) is None


# --------------------------------------------------------------------------------------- AC3


def test_module_imports_only_stdlib_and_persona_matrix() -> None:
    """CON-1/CON-2: zero IO, zero config reads, zero model calls — enforced structurally by
    inspecting this module's own import statements."""

    stdlib_modules = set(sys.stdlib_module_names) | set(sys.builtin_module_names)
    tree = ast.parse(
        Path(commands.__file__).read_text(encoding="utf-8"), filename=commands.__file__
    )
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top_level = alias.name.split(".")[0]
                assert top_level in stdlib_modules, f"non-stdlib import: {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "wombat.persona.matrix":
                continue
            top_level = module.split(".")[0]
            assert top_level in stdlib_modules, f"non-stdlib, non-matrix import: {module}"
