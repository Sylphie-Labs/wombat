"""TK-206 — PersonaMatrix domain type acceptance criteria (EP-33, DEC-33/DEC-37).

  AC1 each axis has EXACTLY the DEC-33/DEC-37 levels (checked against the enum member list);
      ``PersonaMatrix`` is frozen + hashable (``hash()`` works; ``FrozenInstanceError`` on
      attribute set); ``DEFAULT_MATRIX`` == (terse, reserved, plain, none, balanced).
  AC2 round-trip: for every matrix in the full 3*3*3*2*3=162 space,
      ``from_strings(to_strings(m)) == m``; parsing a dict with an invalid axis value raises
      ``ValueError`` whose message names both the axis and the offending value.
  AC3 the module docstring names all six constitution-excluded axes and their ids
      (CON-2, CON-3, CON-6, CON-1, CON-5, NG-2, NG-3).
"""

from __future__ import annotations

import dataclasses
import itertools

import pytest

from wombat.persona import matrix
from wombat.persona.matrix import (
    DEFAULT_MATRIX,
    Brevity,
    Directness,
    Humor,
    PersonaMatrix,
    Proactivity,
    Warmth,
    from_strings,
    to_strings,
)

# --------------------------------------------------------------------------------------- AC1


def test_brevity_levels() -> None:
    assert list(Brevity) == [Brevity.TERSE, Brevity.BALANCED, Brevity.EXPANSIVE]


def test_warmth_levels() -> None:
    assert list(Warmth) == [Warmth.RESERVED, Warmth.NEUTRAL, Warmth.WARM]


def test_directness_levels() -> None:
    assert list(Directness) == [Directness.GENTLE, Directness.PLAIN, Directness.BLUNT]


def test_humor_levels_exactly_two() -> None:
    assert list(Humor) == [Humor.NONE, Humor.DRY]


def test_proactivity_levels() -> None:
    assert list(Proactivity) == [Proactivity.MINIMAL, Proactivity.BALANCED, Proactivity.FORWARD]


def test_persona_matrix_is_frozen_and_hashable() -> None:
    m = DEFAULT_MATRIX
    assert hash(m) is not None
    with pytest.raises(dataclasses.FrozenInstanceError):
        m.brevity = Brevity.EXPANSIVE  # type: ignore[misc]


def test_default_matrix() -> None:
    assert PersonaMatrix(
        brevity=Brevity.TERSE,
        warmth=Warmth.RESERVED,
        directness=Directness.PLAIN,
        humor=Humor.NONE,
        proactivity=Proactivity.BALANCED,
    ) == DEFAULT_MATRIX


# --------------------------------------------------------------------------------------- AC2


def _all_matrices() -> list[PersonaMatrix]:
    return [
        PersonaMatrix(brevity=b, warmth=w, directness=d, humor=h, proactivity=p)
        for b, w, d, h, p in itertools.product(Brevity, Warmth, Directness, Humor, Proactivity)
    ]


def test_full_space_is_162() -> None:
    assert len(_all_matrices()) == 3 * 3 * 3 * 2 * 3 == 162


def test_round_trip_full_space() -> None:
    for m in _all_matrices():
        assert from_strings(to_strings(m)) == m


def test_from_strings_unknown_value_names_axis_and_value() -> None:
    values = to_strings(DEFAULT_MATRIX)
    values["warmth"] = "nonsense"
    with pytest.raises(ValueError) as excinfo:
        from_strings(values)
    message = str(excinfo.value)
    assert "warmth" in message
    assert "nonsense" in message


def test_from_strings_missing_axis_raises() -> None:
    values = to_strings(DEFAULT_MATRIX)
    del values["proactivity"]
    with pytest.raises(ValueError, match="proactivity"):
        from_strings(values)


# --------------------------------------------------------------------------------------- AC3


def test_module_doc_names_excluded_axes_and_constitution_ids() -> None:
    doc = matrix.__doc__
    assert doc is not None

    excluded_axes = [
        "interruption-eagerness",
        "chattiness-as-frequency",
        "empathy-as-motive-inference",
        "persuasion/flattery/sycophancy",
        "action-initiative",
        "coaching/clinical register",
        "persistence/reminder-frequency",
    ]
    for phrase in excluded_axes:
        assert phrase in doc

    constitution_ids = ["CON-1", "CON-2", "CON-3", "CON-5", "CON-6", "NG-2", "NG-3"]
    for con_id in constitution_ids:
        assert con_id in doc
