"""TK-13 — production operating-parameter store acceptance criteria (EP-9).

Covers all five ACs of TK-13:
  AC1  every parameter loads as a typed value; missing/mistyped fails at load; version field.
  AC2  the TK-48 RatingTuner bounded-update block — five LOCKED constants, present + equal.
  AC3  the morning-brief time is owned here and defined nowhere else.
  AC4  the owned constants are not hard-coded in the production consumer modules; the store
       lives in params.py, never config.py (DEC-25 — _sim spikes excluded as frozen provenance).
  AC5  the store is static — frozen, no writer, never written by the nightly tuner.
"""

from __future__ import annotations

import re
from datetime import time
from pathlib import Path

import pytest
from pydantic import ValidationError

import wombat.config as wombat_config
from wombat.params import (
    OperatingParams,
    OperatingParamsError,
    RatingTunerBounds,
    load_operating_params,
)

_SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "wombat"


def _valid_mapping() -> dict[str, object]:
    """A complete, well-typed parameter mapping (mirrors the shipped wombat_params.yaml)."""
    return {
        "version": 4,
        "urgency_threshold": 0.75,
        "load_flush_threshold": 1.0,
        "per_class_daily_ceiling": 3,
        "flush_min_age_seconds": 300.0,
        "decay_ttl_seconds": 86400.0,
        "max_pending": 100,
        "mouth_daily_token_ceiling": 100000,
        "mouth_max_usd_per_drive": 0.50,
        "mouth_max_calls_per_drive": 3,
        "morning_brief_time": "07:00:00",
        "rating_tuner": {
            "clamp_floor": 0.35,
            "clamp_ceiling": 0.65,
            "delta_bound": 0.05,
            "gain": 0.20,
            "surfacing_ceiling_per_day": 12.0,
        },
        "presence_staleness_ceiling_seconds": 300.0,
        "presence_confidence_floor": 0.5,
        "presence_idle_threshold_seconds": 60.0,
        "sweeper_interval_seconds": 5.0,
        "sweeper_lease_ttl_seconds": 60.0,
    }


def _write_yaml(tmp_path: Path, mapping: object) -> Path:
    import yaml

    dst = tmp_path / "wombat_params.yaml"
    dst.write_text(yaml.safe_dump(mapping), encoding="utf-8")
    return dst


# --- AC1 -------------------------------------------------------------------------------


def test_shipped_params_load_with_every_field_typed() -> None:
    """The packaged wombat_params.yaml loads and every requested field is typed (AC1)."""
    params = load_operating_params()

    assert isinstance(params.version, int)
    assert isinstance(params.urgency_threshold, float)
    assert isinstance(params.load_flush_threshold, float)
    assert isinstance(params.flush_min_age_seconds, float)
    assert isinstance(params.decay_ttl_seconds, float)
    assert isinstance(params.max_pending, int)
    assert isinstance(params.per_class_daily_ceiling, int)
    assert isinstance(params.mouth_daily_token_ceiling, int)
    assert isinstance(params.mouth_max_usd_per_drive, float)
    assert isinstance(params.mouth_max_calls_per_drive, int)
    assert isinstance(params.morning_brief_time, time)
    assert isinstance(params.rating_tuner, RatingTunerBounds)
    assert isinstance(params.presence_staleness_ceiling_seconds, float)
    assert isinstance(params.presence_confidence_floor, float)
    assert isinstance(params.presence_idle_threshold_seconds, float)
    assert isinstance(params.sweeper_interval_seconds, float)
    assert isinstance(params.sweeper_lease_ttl_seconds, float)


def test_file_carries_a_version_field() -> None:
    """The store carries a version field for auditability (AC1/AC5)."""
    assert load_operating_params().version >= 1


def test_missing_field_fails_loud_at_load(tmp_path: Path) -> None:
    """A missing field raises at load — no silent default (AC1)."""
    mapping = _valid_mapping()
    del mapping["urgency_threshold"]
    with pytest.raises(OperatingParamsError):
        load_operating_params(_write_yaml(tmp_path, mapping))


def test_mistyped_field_fails_loud_at_load(tmp_path: Path) -> None:
    """A mistyped field raises at load — no silent coercion to a default (AC1)."""
    mapping = _valid_mapping()
    mapping["morning_brief_time"] = "not-a-time"
    with pytest.raises(OperatingParamsError):
        load_operating_params(_write_yaml(tmp_path, mapping))


def test_unexpected_field_fails_loud_at_load(tmp_path: Path) -> None:
    """An unknown field raises (extra='forbid') — the schema is closed (AC1)."""
    mapping = _valid_mapping()
    mapping["surprise_knob"] = 1
    with pytest.raises(OperatingParamsError):
        load_operating_params(_write_yaml(tmp_path, mapping))


def test_non_mapping_file_fails_loud(tmp_path: Path) -> None:
    """A YAML file that is not a mapping fails loud rather than half-loading (AC1)."""
    with pytest.raises(OperatingParamsError):
        load_operating_params(_write_yaml(tmp_path, ["not", "a", "mapping"]))


# --- AC2 — the TK-48 LOCKED bounded-update block ---------------------------------------

# The FIVE locked constants the TK-48 (RISK-4) ablation derived. They are ONE coherent block:
# the band is chosen jointly with sensitivity so the worst-case clamped surfacing rate lands
# exactly at the ceiling — see wombat_params.yaml + RatingTunerBounds for the why.
_LOCKED_TUNER = {
    "clamp_floor": 0.35,
    "clamp_ceiling": 0.65,
    "delta_bound": 0.05,
    "gain": 0.20,
    "surfacing_ceiling_per_day": 12.0,
}


def test_tuner_block_present_typed_and_equals_locked_defaults() -> None:
    """All five tuner constants are present, typed float, and equal the TK-48 values (AC2)."""
    tuner = load_operating_params().rating_tuner
    for name, expected in _LOCKED_TUNER.items():
        actual = getattr(tuner, name)
        assert isinstance(actual, float), f"{name} must be typed float"
        assert actual == expected, f"{name}: documented default {actual} != locked {expected}"


# --- Presence hold (TK-11) — the 3 injected bounds load, are floats, equal the shipped values ---


def test_presence_hold_fields_load_are_float_and_equal_shipped_values() -> None:
    """The TK-11 presence config fields load as floats and match the documented YAML values."""
    params = load_operating_params()

    assert isinstance(params.presence_staleness_ceiling_seconds, float)
    assert params.presence_staleness_ceiling_seconds == 300.0

    assert isinstance(params.presence_confidence_floor, float)
    assert params.presence_confidence_floor == 0.5

    assert isinstance(params.presence_idle_threshold_seconds, float)
    assert params.presence_idle_threshold_seconds == 60.0


# --- Sweeper cadence (TK-53) — the 2 sweeper tunables load, are typed, equal shipped values ---


def test_sweeper_cadence_fields_load_are_typed_and_equal_shipped_values() -> None:
    """The TK-53 sweeper cadence tunables load as the documented shipped YAML values."""
    params = load_operating_params()

    assert isinstance(params.sweeper_interval_seconds, float)
    assert params.sweeper_interval_seconds == 5.0

    assert isinstance(params.sweeper_lease_ttl_seconds, float)
    assert params.sweeper_lease_ttl_seconds == 60.0


# --- Spend ledger (TK-9) — the 3 mouth-budget tunables load, are typed, equal shipped values ---


def test_spend_ledger_fields_load_are_typed_and_equal_shipped_values() -> None:
    """The TK-9 mouth-budget tunables load as the documented shipped YAML values."""
    params = load_operating_params()

    assert isinstance(params.mouth_daily_token_ceiling, int)
    assert params.mouth_daily_token_ceiling == 100000

    assert isinstance(params.mouth_max_usd_per_drive, float)
    assert params.mouth_max_usd_per_drive == 0.50

    assert isinstance(params.mouth_max_calls_per_drive, int)
    assert params.mouth_max_calls_per_drive == 3


# --- AC3 — the morning-brief time is owned here and nowhere else ------------------------


def test_morning_brief_time_has_a_single_fixed_value() -> None:
    """The brief time is a single fixed value read from this store (AC3)."""
    assert load_operating_params().morning_brief_time == time(7, 0, 0)


def test_no_second_definition_of_the_brief_time_elsewhere() -> None:
    """No other src module defines the brief time — closing the TK-97 gap (AC3).

    Scans src/wombat for the brief-time literal, allowing only params.py (the field) and the
    YAML source-of-truth. A second hard-coded "07:00" anywhere else is a competing definition.
    """
    offenders: list[str] = []
    for py in _SRC_ROOT.rglob("*.py"):
        if py.name == "params.py":
            continue
        if re.search(r"\b07:00\b", py.read_text(encoding="utf-8")):
            offenders.append(str(py))
    assert not offenders, f"brief time redefined outside the param store: {offenders}"


# --- AC4 — owned constants not hard-coded in production consumers; store home -----------

# The DECLARED list of PRODUCTION consumer module paths (this list is the guard's source of
# truth). It becomes enforcing the moment TK-27/28/9/49 land these files, and passes
# vacuously while they are still absent. DEC-25: the de-risk spikes rating/tuner_sim.py and
# gate/trigger_sim.py are EXCLUDED — tuner_sim is the frozen provenance the LOCKED constants
# were lifted FROM, and trigger_sim takes thresholds as args (owns no module-level constant).
_PRODUCTION_CONSUMER_PATHS = (
    _SRC_ROOT / "gate" / "scoring.py",
    _SRC_ROOT / "gate" / "pipeline.py",
    _SRC_ROOT / "gate" / "trigger.py",  # TK-27
    _SRC_ROOT / "gate" / "ceiling.py",  # TK-27
    _SRC_ROOT / "gate" / "decay.py",  # TK-28 (decay_ttl_seconds)
    _SRC_ROOT / "cost" / "daily_spend_ledger.py",  # TK-9
    _SRC_ROOT / "stages" / "compose.py",  # TK-9 (mouth ceilings injected, not hard-coded)
    _SRC_ROOT / "bootstrap.py",  # TK-9 (BudgetPolicy ceilings wired from OperatingParams)
    _SRC_ROOT / "runtime.py",  # TK-53 (sweeper cadence injected, not hard-coded)
    _SRC_ROOT / "rating" / "rating_tuner.py",  # TK-49, not yet built
)

# The owned, distinctive numeric literals. Generic values (1.0, 3, 100) are intentionally
# omitted — they false-positive on incidental arithmetic; these floats do not.
_OWNED_LITERALS = ("0.75", "0.35", "0.65", "0.05", "12.0")


def test_production_consumers_do_not_hard_code_owned_constants() -> None:
    """No production consumer re-defines an owned constant inline (AC4, DEC-25).

    Excludes the frozen _sim spikes by construction: they are simply not in the declared
    path list. Absent production files pass vacuously, so the guard is safe to land now and
    sharp the moment the consumers arrive and import from OperatingParams.
    """
    offenders: list[str] = []
    for path in _PRODUCTION_CONSUMER_PATHS:
        if not path.exists():
            continue  # vacuously clean until TK-27/28/9/49 land
        src = path.read_text(encoding="utf-8")
        for literal in _OWNED_LITERALS:
            if literal in src:
                offenders.append(f"{path.name}: {literal}")
    assert not offenders, f"owned constants hard-coded in production consumers: {offenders}"


def test_frozen_spikes_are_excluded_from_the_guard() -> None:
    """Guard scope check: the _sim spikes are not in the scanned path list (DEC-25)."""
    scanned = {p.name for p in _PRODUCTION_CONSUMER_PATHS}
    assert "tuner_sim.py" not in scanned
    assert "trigger_sim.py" not in scanned


def test_operating_params_lives_in_params_not_config() -> None:
    """The store home is wombat.params, never wombat.config (no WombatConfig collision, AC4)."""
    assert OperatingParams.__module__ == "wombat.params"
    assert not hasattr(wombat_config, "OperatingParams")
    # And the credential config does not leak operating constants.
    assert not hasattr(wombat_config, "urgency_threshold")


# --- AC5 — static store, never written by the tuner ------------------------------------


def test_operating_params_is_frozen() -> None:
    """The loaded store cannot be mutated in process (AC5 — static config)."""
    params = load_operating_params()
    with pytest.raises(ValidationError):
        params.urgency_threshold = 0.1  # type: ignore[misc]
    with pytest.raises(ValidationError):
        params.rating_tuner.gain = 0.99  # type: ignore[misc]


def test_param_store_module_exposes_no_writer() -> None:
    """The store module has no write path, so the nightly tuner cannot write here (AC5).

    The static operating config is human-edited only; TK-49 writes adaptive RatingParams into
    the cog-worx user scope, a separate path. Assert params.py contains no YAML/file writer.
    """
    src = (_SRC_ROOT / "params.py").read_text(encoding="utf-8")
    assert "yaml.safe_dump" not in src and "yaml.dump" not in src
    assert ".write_text" not in src and ".write_bytes" not in src
    assert re.search(r"open\([^)]*['\"]w", src) is None
