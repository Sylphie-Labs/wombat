"""TK-220 — persona policy-as-data acceptance criteria (EP-33, DEC-38(1)/(4), Q-108(b)).

  AC2 data-driven placement: a test-fixture policy granting humor to the draft mouth with a
      distinct clause text renders it for draft with ZERO code changes, and the immutable
      guard_suffix is still present (TK-219 seam invariant).
  AC3 fail-loud: missing or malformed policy file, unknown axis/level/mouth key, proactivity
      present, version mismatch, or non-empty DEFAULT-level clause — each fails LOUD naming the
      offense; no silent fallback to built-in text.

AC1 (byte-identity from data, now fed through wombat.persona.policy) is exercised by
tests/persona/test_builder.py — this module holds the loader's OWN acceptance criteria plus the
AC2 data-driven-placement scenario.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml

from wombat.persona.builder import ClauseAlgebraStrategy, Mouth
from wombat.persona.expression import EMPTY_CUES, render_expression
from wombat.persona.matrix import DEFAULT_MATRIX, Humor, PersonaMatrix
from wombat.persona.policy import (
    PERSONA_POLICY_VERSION,
    PersonaPolicyError,
    default_policy,
    load_policy,
)

# --------------------------------------------------------------------------------------- helpers


def _valid_policy_dict() -> dict[str, Any]:
    """A fresh, deep-copyable, VALID policy payload — the shipped default's shape, so each test
    mutates its own copy rather than sharing mutable state."""

    return {
        "version": PERSONA_POLICY_VERSION,
        "mouth_axes": {
            "compose": ["brevity", "warmth", "directness", "humor"],
            "brief": ["brevity", "warmth", "directness", "humor"],
            "draft": ["brevity", "warmth", "directness"],
            "reflection": ["brevity", "warmth", "directness"],
            # TK-292 (DEC-65a/c): the companion register — humor in-bounds, same as compose/brief.
            "chat": ["brevity", "warmth", "directness", "humor"],
        },
        "clauses": {
            "brevity": {
                "terse": "",
                "balanced": "A sentence or two is fine if it helps clarity.",
                "expansive": "Feel free to add a bit more detail and context.",
            },
            "warmth": {
                "reserved": "",
                "neutral": "Keep the tone even and matter-of-fact.",
                "warm": "Let the tone feel warm and friendly.",
            },
            "directness": {
                "gentle": "Soften the phrasing and hedge gently.",
                "plain": "",
                "blunt": "Be direct and blunt, without hedging.",
            },
            "humor": {
                "none": "",
                "dry": "A touch of dry humor is welcome.",
            },
        },
    }


def _write_policy(tmp_path: Path, payload: Any, name: str = "persona_policy.yaml") -> Path:
    path = tmp_path / name
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return path


def _write_raw(tmp_path: Path, text: str, name: str = "persona_policy.yaml") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


# --------------------------------------------------------------------------------------- AC2


def test_fixture_policy_grants_humor_to_draft_with_zero_code_changes(tmp_path: Path) -> None:
    payload = _valid_policy_dict()
    payload["mouth_axes"]["draft"] = ["brevity", "warmth", "directness", "humor"]
    payload["clauses"]["humor"]["dry"] = "A distinct test-only aside."
    policy = load_policy(_write_policy(tmp_path, payload))

    matrix = PersonaMatrix(
        brevity=DEFAULT_MATRIX.brevity,
        warmth=DEFAULT_MATRIX.warmth,
        directness=DEFAULT_MATRIX.directness,
        humor=Humor.DRY,
        proactivity=DEFAULT_MATRIX.proactivity,
    )
    strategy = ClauseAlgebraStrategy("Steward", policy=policy)
    result = render_expression(strategy, Mouth.DRAFT, matrix, EMPTY_CUES)

    assert "A distinct test-only aside." in result.instruction
    # TK-219 seam invariant: the immutable draft guard suffix is still present, unconditionally.
    assert result.instruction.endswith("No preamble, no signature.")


def test_fixture_policy_draft_humor_absent_when_humor_level_is_default(tmp_path: Path) -> None:
    payload = _valid_policy_dict()
    payload["mouth_axes"]["draft"] = ["brevity", "warmth", "directness", "humor"]
    payload["clauses"]["humor"]["dry"] = "A distinct test-only aside."
    policy = load_policy(_write_policy(tmp_path, payload))

    strategy = ClauseAlgebraStrategy("Steward", policy=policy)
    result = render_expression(strategy, Mouth.DRAFT, DEFAULT_MATRIX, EMPTY_CUES)

    assert "A distinct test-only aside." not in result.instruction


def test_default_policy_loads_shipped_yaml() -> None:
    """The packaged persona_policy.yaml itself loads cleanly (proves it ships + validates)."""

    policy = default_policy()
    assert policy.version == PERSONA_POLICY_VERSION
    assert set(policy.mouth_axes) == {"compose", "brief", "draft", "reflection", "chat"}
    assert set(policy.clauses) == {"brevity", "warmth", "directness", "humor"}


def test_default_policy_chat_mouth_axes_is_exactly_the_four_axes() -> None:
    """AC3 (TK-292): the shipped yaml's mouth_axes['chat'] is exactly brevity/warmth/
    directness/humor — the same four axes compose/brief render, reusing existing clause text."""

    assert set(default_policy().mouth_axes["chat"]) == {
        "brevity",
        "warmth",
        "directness",
        "humor",
    }


def test_chat_humor_dry_appends_the_existing_dry_clause() -> None:
    """AC3 (TK-292): humor=dry on a CHAT render appends the SAME dry clause text compose/brief
    already use — no new clause string was added."""

    policy = default_policy()
    matrix = PersonaMatrix(
        brevity=DEFAULT_MATRIX.brevity,
        warmth=DEFAULT_MATRIX.warmth,
        directness=DEFAULT_MATRIX.directness,
        humor=Humor.DRY,
        proactivity=DEFAULT_MATRIX.proactivity,
    )
    strategy = ClauseAlgebraStrategy("Steward", policy=policy)
    result = render_expression(strategy, Mouth.CHAT, matrix, EMPTY_CUES)

    assert policy.clauses["humor"][Humor.DRY.value] in result.instruction


def test_default_policy_is_cached_singleton() -> None:
    assert default_policy() is default_policy()


# --------------------------------------------------------------------------------------- AC3


def test_missing_file_fails_loud(tmp_path: Path) -> None:
    with pytest.raises(PersonaPolicyError, match="not readable"):
        load_policy(tmp_path / "does_not_exist.yaml")


def test_malformed_yaml_fails_loud(tmp_path: Path) -> None:
    path = _write_raw(tmp_path, "version: 1\nmouth_axes: [unclosed\n")
    with pytest.raises(PersonaPolicyError, match="not valid YAML"):
        load_policy(path)


def test_non_mapping_yaml_fails_loud(tmp_path: Path) -> None:
    path = _write_raw(tmp_path, "- just\n- a\n- list\n")
    with pytest.raises(PersonaPolicyError, match="must contain a YAML mapping"):
        load_policy(path)


def test_version_mismatch_fails_loud(tmp_path: Path) -> None:
    payload = _valid_policy_dict()
    payload["version"] = PERSONA_POLICY_VERSION + 1
    with pytest.raises(PersonaPolicyError, match="version"):
        load_policy(_write_policy(tmp_path, payload))


def test_unknown_top_level_key_fails_loud(tmp_path: Path) -> None:
    payload = _valid_policy_dict()
    payload["extra_field"] = "surprise"
    with pytest.raises(PersonaPolicyError, match="unknown top-level key"):
        load_policy(_write_policy(tmp_path, payload))


def test_missing_top_level_key_fails_loud(tmp_path: Path) -> None:
    payload = _valid_policy_dict()
    del payload["clauses"]
    with pytest.raises(PersonaPolicyError, match="missing top-level key"):
        load_policy(_write_policy(tmp_path, payload))


def test_unknown_mouth_key_fails_loud(tmp_path: Path) -> None:
    payload = _valid_policy_dict()
    payload["mouth_axes"]["side_channel"] = ["brevity"]
    with pytest.raises(PersonaPolicyError, match="unknown mouth key"):
        load_policy(_write_policy(tmp_path, payload))


def test_missing_mouth_key_fails_loud(tmp_path: Path) -> None:
    payload = _valid_policy_dict()
    del payload["mouth_axes"]["draft"]
    with pytest.raises(PersonaPolicyError, match="missing mouth key"):
        load_policy(_write_policy(tmp_path, payload))


def test_unknown_axis_name_in_mouth_axes_fails_loud(tmp_path: Path) -> None:
    payload = _valid_policy_dict()
    payload["mouth_axes"]["draft"].append("snark")
    with pytest.raises(PersonaPolicyError, match="unknown axis name"):
        load_policy(_write_policy(tmp_path, payload))


def test_proactivity_in_mouth_axes_fails_loud(tmp_path: Path) -> None:
    payload = _valid_policy_dict()
    payload["mouth_axes"]["draft"].append("proactivity")
    with pytest.raises(PersonaPolicyError, match="proactivity"):
        load_policy(_write_policy(tmp_path, payload))


def test_proactivity_in_clauses_fails_loud(tmp_path: Path) -> None:
    payload = _valid_policy_dict()
    payload["clauses"]["proactivity"] = {"minimal": "", "balanced": "", "forward": ""}
    with pytest.raises(PersonaPolicyError, match="proactivity"):
        load_policy(_write_policy(tmp_path, payload))


def test_unknown_axis_key_in_clauses_fails_loud(tmp_path: Path) -> None:
    payload = _valid_policy_dict()
    payload["clauses"]["snark"] = {"none": ""}
    with pytest.raises(PersonaPolicyError, match="unknown axis key"):
        load_policy(_write_policy(tmp_path, payload))


def test_missing_axis_key_in_clauses_fails_loud(tmp_path: Path) -> None:
    payload = _valid_policy_dict()
    del payload["clauses"]["humor"]
    with pytest.raises(PersonaPolicyError, match="missing axis key"):
        load_policy(_write_policy(tmp_path, payload))


def test_unknown_level_key_fails_loud(tmp_path: Path) -> None:
    payload = _valid_policy_dict()
    payload["clauses"]["humor"]["playful"] = "Struck by DEC-37(c) — must never validate."
    with pytest.raises(PersonaPolicyError, match="unknown level key"):
        load_policy(_write_policy(tmp_path, payload))


def test_missing_level_key_fails_loud(tmp_path: Path) -> None:
    payload = _valid_policy_dict()
    del payload["clauses"]["humor"]["dry"]
    with pytest.raises(PersonaPolicyError, match="missing level key"):
        load_policy(_write_policy(tmp_path, payload))


def test_non_string_clause_text_fails_loud(tmp_path: Path) -> None:
    payload = _valid_policy_dict()
    payload["clauses"]["humor"]["dry"] = 42
    with pytest.raises(PersonaPolicyError, match="must be a string"):
        load_policy(_write_policy(tmp_path, payload))


def test_non_empty_default_level_clause_fails_loud(tmp_path: Path) -> None:
    payload = _valid_policy_dict()
    payload["clauses"]["humor"]["none"] = "not empty"
    with pytest.raises(PersonaPolicyError, match="DEFAULT level"):
        load_policy(_write_policy(tmp_path, payload))


@pytest.mark.parametrize(
    "axis,default_level",
    [
        ("brevity", "terse"),
        ("warmth", "reserved"),
        ("directness", "plain"),
        ("humor", "none"),
    ],
)
def test_every_axis_default_level_must_be_empty(
    tmp_path: Path, axis: str, default_level: str
) -> None:
    payload = _valid_policy_dict()
    payload["clauses"][axis][default_level] = "not empty"
    with pytest.raises(PersonaPolicyError, match="DEFAULT level"):
        load_policy(_write_policy(tmp_path, payload))


def test_mouth_axes_not_a_mapping_fails_loud(tmp_path: Path) -> None:
    payload = _valid_policy_dict()
    payload["mouth_axes"] = ["not", "a", "mapping"]
    with pytest.raises(PersonaPolicyError, match="mouth_axes must be a mapping"):
        load_policy(_write_policy(tmp_path, payload))


def test_clauses_not_a_mapping_fails_loud(tmp_path: Path) -> None:
    payload = _valid_policy_dict()
    payload["clauses"] = ["not", "a", "mapping"]
    with pytest.raises(PersonaPolicyError, match="clauses must be a mapping"):
        load_policy(_write_policy(tmp_path, payload))


def test_valid_fixture_still_loads(tmp_path: Path) -> None:
    """Sanity check: the helper's baseline payload (unmutated) is itself valid — every negative
    test above is proven to fail for the SPECIFIC mutation, not for an already-broken fixture."""

    policy = load_policy(_write_policy(tmp_path, copy.deepcopy(_valid_policy_dict())))
    assert policy.version == PERSONA_POLICY_VERSION
