"""wombat.devices.biometric_projection — Tier 2 (TK-347, R7): ``current_body_state``, ONE bounded
biometric line merged into ``bootstrap.py``'s SAME shared ``asr_context_hook`` closure the
DEC-68(d)(1) precedent already established (``current_activity``, TK-311/TK-323) — chat and voice
payloads only, no second closure, no second channel.

``project_current_body_state(observations, *, clock)`` reads ``wombat_observations``'
``channel='biometric'`` rows (``devices.biometric_ingest``'s closed-projection ledger, TK-341,
byte-untouched here) over a trailing ``_FRESHNESS_WINDOW`` ending at ``clock()``, picks the SINGLE
freshest row (by ``started_at``), and renders it as ONE fixed-shape line of ``field=value`` pairs
drawn ONLY from that row's kind's numeric fields — never a sentence, never free text. This mirrors
``devices.biometric_ingest._KIND_SCHEMAS``' field names (restated here, not imported — the SAME
byte-untouched-elsewhere precedent ``behavior/stages/dream_biometrics.py`` already set for its own
``asleep_minutes``/``bpm`` reads), deliberately DROPPING ``workout``'s ``activity`` enum field: the
line is a fixed-shape sequence of NUMBERS, never a string riding this grounding key, so the
biometric ingest door's free-text refusal (TK-341 §3) is never quietly loosened by what leaves
through this one extra tier.

ABSENT-NOT-WRONG (the whole point of this module): ``observations`` is ``None`` (the
``wombat_observe_biometrics`` consent toggle off — ``bootstrap.py`` only ever constructs
``biometric_observation_store`` when that toggle is on, the SAME toggle-gated construction TK-341's
route already uses; no separate toggle check lives here, by design — no new config field, TK-347
non-goal), zero rows in the freshness window, or ANY exception raised by ``get_window`` all degrade
to ``None`` (never an empty string, never a stale/guessed number) — a wrong body-state claim in the
mouth's prompt is worse than none. An exception additionally logs exactly ONE loud warning (CON-3
parity with ``voice/context_prefetch.py``'s sibling grounding builders).

PINNED CONSTANTS (DEC-63 no-knob precedent — NOT operator-tunable): ``_FRESHNESS_WINDOW`` = 6 hours
— generous enough to still call a periodic (few-times-a-day) biometric sync "current" without
presenting yesterday's reading as now; ``_MAX_BODY_STATE_CHARS`` = 160, the SAME cap value
``voice/context_prefetch.py``'s ``_MAX_ACTIVITY_CHARS`` uses for its own single bounded line.

STRUCTURAL: this module never imports from ``wombat.bootstrap`` — ``bootstrap.py`` imports THIS
module and merges its result into the ONE shared ``asr_context_hook`` closure; the closure is what
keeps this key on chat/voice only (brief/draft/reflection payloads are built elsewhere and never
call it, by construction — see ``bootstrap.py``).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from wombat.observations import ObservationStore

logger = logging.getLogger(__name__)

_CHANNEL = "biometric"

# DEC-63 no-knob precedent — see the module docstring for the rationale behind both values.
_FRESHNESS_WINDOW = timedelta(hours=6)
_MAX_BODY_STATE_CHARS = 160

# The devices.biometric_ingest ledger vocabulary this module reads (byte-untouched there) —
# restated rather than imported (mirrors dream_biometrics.py's own precedent), NUMERIC fields
# only: workout's "activity" enum is deliberately excluded so the rendered line stays a fixed-shape
# sequence of numbers, never a string (see module docstring).
_NUMERIC_FIELDS_BY_KIND: dict[str, tuple[str, ...]] = {
    "sleep_session": ("asleep_minutes", "in_bed_minutes", "awakenings"),
    "workout": (
        "duration_seconds",
        "active_energy_kcal",
        "avg_hr_bpm",
        "max_hr_bpm",
        "distance_meters",
    ),
    "resting_hr_daily": ("bpm",),
    "hrv_daily": ("sdnn_ms",),
    "steps_hourly": ("steps",),
}


def _format_number(value: int | float) -> str:
    """Ints render bare; floats render to one decimal place — deterministic, never scientific
    notation or a variable number of decimals."""
    if isinstance(value, bool):  # defensive: bool is an int subclass, never expected here
        return str(int(value))
    if isinstance(value, int):
        return str(value)
    return f"{value:.1f}"


def _render_row(row: dict[str, Any]) -> str | None:
    """ONE fixed-shape ``"<kind>: field=value field=value"`` line for a single biometric row, or
    ``None`` if the row's ``kind`` is unrecognized or carries none of its kind's numeric fields."""
    kind = row.get("kind")
    fields = _NUMERIC_FIELDS_BY_KIND.get(kind) if isinstance(kind, str) else None
    if not fields:
        return None
    payload = row.get("payload")
    if not isinstance(payload, dict):
        return None
    parts: list[str] = []
    for field_name in fields:
        value = payload.get(field_name)
        if isinstance(value, bool) or not isinstance(value, int | float):
            continue
        parts.append(f"{field_name}={_format_number(value)}")
    if not parts:
        return None
    return f"{kind}: " + " ".join(parts)


def project_current_body_state(
    observations: ObservationStore | None,
    *,
    clock: Callable[[], datetime],
) -> str | None:
    """Return ONE bounded ``current_body_state`` line, or ``None`` (absent-not-wrong — see the
    module docstring). ``observations`` is ``None`` (consent toggle off): returns ``None``
    immediately, no read, no warning."""
    if observations is None:
        return None
    now = clock()
    start = now - _FRESHNESS_WINDOW
    try:
        rows = observations.get_window(_CHANNEL, start, now)
    except Exception:
        logger.warning(
            "project_current_body_state: get_window raised — proceeding with no "
            "current_body_state line",
            exc_info=True,
        )
        return None
    if not rows:
        return None
    freshest = max(rows, key=lambda row: row["started_at"])
    line = _render_row(freshest)
    if line is None:
        return None
    return line[:_MAX_BODY_STATE_CHARS]


__all__ = ["project_current_body_state"]
