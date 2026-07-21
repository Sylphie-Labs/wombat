"""CeilingLedger — the production per-class daily surfacing ceiling (TK-27, EP-9, DEC-13).

Implements ``trigger.CeilingProtocol`` over the shared TK-152 ``DailyLedger`` primitive, so
the surfacing ceiling shares the SAME wombat-day boundary (Q-15) as the mouth-spend ledger
(TK-9) and the morning-brief once-per-day counter (TK-97) instead of re-deriving its own day
math. Keyed per ``EventClass`` (DEC-13's personalization key) via ``ledger_name =
"ceiling:<EventClass.value>"``, so each event class gets its own independent daily count.

No inline threshold literal lives here: ``per_class_daily_ceiling`` is an injected
constructor arg (composition passes ``OperatingParams.per_class_daily_ceiling``; tests pass a
literal directly) — TK-13 owns the value, this module only enforces it.

``FlushDayLatch`` (TK-287, DEC-63b) is the SAME shape over the SAME shared ``DailyLedger`` —
a once-per-wombat-day gate for the load-flush arm, keyed by the fixed ``ledger_name =
"flush:load"`` (one flush per wombat-day, not per event class: DEC-63 rejected a cooldown
tunable, so there is nothing to inject beyond the ledger itself).
"""

from __future__ import annotations

import logging
from datetime import date

from wombat.domain.daily_ledger import DailyLedger
from wombat.rating.params import EventClass

_log = logging.getLogger(__name__)

_LEDGER_PREFIX = "ceiling"
_FLUSH_LEDGER_NAME = "flush:load"


class CeilingLedger:
    """The production ``CeilingProtocol`` implementation over a real ``DailyLedger``."""

    def __init__(self, *, daily_ledger: DailyLedger, per_class_daily_ceiling: int) -> None:
        self._daily_ledger = daily_ledger
        self._per_class_daily_ceiling = per_class_daily_ceiling

    def _ledger_name(self, event_class: EventClass) -> str:
        return f"{_LEDGER_PREFIX}:{event_class.value}"

    def allow(self, event_class: EventClass) -> bool:
        """``True`` iff today's count for ``event_class`` is still below the ceiling."""
        row = self._daily_ledger.current_row(self._ledger_name(event_class))
        return row.value < self._per_class_daily_ceiling

    def record(self, event_class: EventClass) -> None:
        """Book one surfacing of ``event_class`` against today's count."""
        self._daily_ledger.increment(self._ledger_name(event_class))


class FlushDayLatch:
    """The once-per-wombat-day gate for the load-flush arm (TK-287, DEC-63b).

    The exact ``CeilingLedger`` shape (``allow()`` / ``record()``) over the SAME shared
    ``DailyLedger``, keyed by the fixed ``ledger_name = "flush:load"`` — one flush per
    wombat-day, restart-durable (the count lives in Postgres, not memory). ``record()`` is
    called by ``Gate._try_flush`` exactly when a ``SURFACE_FLUSH`` decision is returned.

    ``note_denied()`` is the once-per-wombat-day INFO log for a denied flush attempt: an
    in-memory ``_last_denied`` wombat-date (compared against ``daily_ledger.today()``) dedups
    repeated denials within the same day down to a single line. This in-memory dedup resets on
    process restart, which is acceptable (at most one extra INFO line after a restart) — the
    ``allow()``/``record()`` count itself stays durable regardless.
    """

    def __init__(self, *, daily_ledger: DailyLedger) -> None:
        self._daily_ledger = daily_ledger
        self._last_denied: date | None = None

    def allow(self) -> bool:
        """``True`` iff today's flush count is still below 1 (nothing flushed yet today)."""
        row = self._daily_ledger.current_row(_FLUSH_LEDGER_NAME)
        return row.value < 1

    def record(self) -> None:
        """Book today's one-and-only load flush."""
        self._daily_ledger.increment(_FLUSH_LEDGER_NAME)

    def note_denied(self) -> None:
        """Log ONE INFO line for a denied flush attempt, at most once per wombat day."""
        today = self._daily_ledger.today()
        if today == self._last_denied:
            return
        self._last_denied = today
        _log.info("load flush denied: already flushed today (%s)", today)


__all__ = ["CeilingLedger", "FlushDayLatch"]
