"""Tests for the rating-parameter vocabulary leaf (TK-41, EP-10).

TK-301 (DEC-67c) AC2 adds two lock-step checks for the UNRELATED-but-neighboring
``wombat.params.OperatingParams``/``PersonalityBand`` store: a params file missing the new
``eager`` field raises loud, and a pre-ticket "version 8" file (which necessarily lacks
``eager``, since the field was added in the version-9 bump) fails the same way — see
``test_missing_eager_field_fails_loud_at_load`` / ``test_version_8_file_missing_eager_fails_
loud_at_load`` below.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
import yaml

from wombat.params import OperatingParamsError, load_operating_params
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


# --- TK-301 (DEC-67c) AC2 — personality_band.eager lock-step -----------------------------


def _shipped_params_mapping() -> dict[str, object]:
    from importlib import resources

    packaged = Path(str(resources.files("wombat").joinpath("wombat_params.yaml")))
    mapping = yaml.safe_load(packaged.read_text(encoding="utf-8"))
    assert isinstance(mapping, dict)
    return mapping


def _personality_band(mapping: dict[str, object]) -> dict[str, object]:
    band = mapping["personality_band"]
    assert isinstance(band, dict)
    return band


def test_missing_eager_field_fails_loud_at_load(tmp_path: Path) -> None:
    """A params file whose personality_band lacks "eager" fails LOUD (AC2) — the field has no
    Python default, so pydantic's required-field validation raises rather than silently
    defaulting."""
    mapping = _shipped_params_mapping()
    band = _personality_band(mapping)
    assert "eager" in band  # sanity: the shipped file DOES carry it
    del band["eager"]

    dst = tmp_path / "wombat_params.yaml"
    dst.write_text(yaml.safe_dump(mapping), encoding="utf-8")

    with pytest.raises(OperatingParamsError):
        load_operating_params(dst)


def test_version_8_file_missing_eager_fails_loud_at_load(tmp_path: Path) -> None:
    """A pre-ticket "version 8" file (eager did not exist yet at that version) fails the same
    lock-step load as any other file missing the field (AC2)."""
    mapping = _shipped_params_mapping()
    mapping["version"] = 8
    del _personality_band(mapping)["eager"]

    dst = tmp_path / "wombat_params.yaml"
    dst.write_text(yaml.safe_dump(mapping), encoding="utf-8")

    with pytest.raises(OperatingParamsError):
        load_operating_params(dst)
