"""Stability-proof tests for the RatingTuner bounded-stability spike (TK-48, RISK-4).

These are the runnable checks that decide the spike verdict:
  * AC1 — N=30 nights (incl. all-IGNORED and all-LOAD_BEARING) keep all four stability
          assertions green, and a numeric report is produced.
  * AC2 — removing the clamps breaks at least one assertion (ablation: clamps are
          load-bearing).
"""

from __future__ import annotations

import pytest

from wombat.rating.params import RatingParams
from wombat.rating.tuner_sim import (
    HARD_SURFACING_CEILING,
    VARIANCE_THRESHOLD,
    NightOutcomes,
    Outcome,
    TunerConfig,
    all_ignored_schedule,
    all_load_bearing_schedule,
    evaluate_stability,
    format_report,
    mixed_schedule,
    run_simulation,
    surfacings_per_day,
    tune_one_night,
)

N = 30


def _clamped_schedules() -> list[tuple[str, list[NightOutcomes]]]:
    return [
        ("mixed", mixed_schedule(N)),
        ("all_load_bearing", all_load_bearing_schedule(N)),
        ("all_ignored", all_ignored_schedule(N)),
    ]


# --- net signal / outcome model ---------------------------------------------------------


def test_net_signal_bounds() -> None:
    assert NightOutcomes(load_bearing=10).net_signal() == 1.0
    assert NightOutcomes(ignored=5, regretted=5).net_signal() == -1.0
    assert NightOutcomes().net_signal() == 0.0  # empty night = no signal


def test_outcome_enum_complete() -> None:
    assert {o.value for o in Outcome} == {"load_bearing", "ignored", "regretted"}


def test_surfacings_monotone_in_levers() -> None:
    loud = RatingParams(urgency_base=0.9, load_base=0.1)
    quiet = RatingParams(urgency_base=0.1, load_base=0.9)
    assert surfacings_per_day(loud) > surfacings_per_day(quiet)


# --- per-night tuner bound --------------------------------------------------------------


def test_single_night_delta_respects_bound() -> None:
    cfg = TunerConfig()
    before = RatingParams()
    after = tune_one_night(before, NightOutcomes(load_bearing=100), cfg)
    assert abs(after.urgency_base - before.urgency_base) <= cfg.delta_bound + 1e-12
    assert abs(after.load_base - before.load_base) <= cfg.delta_bound + 1e-12


def test_tune_one_night_is_pure() -> None:
    before = RatingParams()
    _ = tune_one_night(before, NightOutcomes(ignored=10), TunerConfig())
    assert before.urgency_base == 0.5  # input untouched


# --- AC1: all stability assertions pass under clamps, incl. pathological runs -----------


@pytest.mark.parametrize("label", ["mixed", "all_load_bearing", "all_ignored"])
def test_clamped_run_passes_all_stability_assertions(label: str) -> None:
    schedule = dict(_clamped_schedules())[label]
    result = run_simulation(schedule, TunerConfig(), label)
    check = evaluate_stability(result)
    assert check.all_passed, f"{label} failed: {check.failures()}"


def test_clamped_runs_specific_metrics() -> None:
    cfg = TunerConfig()
    for label, schedule in _clamped_schedules():
        result = run_simulation(schedule, cfg, label)
        # max per-night delta within bound
        assert result.max_abs_delta <= cfg.delta_bound + 1e-12
        # surfacings never exceed the hard ceiling
        assert result.max_surfacings <= HARD_SURFACING_CEILING + 1e-12
        # converged: back-half variance under threshold
        assert result.second_half_param_variance() < VARIANCE_THRESHOLD
        # params never cross floor/ceiling
        for r in result.records:
            assert cfg.urgency_floor <= r.urgency_base <= cfg.urgency_ceiling
            assert cfg.load_floor <= r.load_base <= cfg.load_ceiling


def test_numeric_report_is_produced() -> None:
    result = run_simulation(all_load_bearing_schedule(N), TunerConfig(), "all_load_bearing")
    report = format_report(result, evaluate_stability(result))
    assert "max surfacings / day" in report
    assert "ALL PASSED" in report


def test_simulation_is_deterministic() -> None:
    s = mixed_schedule(N)
    a = run_simulation(s, TunerConfig(), "a")
    b = run_simulation(s, TunerConfig(), "b")
    assert [r.urgency_base for r in a.records] == [r.urgency_base for r in b.records]


# --- AC2: ablation — removing clamps breaks at least one assertion ----------------------


def test_ablation_all_load_bearing_breaks_a_stability_assertion() -> None:
    ablated = run_simulation(
        all_load_bearing_schedule(N),
        TunerConfig(clamps_enabled=False),
        "all_load_bearing (no clamps)",
    )
    check = evaluate_stability(ablated)
    assert not check.all_passed
    assert check.failures(), "ablation should break >= 1 assertion (clamps load-bearing)"


def test_ablation_breaks_surfacing_ceiling_specifically() -> None:
    # With no clamps, all-load-bearing drives urgency up and load down unbounded, so the
    # synthetic surfacing rate blows past the hard ceiling.
    ablated = run_simulation(
        all_load_bearing_schedule(N), TunerConfig(clamps_enabled=False), "ablate"
    )
    assert ablated.max_surfacings > HARD_SURFACING_CEILING


def test_clamps_are_load_bearing_contrast() -> None:
    # The SAME schedule passes with clamps and fails without — the clamps are the cause.
    schedule = all_load_bearing_schedule(N)
    clamped = evaluate_stability(run_simulation(schedule, TunerConfig(), "c"))
    ablated = evaluate_stability(
        run_simulation(schedule, TunerConfig(clamps_enabled=False), "a")
    )
    assert clamped.all_passed
    assert not ablated.all_passed
