"""Production operating-parameter store (TK-13, EP-9).

ONE typed, versioned home for wombat's GLOBAL, STATIC operating constants — the values the
de-risk spikes (TK-22/26/48/73) recommend but no other ticket persists. The gate (TK-27/28),
the spend ledger (TK-9), the morning-brief timer (TK-97) and the RatingTuner (TK-49) read
their thresholds/ceilings/min-age/clamp-bounds from HERE instead of scattered magic numbers.

Distinct, by design, from two neighbours:
  * ``WombatConfig`` (``wombat.config``) — the pydantic-settings ENV loader for the DeepSeek
    egress credentials. Process credentials from env, NOT operating constants. They MUST NOT
    share a module or a type name (audit collision fix).
  * ``RatingParams`` (``wombat.rating.params``) — the PER-EVENT-CLASS, ADAPTIVE values the
    nightly tuner writes into the cog-worx user scope. Those are personalization; THESE are
    the static global operating config.

The source of truth is the human-edited ``wombat_params.yaml`` next to this module. It is
loaded into a FROZEN ``OperatingParams`` — there is no mutation API and no writer here, so
this store is structurally incapable of being written by the nightly tuner (AC5). A missing
or mistyped field fails LOUD at load time (``extra="forbid"`` + required fields); there is no
silent default in code — the documented defaults live in the versioned YAML.

TK-302 (DEC-67(d)/(h)): ``load_operating_params``'s keyword-only ``overlay`` applies, AFTER file
validation, a boot-time app-editable overlay of the EIGHT ``PARAMS_APP_EDITABLE`` keys sourced
from ``wombat_settings`` (``wombat.runtime.serve``) — the YAML file keeps fail-loud custody as
the complete default source; the overlay is a restart-tier, clamp-and-skip, never-fatal veneer
on top of it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from datetime import time
from importlib import resources
from pathlib import Path
from typing import Any, NamedTuple

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

# Bump in lock-step with wombat_params.yaml's ``version`` whenever a field is added, removed,
# or renamed, so a persisted file can be reconciled against the code's expectation.
# v8 (TK-301, DEC-67(c)): personality_band gained the required "eager" field.
OPERATING_PARAMS_VERSION = 8

_PARAMS_FILENAME = "wombat_params.yaml"

logger = logging.getLogger(__name__)


class OperatingParamsError(RuntimeError):
    """Raised when the operating-parameter file is absent, unparseable, or invalid."""


class RatingTunerBounds(BaseModel):
    """The TK-48 (RISK-4) bounded-update block — FIVE values that move as ONE block.

    The TK-48 ablation proved the floor/ceiling clamps ALONE do not bound the daily surfacing
    RATE: the clamp band must be chosen JOINTLY with the surfacing sensitivity so the
    worst-case clamped rate lands exactly at ``surfacing_ceiling_per_day``. Changing the band
    without re-deriving the ceiling breaks the bound — hence one coherent block, not five free
    knobs. The tuner (TK-49) clamps against these; it does not own them (owned here).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    clamp_floor: float  # per-param floor, applies to both urgency and load (LOCKED 0.35)
    clamp_ceiling: float  # per-param ceiling, applies to both urgency and load (LOCKED 0.65)
    delta_bound: float  # max per-night change to any one parameter (LOCKED 0.05)
    gain: float  # tuner learning gain (LOCKED 0.20)
    surfacing_ceiling_per_day: float  # hard daily surfacing ceiling (LOCKED 12.0)


class PersonalityBand(BaseModel):
    """TK-215 (DEC-37(a), Q-107(a)): the bounded deterministic ``urgency_threshold`` offset per
    ``Proactivity`` level — the ONE persona axis with gate-side actuation, zero LLM (NG-4/CON-1).

    ``minimal``/``balanced``/``forward``/``eager`` (TK-301, DEC-67(c)) are the per-level offsets
    ADDED to the base ``urgency_threshold`` (``gate.trigger.effective_urgency_threshold``);
    ``floor``/``cap`` clamp the result so no level can push the effective threshold outside a
    bounded band. Human-edited only, same custody as every other gate constant here — the
    RatingTuner never writes this block (non_goal).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    minimal: float  # offset at Proactivity.MINIMAL (PROVISIONAL, >=0 -> raises the threshold)
    balanced: float  # offset at Proactivity.BALANCED (PROVISIONAL, 0.0 = today's gate exactly)
    forward: float  # offset at Proactivity.FORWARD (PROVISIONAL, <=0 -> lowers the threshold)
    # eager: offset at Proactivity.EAGER (TK-301, DEC-67(c), PROVISIONAL) -- <= forward, lowers
    # the threshold further still. NO Python default: the YAML is the source, so a file that
    # predates this field fails loud at load rather than silently defaulting.
    eager: float
    floor: float  # the effective threshold never drops below this (PROVISIONAL)
    cap: float  # the effective threshold never exceeds this (PROVISIONAL)


class OperatingParams(BaseModel):
    """Typed, frozen view of wombat's static production operating constants (TK-13).

    Every field is REQUIRED with no Python default: the documented defaults live in
    ``wombat_params.yaml``, and a field missing from that file is a load-time error rather
    than a silent code default (AC1). Frozen + no writer (AC5) — the nightly tuner cannot
    write here.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Bumped in the YAML on any value change so the operating config is auditable (AC5).
    version: int

    # --- Gate trigger thresholds (TK-26, PROVISIONAL) ---
    urgency_threshold: float
    load_flush_threshold: float
    per_class_daily_ceiling: int

    # --- Pending-set / flush mechanics ---
    flush_min_age_seconds: float  # TK-27 flush-arm min-age guard (audit gap)
    decay_ttl_seconds: float
    max_pending: int  # the MAX_PENDING cap on the pending-set size

    # --- Spend ledger (TK-9) — two layers: the wombat-owned daily token ceiling (layer 2, the
    # DailySpendLedger) and cog-worx's per-drive-segment BudgetPolicy ceilings (layer 1, the
    # inner backstop; CF-3.0-B cumulative-per-run remains deferred in cog-worx) ---
    mouth_daily_token_ceiling: int
    mouth_max_usd_per_drive: float
    mouth_max_calls_per_drive: int

    # --- Mouth model-call timeout (TK-283, DEC-61) — the ONE tunable shared by every model-
    # calling mouth site (compose/brief_compose/speech_shape/reflection_compose/draft_composer);
    # the promptness guarantee belongs to the fallback, not the model wait ---
    mouth_model_timeout_seconds: float

    # --- Morning brief (TK-97) — the single fixed value; no runtime knob ---
    morning_brief_time: time

    # --- Nightly dream (TK-52) — the single fixed value; no runtime knob (mirrors morning_
    # brief_time above; TK-52's non_goal caps configurability at this one constant) ---
    nightly_dream_time: time

    # --- RatingTuner bounded-update block (TK-48, LOCKED) ---
    rating_tuner: RatingTunerBounds

    # --- Personality band (TK-215, DEC-37(a)/Q-107(a), PROVISIONAL) ---
    personality_band: PersonalityBand

    # --- Presence hold (TK-11) ---
    presence_staleness_ceiling_seconds: float
    presence_confidence_floor: float
    presence_idle_threshold_seconds: float

    # --- Sweeper cadence (TK-53) — the standing runtime's durable-timer poll loop ---
    sweeper_interval_seconds: float
    sweeper_lease_ttl_seconds: float

    # --- Dream substrate budget (TK-54) — DEC-23 bounded off-path inference; the per-drive-
    # segment BudgetGuard ceiling wombat.pathways.dream_substrate.build_dream_substrate wires ---
    dream_budget_max_usd: float
    dream_budget_max_calls: int


def _default_params_path() -> Path:
    """Resolve the packaged ``wombat_params.yaml`` (works editable and from a wheel)."""
    return Path(str(resources.files("wombat").joinpath(_PARAMS_FILENAME)))


def _parse_time_of_day(raw: str) -> time:
    return time.fromisoformat(raw)


def _parse_float(raw: str) -> float:
    return float(raw)


def _parse_int(raw: str) -> int:
    return int(raw)


class ParamOverlaySpec(NamedTuple):
    """One row of the closed DEC-67(d) app-editable operating-parameter overlay spec: the
    ``OperatingParams`` field it maps to, the str -> value parser, and the inclusive
    ``[min, max]`` clamp band. ``min``/``max`` are both ``None`` for the two HH:MM:SS time
    fields, which admit any valid time with no numeric band."""

    field: str
    parser: Callable[[str], Any]
    min: float | None
    max: float | None


# DEC-67(d): the closed, frozen spec for wombat's EIGHT app-editable operating-parameter
# overlay keys — the ONLY ``OperatingParams`` fields a settings-table row may override at boot
# (``load_operating_params``'s ``overlay=`` below). Every other field (``rating_tuner``,
# ``personality_band``, ``flush_min_age_seconds``, ``presence_*``, ``sweeper_*``,
# ``dream_budget_*``, ...) is structurally absent from this mapping — the human-edited YAML
# stays their ONLY source, by construction (non_goal: no overlay of any non-enumerated field).
PARAMS_APP_EDITABLE: Mapping[str, ParamOverlaySpec] = {
    "wombat_param_morning_brief_time": ParamOverlaySpec(
        "morning_brief_time", _parse_time_of_day, None, None
    ),
    "wombat_param_nightly_dream_time": ParamOverlaySpec(
        "nightly_dream_time", _parse_time_of_day, None, None
    ),
    "wombat_param_urgency_threshold": ParamOverlaySpec(
        "urgency_threshold", _parse_float, 0.60, 0.95
    ),
    "wombat_param_per_class_daily_ceiling": ParamOverlaySpec(
        "per_class_daily_ceiling", _parse_int, 0, 10
    ),
    "wombat_param_decay_ttl_seconds": ParamOverlaySpec(
        "decay_ttl_seconds", _parse_float, 3600.0, 604800.0
    ),
    "wombat_param_mouth_model_timeout_seconds": ParamOverlaySpec(
        "mouth_model_timeout_seconds", _parse_float, 2.0, 60.0
    ),
    "wombat_param_mouth_daily_token_ceiling": ParamOverlaySpec(
        "mouth_daily_token_ceiling", _parse_int, 10000, 1000000
    ),
    "wombat_param_mouth_max_usd_per_drive": ParamOverlaySpec(
        "mouth_max_usd_per_drive", _parse_float, 0.05, 5.00
    ),
}


def _clamp(value: Any, spec: ParamOverlaySpec) -> tuple[Any, bool]:
    """Clamp ``value`` into ``spec``'s inclusive band; ``(value, False)`` unchanged when the spec
    carries no band (the two time fields) or ``value`` already sits inside it."""
    if spec.min is None or spec.max is None:
        return value, False
    if value < spec.min:
        return spec.min, True
    if value > spec.max:
        return spec.max, True
    return value, False


def load_operating_params(
    path: Path | None = None,
    *,
    overlay: Mapping[str, str] | None = None,
) -> OperatingParams:
    """Load + validate the operating parameters from the versioned YAML, or fail LOUD.

    Reads the human-edited source-of-truth (the packaged ``wombat_params.yaml`` unless an
    explicit ``path`` is given). A missing file, non-mapping content, or any missing/mistyped/
    unexpected field raises ``OperatingParamsError`` — never a silent default (AC1). The FILE
    keeps fail-loud custody as the complete default source.

    ``overlay`` (DEC-67(d), applied AFTER file validation) is the boot-time app-editable
    operating-parameter overlay: every key present in ``PARAMS_APP_EDITABLE`` is parsed with its
    spec's parser and clamped into its ``[min, max]`` band. An unparseable value is DROPPED with
    ONE ``logger.warning`` naming the key and raw value (never fatal); a parseable but
    out-of-band value is clamped to the nearer bound with ONE ``logger.warning`` naming the key,
    the parsed value, and the clamp result. A key absent from ``PARAMS_APP_EDITABLE`` is ignored
    silently (callers are expected to have already filtered to the eight admitted keys). No
    overlay (``None`` or empty) returns the file-validated model completely unchanged (byte-equal
    ``model_dump()``, AC1).
    """
    src = path or _default_params_path()
    try:
        text = src.read_text(encoding="utf-8")
    except OSError as exc:
        raise OperatingParamsError(f"operating-parameter file not readable: {src}") from exc

    raw = yaml.safe_load(text)
    if not isinstance(raw, dict):
        raise OperatingParamsError(
            f"operating-parameter file {src} must contain a YAML mapping, got {type(raw).__name__}"
        )

    try:
        params = OperatingParams.model_validate(raw)
    except ValidationError as exc:
        raise OperatingParamsError(f"invalid operating-parameter file {src}: {exc}") from exc

    if not overlay:
        return params

    updates: dict[str, Any] = {}
    for key, raw_value in overlay.items():
        spec = PARAMS_APP_EDITABLE.get(key)
        if spec is None:
            continue
        try:
            parsed = spec.parser(raw_value)
        except (TypeError, ValueError):
            logger.warning(
                "wombat_settings contains an unparseable operating-parameter overlay value for "
                "%s: %r; skipping this row (the file's own value is kept)",
                key,
                raw_value,
            )
            continue
        clamped, was_clamped = _clamp(parsed, spec)
        if was_clamped:
            logger.warning(
                "wombat_settings operating-parameter overlay value for %s (%r) is outside "
                "[%s, %s]; clamping to %r",
                key,
                parsed,
                spec.min,
                spec.max,
                clamped,
            )
        updates[spec.field] = clamped

    if not updates:
        return params
    return params.model_copy(update=updates)
