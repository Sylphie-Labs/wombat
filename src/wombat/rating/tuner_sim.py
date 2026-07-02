"""SPIKE — RatingTuner bounded-stability simulation (TK-48, RISK-4, EP-14).

THROWAWAY de-risk spike. Standalone, model-free, no cog-worx / Neo4j / Postgres I/O.
It simulates ``N`` consecutive nightly tuning passes over a *synthetic* outcome corpus and
proves the de-risk hypothesis:

    A proportional RatingTuner with a per-night delta bound + floor/ceiling clamps keeps
    surfacings/day inside the hard ceiling and converges (parameter variance < threshold
    after N/2 nights); REMOVING the clamps breaks at least one stability assertion.

This is a prototype (``engineering_level: prototype``): a naive proportional tuner stub +
synthetic generator + assertion harness producing a numeric report. It does NOT implement
the production tuner (TK-49); it *recommends* the bounds the production tuner will clamp
against (persisted by TK-13).

Everything here is pure and deterministic — no RNG, no clock, no model call (NG-4). Run as
``python -m wombat.rating.tuner_sim`` to print the numeric stability report.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from wombat.rating.params import RatingParams

# ---------------------------------------------------------------------------------------
# Synthetic outcome vocabulary
# ---------------------------------------------------------------------------------------


class Outcome(Enum):
    """Realized terminal outcome of a surfaced item (synthetic, for the spike only).

    LOAD_BEARING  the surfacing mattered  -> tuner should keep/raise urgency.
    IGNORED       the surfacing was noise -> tuner should mute (raise load, lower urgency).
    REGRETTED     the surfacing was wrong -> strongest mute signal.
    """

    LOAD_BEARING = "load_bearing"
    IGNORED = "ignored"
    REGRETTED = "regretted"


@dataclass(frozen=True, slots=True)
class NightOutcomes:
    """One synthetic night's outcome corpus for a single event class: an outcome histogram."""

    load_bearing: int = 0
    ignored: int = 0
    regretted: int = 0

    @property
    def total(self) -> int:
        return self.load_bearing + self.ignored + self.regretted

    def net_signal(self) -> float:
        """Net outcome signal in [-1.0, 1.0].

        +1 = every outcome load-bearing (surface more); -1 = every outcome a regret/ignore
        (surface less). Empty night => 0.0 (no signal). This is the error term the
        proportional tuner reacts to.
        """
        if self.total == 0:
            return 0.0
        positive = self.load_bearing
        negative = self.ignored + self.regretted
        return (positive - negative) / self.total


# ---------------------------------------------------------------------------------------
# Tuner configuration — the clamp design under test
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TunerConfig:
    """Operating constants for the proportional tuner. These are what the spike recommends.

    ``delta_bound``        max absolute change to any single parameter per night.
    ``gain``               proportional gain: raw_delta = gain * net_signal.
    ``urgency_floor/ceil`` clamp range for urgency_base (the loudness lever).
    ``load_floor/ceil``    clamp range for load_base (the mute lever).
    ``clamps_enabled``     ABLATION SWITCH. When False, neither the per-night delta bound
                           NOR the floor/ceiling are applied — used to prove the clamps are
                           load-bearing (AC2 of the spike).

    KEY RECOMMENDATION OF THE SPIKE: the clamp band is NOT free — it must be tight enough
    that even the loudest clamped param set stays under the hard surfacing ceiling. With the
    surfacing model below, the worst case is (urgency_ceiling - load_floor); these defaults
    keep that at 0.10 => exactly HARD_SURFACING_CEILING surfacings/day at saturation, never
    above. This is the operating-constant recommendation TK-49/TK-13 should persist.
    """

    delta_bound: float = 0.05
    gain: float = 0.20
    urgency_floor: float = 0.35
    urgency_ceiling: float = 0.65
    load_floor: float = 0.35
    load_ceiling: float = 0.65
    clamps_enabled: bool = True


# ---------------------------------------------------------------------------------------
# Surfacing model — synthetic, deterministic
# ---------------------------------------------------------------------------------------

# Hard operating ceiling: the gate must never surface more than this many items/day for a
# class. The whole point of the clamps is to keep the simulated surfacing rate under it.
HARD_SURFACING_CEILING = 12.0

# How many candidate items/day the class generates (synthetic constant).
_DAILY_CANDIDATES = 20.0

# Surfacing sensitivity: how strongly the net score (urgency_base - load_base) moves the
# surfaced fraction. Chosen with the clamp band so the worst clamped case stays under the
# hard ceiling: at the default band net_max = ceiling - floor = 0.30, and
#   _DAILY_CANDIDATES * (0.5 + _SURFACING_SENSITIVITY * 0.30) = 20 * (0.5 + 0.1) = 12.0.
_SURFACING_SENSITIVITY = 1.0 / 3.0


def surfacings_per_day(params: RatingParams) -> float:
    """Deterministic synthetic surfacing rate for a parameter set.

    Model: an item surfaces when its net score (urgency_base minus load_base) is positive;
    the fraction of the day's candidates that surface rises with urgency_base and falls
    with load_base. Monotone in both levers, bounded to [0, _DAILY_CANDIDATES].

    This is the feedback channel the tuner steers: raising urgency_base (or lowering
    load_base) increases surfacings; the clamps cap how far/fast that can go.
    """
    net = params.urgency_base - params.load_base  # in [-1, 1]
    fraction = max(0.0, min(1.0, 0.5 + _SURFACING_SENSITIVITY * net))
    return _DAILY_CANDIDATES * fraction


# ---------------------------------------------------------------------------------------
# The proportional tuner stub
# ---------------------------------------------------------------------------------------


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def tune_one_night(
    params: RatingParams,
    outcomes: NightOutcomes,
    config: TunerConfig,
) -> RatingParams:
    """Apply one nightly proportional update to ``params`` given a night's outcomes.

    Direction: a positive net signal (load-bearing) => surface MORE => raise urgency_base
    and lower load_base. A negative net signal (ignored/regretted) => surface LESS => lower
    urgency_base and raise load_base. Symmetric, so the two levers move in opposition.

    Bounding (when ``config.clamps_enabled``):
      1. the raw proportional delta is clipped to +/- ``delta_bound`` (per-night bound);
      2. the resulting parameter is clamped to its [floor, ceiling].

    With clamps disabled, the raw unbounded delta is applied and no floor/ceiling holds —
    this is the ablation path that the harness proves unstable.

    Pure function: no I/O, no mutation of the input (frozen dataclass in, new one out).
    """
    signal = outcomes.net_signal()
    raw_delta = config.gain * signal

    if config.clamps_enabled:
        delta = _clamp(raw_delta, -config.delta_bound, config.delta_bound)
        new_urgency = _clamp(
            params.urgency_base + delta, config.urgency_floor, config.urgency_ceiling
        )
        new_load = _clamp(
            params.load_base - delta, config.load_floor, config.load_ceiling
        )
    else:
        # Ablation: no per-night bound, no floor/ceiling. Raw deltas accumulate freely.
        new_urgency = params.urgency_base + raw_delta
        new_load = params.load_base - raw_delta

    return params.with_updates(urgency_base=new_urgency, load_base=new_load)


# ---------------------------------------------------------------------------------------
# Simulation harness
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NightRecord:
    """Per-night audit row captured during a simulation run."""

    night: int
    urgency_base: float
    load_base: float
    urgency_delta: float
    load_delta: float
    surfacings: float


@dataclass(frozen=True, slots=True)
class SimResult:
    """Outcome of an N-night simulation run, with the metrics the assertions check."""

    label: str
    config: TunerConfig
    records: tuple[NightRecord, ...] = field(default_factory=tuple)

    @property
    def nights(self) -> int:
        return len(self.records)

    @property
    def max_abs_delta(self) -> float:
        """Largest single-parameter change observed on any night (0.0 if no nights)."""
        if not self.records:
            return 0.0
        return max(max(abs(r.urgency_delta), abs(r.load_delta)) for r in self.records)

    @property
    def max_surfacings(self) -> float:
        if not self.records:
            return 0.0
        return max(r.surfacings for r in self.records)

    def second_half_param_variance(self) -> float:
        """Max variance of urgency_base/load_base over the SECOND half of the run.

        Convergence proxy: after N/2 nights a bounded tuner should have settled, so the
        spread of the parameters in the back half is small. We report the larger of the two
        levers' variances.
        """
        if self.nights < 2:
            return 0.0
        half = self.nights // 2
        tail = self.records[half:]
        return max(
            _variance([r.urgency_base for r in tail]),
            _variance([r.load_base for r in tail]),
        )


def _variance(values: list[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    return sum((v - mean) ** 2 for v in values) / n


def run_simulation(
    outcome_schedule: list[NightOutcomes],
    config: TunerConfig,
    label: str,
    start: RatingParams | None = None,
) -> SimResult:
    """Run the tuner over a fixed schedule of nightly outcomes; capture a per-night audit.

    Deterministic: same schedule + config + start => identical result. ``start`` defaults to
    the neutral baseline ``RatingParams()``.
    """
    params = start if start is not None else RatingParams()
    records: list[NightRecord] = []
    for i, outcomes in enumerate(outcome_schedule):
        before = params
        params = tune_one_night(before, outcomes, config)
        records.append(
            NightRecord(
                night=i + 1,
                urgency_base=params.urgency_base,
                load_base=params.load_base,
                urgency_delta=params.urgency_base - before.urgency_base,
                load_delta=params.load_base - before.load_base,
                surfacings=surfacings_per_day(params),
            )
        )
    return SimResult(label=label, config=config, records=tuple(records))


# ---------------------------------------------------------------------------------------
# Synthetic schedules (incl. the pathological runs the poc names)
# ---------------------------------------------------------------------------------------


def all_load_bearing_schedule(nights: int, count: int = 10) -> list[NightOutcomes]:
    """Pathological run: every outcome every night is LOAD_BEARING (max 'surface more')."""
    return [NightOutcomes(load_bearing=count) for _ in range(nights)]


def all_ignored_schedule(nights: int, count: int = 10) -> list[NightOutcomes]:
    """Pathological run: every outcome every night is IGNORED (max 'surface less')."""
    return [NightOutcomes(ignored=count) for _ in range(nights)]


def mixed_schedule(nights: int) -> list[NightOutcomes]:
    """A realistic-ish oscillating mix that should converge under the clamps.

    Alternates a load-bearing-leaning night with an ignored-leaning night so the raw signal
    flip-flops; a stable tuner must damp this into a small back-half variance rather than
    oscillate.
    """
    schedule: list[NightOutcomes] = []
    for i in range(nights):
        if i % 2 == 0:
            schedule.append(NightOutcomes(load_bearing=7, ignored=3))
        else:
            schedule.append(NightOutcomes(load_bearing=3, ignored=6, regretted=1))
    return schedule


# ---------------------------------------------------------------------------------------
# Stability assertions
# ---------------------------------------------------------------------------------------

VARIANCE_THRESHOLD = 1e-3


@dataclass(frozen=True, slots=True)
class StabilityCheck:
    """Result of evaluating the four stability assertions against a SimResult."""

    label: str
    delta_within_bound: bool
    params_within_clamps: bool
    surfacings_within_ceiling: bool
    converged: bool

    @property
    def all_passed(self) -> bool:
        return (
            self.delta_within_bound
            and self.params_within_clamps
            and self.surfacings_within_ceiling
            and self.converged
        )

    def failures(self) -> list[str]:
        out: list[str] = []
        if not self.delta_within_bound:
            out.append("delta_within_bound")
        if not self.params_within_clamps:
            out.append("params_within_clamps")
        if not self.surfacings_within_ceiling:
            out.append("surfacings_within_ceiling")
        if not self.converged:
            out.append("converged")
        return out


def evaluate_stability(result: SimResult) -> StabilityCheck:
    """Evaluate the four de-risk assertions against a completed run.

    1. max delta per night <= configured bound.
    2. no param crosses its floor/ceiling.
    3. surfacings/day never exceeds HARD_SURFACING_CEILING.
    4. parameter variance over the second half < VARIANCE_THRESHOLD (convergence).
    """
    cfg = result.config
    delta_ok = result.max_abs_delta <= cfg.delta_bound + 1e-12

    params_ok = all(
        cfg.urgency_floor - 1e-12 <= r.urgency_base <= cfg.urgency_ceiling + 1e-12
        and cfg.load_floor - 1e-12 <= r.load_base <= cfg.load_ceiling + 1e-12
        for r in result.records
    )

    surfacings_ok = result.max_surfacings <= HARD_SURFACING_CEILING + 1e-12
    converged = result.second_half_param_variance() < VARIANCE_THRESHOLD

    return StabilityCheck(
        label=result.label,
        delta_within_bound=delta_ok,
        params_within_clamps=params_ok,
        surfacings_within_ceiling=surfacings_ok,
        converged=converged,
    )


# ---------------------------------------------------------------------------------------
# Numeric report
# ---------------------------------------------------------------------------------------


def format_report(result: SimResult, check: StabilityCheck) -> str:
    """Render a compact numeric stability report for one run."""
    lines = [
        f"=== {result.label} (clamps_enabled={result.config.clamps_enabled}) ===",
        f"  nights                  : {result.nights}",
        f"  max |delta| / night     : {result.max_abs_delta:.4f}"
        f"  (bound {result.config.delta_bound:.4f})",
        f"  max surfacings / day    : {result.max_surfacings:.4f}"
        f"  (ceiling {HARD_SURFACING_CEILING:.2f})",
        f"  back-half param variance: {result.second_half_param_variance():.2e}"
        f"  (threshold {VARIANCE_THRESHOLD:.0e})",
        f"  final urgency_base      : {result.records[-1].urgency_base:.4f}"
        if result.records
        else "  final urgency_base      : n/a",
        f"  final load_base         : {result.records[-1].load_base:.4f}"
        if result.records
        else "  final load_base         : n/a",
        f"  delta_within_bound      : {check.delta_within_bound}",
        f"  params_within_clamps    : {check.params_within_clamps}",
        f"  surfacings_within_ceil  : {check.surfacings_within_ceiling}",
        f"  converged               : {check.converged}",
        f"  ALL PASSED              : {check.all_passed}",
    ]
    return "\n".join(lines)


def main() -> None:
    """Run the spike: clamped runs prove stability; an ablation run proves clamps matter."""
    nights = 30
    schedules = [
        ("mixed", mixed_schedule(nights)),
        ("all_load_bearing", all_load_bearing_schedule(nights)),
        ("all_ignored", all_ignored_schedule(nights)),
    ]
    clamped = TunerConfig()

    print(f"RatingTuner bounded-stability spike (TK-48 / RISK-4) — N={nights} nights\n")
    for label, schedule in schedules:
        result = run_simulation(schedule, clamped, label)
        check = evaluate_stability(result)
        print(format_report(result, check))
        print()

    # Ablation: remove the clamps on the worst-case (all_load_bearing) schedule.
    ablated_cfg = TunerConfig(clamps_enabled=False)
    ablated = run_simulation(
        all_load_bearing_schedule(nights), ablated_cfg, "all_load_bearing (NO CLAMPS)"
    )
    ablated_check = evaluate_stability(ablated)
    print(format_report(ablated, ablated_check))
    print()
    print(
        "ABLATION: with clamps removed, failing assertions = "
        f"{ablated_check.failures() or '[]'} "
        f"(clamps load-bearing: {not ablated_check.all_passed})"
    )


if __name__ == "__main__":
    main()
