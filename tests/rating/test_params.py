"""Tests for the rating-parameter vocabulary leaf (TK-41, EP-10).

TK-301 (DEC-67c) AC2 adds two lock-step checks for the UNRELATED-but-neighboring
``wombat.params.OperatingParams``/``PersonalityBand`` store: a params file missing the new
``eager`` field raises loud, and a pre-ticket "version 8" file (which necessarily lacks
``eager``, since the field was added in the version-9 bump) fails the same way — see
``test_missing_eager_field_fails_loud_at_load`` / ``test_version_8_file_missing_eager_fails_
loud_at_load`` below.
"""

from __future__ import annotations

import logging
from dataclasses import FrozenInstanceError
from datetime import time
from pathlib import Path

import pytest
import yaml

from wombat.params import PARAMS_APP_EDITABLE, OperatingParamsError, load_operating_params
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


# --- TK-302 (DEC-67d/h): PARAMS_APP_EDITABLE overlay spec + load_operating_params(overlay=) ----


def test_params_app_editable_key_set_is_exactly_the_eight_dec67d_keys() -> None:
    # DEC-67(d) non_goal: no overlay of any non-enumerated field — the spec's key set is CLOSED,
    # pinned verbatim.
    assert set(PARAMS_APP_EDITABLE) == {
        "wombat_param_morning_brief_time",
        "wombat_param_nightly_dream_time",
        "wombat_param_urgency_threshold",
        "wombat_param_per_class_daily_ceiling",
        "wombat_param_decay_ttl_seconds",
        "wombat_param_mouth_model_timeout_seconds",
        "wombat_param_mouth_daily_token_ceiling",
        "wombat_param_mouth_max_usd_per_drive",
    }


def test_params_app_editable_bounds_match_dec67d_verbatim() -> None:
    resolved = {key: (spec.field, spec.min, spec.max) for key, spec in PARAMS_APP_EDITABLE.items()}
    assert resolved == {
        "wombat_param_morning_brief_time": ("morning_brief_time", None, None),
        "wombat_param_nightly_dream_time": ("nightly_dream_time", None, None),
        "wombat_param_urgency_threshold": ("urgency_threshold", 0.60, 0.95),
        "wombat_param_per_class_daily_ceiling": ("per_class_daily_ceiling", 0, 10),
        "wombat_param_decay_ttl_seconds": ("decay_ttl_seconds", 3600.0, 604800.0),
        "wombat_param_mouth_model_timeout_seconds": ("mouth_model_timeout_seconds", 2.0, 60.0),
        "wombat_param_mouth_daily_token_ceiling": ("mouth_daily_token_ceiling", 10000, 1000000),
        "wombat_param_mouth_max_usd_per_drive": ("mouth_max_usd_per_drive", 0.05, 5.00),
    }


def test_non_enumerated_fields_are_structurally_absent_from_the_spec() -> None:
    """OUT of scope (verbatim): rating_tuner/personality_band/flush/presence/sweeper/dream
    fields are never overlay-able — the spec maps ONLY to the eight documented fields."""
    mapped_fields = {spec.field for spec in PARAMS_APP_EDITABLE.values()}
    assert "rating_tuner" not in mapped_fields
    assert "personality_band" not in mapped_fields
    assert "flush_min_age_seconds" not in mapped_fields
    assert "presence_staleness_ceiling_seconds" not in mapped_fields
    assert "sweeper_interval_seconds" not in mapped_fields
    assert "dream_budget_max_usd" not in mapped_fields


def test_overlay_with_all_eight_rows_lands_and_is_file_equal_otherwise() -> None:
    # AC1: a store seeded with all eight rows -> OperatingParams carries them, everything else
    # stays file-equal.
    base = load_operating_params()
    overlay = {
        "wombat_param_morning_brief_time": "08:15:00",
        "wombat_param_nightly_dream_time": "03:30:00",
        "wombat_param_urgency_threshold": "0.80",
        "wombat_param_per_class_daily_ceiling": "5",
        "wombat_param_decay_ttl_seconds": "7200",
        "wombat_param_mouth_model_timeout_seconds": "15",
        "wombat_param_mouth_daily_token_ceiling": "50000",
        "wombat_param_mouth_max_usd_per_drive": "1.25",
    }

    overlaid = load_operating_params(overlay=overlay)

    assert overlaid.morning_brief_time == time(8, 15, 0)
    assert overlaid.nightly_dream_time == time(3, 30, 0)
    assert overlaid.urgency_threshold == 0.80
    assert overlaid.per_class_daily_ceiling == 5
    assert overlaid.decay_ttl_seconds == 7200.0
    assert overlaid.mouth_model_timeout_seconds == 15.0
    assert overlaid.mouth_daily_token_ceiling == 50000
    assert overlaid.mouth_max_usd_per_drive == 1.25

    overlaid_fields = {spec.field for spec in PARAMS_APP_EDITABLE.values()}
    assert overlaid.model_dump(exclude=overlaid_fields) == base.model_dump(exclude=overlaid_fields)


def test_per_class_daily_ceiling_zero_is_accepted_unclamped() -> None:
    # "0 = user turning immediate voice OFF - their right" — 0 sits AT the floor, never clamped
    # above it.
    overlaid = load_operating_params(overlay={"wombat_param_per_class_daily_ceiling": "0"})
    assert overlaid.per_class_daily_ceiling == 0


def test_no_overlay_rows_returns_a_byte_equal_model_dump() -> None:
    # AC1: no rows -> byte-equal model dump, whether overlay is None or an empty mapping.
    base = load_operating_params()
    assert load_operating_params(overlay=None).model_dump() == base.model_dump()
    assert load_operating_params(overlay={}).model_dump() == base.model_dump()


def test_ac2_out_of_band_value_clamps_with_one_warning(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        overlaid = load_operating_params(overlay={"wombat_param_urgency_threshold": "0.30"})

    assert overlaid.urgency_threshold == 0.60  # clamped to the DEC-67(d) floor
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "wombat_param_urgency_threshold" in warnings[0]


def test_ac2_unparseable_value_is_skipped_with_one_warning_and_boot_proceeds(
    caplog: pytest.LogCaptureFixture,
) -> None:
    base = load_operating_params()
    with caplog.at_level(logging.WARNING):
        # never raises — boot proceeds regardless of a garbage row.
        overlaid = load_operating_params(
            overlay={"wombat_param_mouth_daily_token_ceiling": "bananas"}
        )

    assert overlaid.mouth_daily_token_ceiling == base.mouth_daily_token_ceiling  # unchanged
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "wombat_param_mouth_daily_token_ceiling" in warnings[0]
