"""Brief-schedule domain: the once-daily fire fence + the pure fire-time math (TK-97, EP-1, Q-80).

Two independent, side-effect-narrow pieces the ``brief_timer`` stage composes:

  * ``BriefRunLedger`` — the exactly-once-per-wombat-day fence, a THIN wrapper over the shared
    Postgres-backed ``DailyLedger`` (TK-152) under the fixed ``ledger_name`` ``"brief:run"``
    (mirrors ``DailySpendLedger``'s ``"spend:tokens"`` pattern exactly). ``ran_today()`` reads
    today's row (``value >= 1``); ``mark_ran()`` increments it. A new wombat-day is a fresh ``0``
    row BY CONSTRUCTION (the ``(ledger_name, wombat_date)`` composite PK — TK-28 precedent), so
    "rollover" needs no logic here: the day after a fire, ``current_row('brief:run')`` resolves a
    different key and reads ``0`` again. Restart-durability + the midnight-straddle boundary come
    free from the injected ``DailyLedger`` (DEC-21).

  * ``next_fire_at`` / ``is_due`` — PURE tz-aware time math (no clock read, no I/O; the caller
    injects the ``now`` instant). ``next_fire_at`` returns the next ABSOLUTE instant at
    ``brief_time`` in ``tz`` STRICTLY after ``now`` (a Wait's ``wake_at`` must be absolute, per the
    cog-worx ``Wait`` seam); ``is_due`` answers "is ``now`` at or past today's ``brief_time``".
    ``datetime.combine(date, time, tzinfo=tz)`` yields ``fold=0`` and normalises DST both
    directions by construction; all comparisons are by absolute instant, so a ``now`` in any zone
    (the runtime supplies UTC) resolves correctly against a ``tz``-local fire instant.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from wombat.domain.daily_ledger import DailyLedger

# The fixed ``daily_ledger`` row this fence rides — mirrors ``DailySpendLedger.LEDGER_NAME``
# ("spend:tokens"). No new table, no new migration: rides ``daily_ledger`` (migration 003).
LEDGER_NAME = "brief:run"


class BriefRunLedger:
    """The once-per-wombat-day brief fence, riding the shared ``DailyLedger`` (TK-152/TK-28)."""

    def __init__(self, ledger: DailyLedger) -> None:
        self._ledger = ledger

    def ran_today(self) -> bool:
        """True iff the brief has already fired this wombat-day (today's ``brief:run`` row >= 1).

        Creates today's row at ``0`` if it doesn't exist yet (``current_row`` semantics) — a fresh
        wombat-day is a distinct ``(ledger_name, wombat_date)`` key, so this is ``False`` again the
        morning after a fire with no reset logic here (TK-28 precedent).
        """
        return self._ledger.current_row(LEDGER_NAME).value >= 1

    def mark_ran(self) -> int:
        """Record that the brief fired today; returns the post-increment count for today."""
        return self._ledger.increment(LEDGER_NAME).value


def _fire_instant_on(day: date, tz: ZoneInfo, brief_time: time) -> datetime:
    """The absolute instant of ``brief_time`` on ``day`` in ``tz`` (fold=0, DST-normalised).

    ``datetime.combine`` attaches ``tz`` at ``fold=0``; a nonexistent (spring-forward gap) or
    ambiguous (fall-back) wall time is resolved deterministically by the ``fold=0`` rule — this
    module never needs a special case because it only ever compares/returns absolute instants.
    """
    return datetime.combine(day, brief_time, tzinfo=tz)


def is_due(now: datetime, tz: ZoneInfo, brief_time: time) -> bool:
    """True iff ``now`` is at or past today's ``brief_time`` fire instant (in ``tz``).

    "Today" is the ``tz``-local civil date of ``now`` (not ``now``'s own zone's date), so a ``now``
    just after midnight UTC that is still "yesterday evening" locally resolves the correct local
    day. Comparison is by absolute instant.
    """
    local_today = now.astimezone(tz).date()
    return now >= _fire_instant_on(local_today, tz, brief_time)


def next_fire_at(now: datetime, tz: ZoneInfo, brief_time: time) -> datetime:
    """The next ABSOLUTE fire instant at ``brief_time`` in ``tz``, STRICTLY after ``now``.

    Today's fire instant if it is still ahead of ``now`` (``now`` earlier than ``brief_time``);
    otherwise tomorrow's (``now`` at or past today's ``brief_time`` — the "already past today"
    rollover). Strictness (``> now``, not ``>=``) means a fire landing exactly ON ``brief_time``
    re-parks for the NEXT day, never the same instant again (a load-bearing exactly-once guard: the
    ``brief_timer`` stage calls this with ``now == ctx.clock()`` right after firing).
    """
    local_today = now.astimezone(tz).date()
    today_fire = _fire_instant_on(local_today, tz, brief_time)
    if today_fire > now:
        return today_fire
    return _fire_instant_on(local_today + timedelta(days=1), tz, brief_time)


__all__ = ["LEDGER_NAME", "BriefRunLedger", "is_due", "next_fire_at"]
