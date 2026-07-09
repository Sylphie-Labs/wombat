"""Tests for the rating-parameter vocabulary leaf (TK-41, EP-10)."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from wombat.params import load_operating_params
from wombat.rating.params import (
    RATING_PARAMS_VERSION,
    EventClass,
    RatingParams,
    default_params_for,
)


def test_known_event_class_returns_fully_typed_params() -> None:
    # AC1: a request for a known class returns a fully-typed RatingParams with documented
    # (non-trivial) defaults.
    params = default_params_for(EventClass.CALENDAR_CONFLICT)
    assert isinstance(params, RatingParams)
    assert params.version == RATING_PARAMS_VERSION
    assert params.urgency_base == 0.65
    assert params.load_base == 0.4


def test_every_event_class_resolves_to_params() -> None:
    for ec in EventClass:
        params = default_params_for(ec)
        assert isinstance(params, RatingParams)
        # all rating fields are present and floats
        for value in (
            params.urgency_base,
            params.urgency_gain,
            params.load_base,
            params.load_gain,
        ):
            assert isinstance(value, float)


def test_baseline_construction_uses_neutral_defaults() -> None:
    params = RatingParams()
    assert params.urgency_base == 0.5
    assert params.urgency_gain == 0.5
    assert params.load_base == 0.5
    assert params.load_gain == 0.5


def test_unknown_field_raises_type_error() -> None:
    # AC2: a misspelled / unknown field is a TypeError at construction — no silent default.
    with pytest.raises(TypeError):
        RatingParams(urgancy_base=0.9)  # type: ignore[call-arg]


def test_params_are_frozen() -> None:
    params = RatingParams()
    with pytest.raises(FrozenInstanceError):
        params.urgency_base = 0.9  # type: ignore[misc]


def test_with_updates_is_pure_and_preserves_version() -> None:
    base = default_params_for(EventClass.CALENDAR_CONFLICT)
    updated = base.with_updates(urgency_base=0.8)
    assert updated.urgency_base == 0.8
    assert updated.version == base.version
    # original untouched (purity)
    assert base.urgency_base == 0.65
    # unspecified fields carried over
    assert updated.load_base == base.load_base


def test_ac2_every_default_is_inside_the_tuner_clamp_band_and_preserves_ordinal_ordering() -> None:
    # TK-185/Q-95: the RatingTuner clamps urgency_base/load_base into a locked
    # [clamp_floor, clamp_ceiling] band (OperatingParams.rating_tuner, TK-48 joint block). Every
    # documented default must already live inside that band so the first non-empty-corpus tune
    # night moves a class WITH its outcome signal instead of snapping it to the nearest edge
    # regardless of direction (CR2-11).
    bounds = load_operating_params().rating_tuner
    urgency_base = {}
    load_base = {}
    for ec in EventClass:
        params = default_params_for(ec)
        assert bounds.clamp_floor <= params.urgency_base <= bounds.clamp_ceiling
        assert bounds.clamp_floor <= params.load_base <= bounds.clamp_ceiling
        urgency_base[ec] = params.urgency_base
        load_base[ec] = params.load_base

    # Cross-class ordinal differentiation (TK-41 design intent) is preserved inside the band:
    # CALENDAR_CONFLICT stays the most urgency-elevated class, REFLECTION the most urgency-muted;
    # MORNING_BRIEF stays the lowest-load class, DRAFT_REPLY the highest-load.
    assert urgency_base[EventClass.CALENDAR_CONFLICT] == max(urgency_base.values())
    assert urgency_base[EventClass.REFLECTION] == min(urgency_base.values())
    assert load_base[EventClass.DRAFT_REPLY] == max(load_base.values())
    assert load_base[EventClass.MORNING_BRIEF] == min(load_base.values())
