"""The canonical quiet-hours predicate (TK-304, DEC-67g).

PURE, load-bearing, zero I/O — mirrors ``presence_hold.py``'s own posture (no clock read of its
own; ``now_local`` is an explicit argument). This is the ONE definition the
``bootstrap.assemble_runtime`` gate_stage wrapper calls to decide whether the immediate-voice
arm should hold for the night (RULING v2.172 r6).

Supports a midnight-spanning window (e.g. ``start="22:00"``, ``end="06:30"``): a window whose
``start`` is not strictly before its ``end`` is treated as wrapping past midnight. ``start`` is
inclusive, ``end`` is exclusive.

``start == end`` or either being blank NEVER holds — a blank field is the documented "feature
off" state (``wombat.config.WombatConfig.wombat_quiet_start``/``wombat_quiet_end``), and an
identical start/end describes a zero-width window, which is degenerate rather than "all day".

Non-blank ``start``/``end`` are assumed already validated ``HH:MM`` (the ``wombat.config``
``_HHMM_OR_BLANK`` type at the env/dotenv/table tier, and ``SettingsUpdate``'s mirror at the
settings-app PUT tier) — this function does no format validation of its own.
"""

from __future__ import annotations

from datetime import datetime, time

_TIME_FORMAT = "%H:%M"


def _parse_hhmm(value: str) -> time:
    return datetime.strptime(value, _TIME_FORMAT).time()


def in_quiet_hours(now_local: time, start: str, end: str) -> bool:
    """Return ``True`` (hold the immediate-voice arm) iff ``now_local`` falls within the
    ``[start, end)`` window described by ``start``/``end`` (``HH:MM``, possibly midnight-
    spanning). ``False`` (never hold) when either is blank or they are equal."""
    if not start or not end or start == end:
        return False
    start_t = _parse_hhmm(start)
    end_t = _parse_hhmm(end)
    if start_t < end_t:
        return start_t <= now_local < end_t
    # start_t > end_t: the window spans midnight (e.g. 22:00 -> 06:30).
    return now_local >= start_t or now_local < end_t


__all__ = ["in_quiet_hours"]
