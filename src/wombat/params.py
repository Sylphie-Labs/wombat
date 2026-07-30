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
"""

from __future__ import annotations

from datetime import time
from importlib import resources
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

# Bump in lock-step with wombat_params.yaml's ``version`` whenever a field is added, removed,
# or renamed, so a persisted file can be reconciled against the code's expectation.
# v8 (TK-301, DEC-67(c)): personality_band gained the required "eager" field.
OPERATING_PARAMS_VERSION = 8

_PARAMS_FILENAME = "wombat_params.yaml"


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


def load_operating_params(path: Path | None = None) -> OperatingParams:
    """Load + validate the operating parameters from the versioned YAML, or fail LOUD.

    Reads the human-edited source-of-truth (the packaged ``wombat_params.yaml`` unless an
    explicit ``path`` is given). A missing file, non-mapping content, or any missing/mistyped/
    unexpected field raises ``OperatingParamsError`` — never a silent default (AC1).
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
        return OperatingParams.model_validate(raw)
    except ValidationError as exc:
        raise OperatingParamsError(f"invalid operating-parameter file {src}: {exc}") from exc
