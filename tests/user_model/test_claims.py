"""Tests for the motive-free claim vocabulary (TK-43, CON-6/NG-1)."""

from __future__ import annotations

import ast
import inspect
from datetime import UTC, datetime

import pytest

from wombat.rating.params import RATING_CLAIM_PREDICATE
from wombat.user_model.claims import Claim, ClaimPredicate

_NOW = datetime(2026, 7, 9, 12, 0, 0, tzinfo=UTC)

# Tokens that would imply the vocabulary has drifted from behavior/outcome into motive (CON-6/
# NG-1). Checked against enum member names AND this module's own source (AC3).
_MOTIVE_TOKENS = ("MOTIVE", "INTENT", "REASON", "WHY", "BELIEF", "FEEL", "WANT", "AVOID")


def test_construct_behavior_observed_claim() -> None:
    """AC1: constructing a Claim with ClaimPredicate.BEHAVIOR_OBSERVED succeeds."""
    claim = Claim(
        predicate=ClaimPredicate.BEHAVIOR_OBSERVED,
        subject="calendar_conflict",
        value='{"action": "dismissed"}',
        event_id="evt-1",
        observed_at=_NOW,
    )
    assert claim.predicate is ClaimPredicate.BEHAVIOR_OBSERVED
    assert claim.subject == "calendar_conflict"


def test_construct_outcome_load_bearing_claim() -> None:
    """AC1: constructing a Claim with ClaimPredicate.OUTCOME_LOAD_BEARING succeeds."""
    claim = Claim(
        predicate=ClaimPredicate.OUTCOME_LOAD_BEARING,
        subject="idempotency-key-123",
        value='{"weight": 0.8}',
        event_id=None,
        observed_at=_NOW,
    )
    assert claim.predicate is ClaimPredicate.OUTCOME_LOAD_BEARING
    assert claim.event_id is None


def test_naive_observed_at_rejected() -> None:
    """observed_at must be tz-aware (codebase-wide aware-clock convention)."""
    with pytest.raises(ValueError, match="naive"):
        Claim(
            predicate=ClaimPredicate.BEHAVIOR_OBSERVED,
            subject="calendar_conflict",
            value="{}",
            event_id=None,
            observed_at=datetime(2026, 7, 9, 12, 0, 0),
        )


def test_raw_string_predicate_rejected_at_runtime() -> None:
    """AC2: a motive predicate is structurally impossible — there is no MOTIVE_* member, and a
    raw string predicate ('motive_avoid_conflict') raises TypeError at construction, since the
    hand-rolled string cannot slip past the runtime wall behind the mypy wall."""
    with pytest.raises(TypeError):
        Claim(
            predicate="motive_avoid_conflict",  # type: ignore[arg-type]
            subject="calendar_conflict",
            value="{}",
            event_id=None,
            observed_at=_NOW,
        )


def test_no_motive_predicate_member_exists() -> None:
    """AC2: there is no MOTIVE_* (or otherwise motive-implying) member in the closed set."""
    for member in ClaimPredicate:
        assert not any(token in member.name for token in _MOTIVE_TOKENS), (
            f"ClaimPredicate.{member.name} looks motive-implying"
        )


def test_motive_free_source_scan() -> None:
    """AC3: scan every identifier defined/used in the module source (classes, functions,
    parameters, attributes, enum members) for motive-implying tokens; assert zero matches.

    Scoped to code identifiers rather than raw text, so the module's own docstrings can
    document the CON-6/NG-1 exclusion rule (which necessarily names the forbidden concepts,
    e.g. 'never motive') without tripping this guard on its own explanatory prose. A REAL
    vocabulary drift — e.g. a new ``MOTIVE_AVOID_CONFLICT`` enum member or a
    ``motive_reason`` field/argument — is still caught, because that appears as an
    identifier, not just prose.
    """
    import wombat.user_model.claims as claims_module

    tree = ast.parse(inspect.getsource(claims_module))

    identifiers: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            identifiers.add(node.name)
        elif isinstance(node, ast.Name):
            identifiers.add(node.id)
        elif isinstance(node, ast.arg):
            identifiers.add(node.arg)
        elif isinstance(node, ast.Attribute):
            identifiers.add(node.attr)

    for identifier in identifiers:
        for token in _MOTIVE_TOKENS:
            assert token.lower() not in identifier.lower(), (
                f"identifier {identifier!r} contains motive-implying token {token!r}"
            )


def test_rating_params_predicate_matches_as_built_vocabulary() -> None:
    """ONE VOCABULARY, NO DRIFT: ClaimPredicate.RATING_PARAMS must equal the as-built
    RATING_CLAIM_PREDICATE in wombat.rating.params (TK-41/Q-41 ruling 4)."""
    assert ClaimPredicate.RATING_PARAMS.value == RATING_CLAIM_PREDICATE
