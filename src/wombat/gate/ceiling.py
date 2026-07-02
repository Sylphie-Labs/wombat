"""CeilingLedger — the production per-class daily surfacing ceiling (TK-27, EP-9, DEC-13).

Implements ``trigger.CeilingProtocol`` over the shared TK-152 ``DailyLedger`` primitive, so
the surfacing ceiling shares the SAME wombat-day boundary (Q-15) as the mouth-spend ledger
(TK-9) and the morning-brief once-per-day counter (TK-97) instead of re-deriving its own day
math. Keyed per ``EventClass`` (DEC-13's personalization key) via ``ledger_name =
"ceiling:<EventClass.value>"``, so each event class gets its own independent daily count.

No inline threshold literal lives here: ``per_class_daily_ceiling`` is an injected
constructor arg (composition passes ``OperatingParams.per_class_daily_ceiling``; tests pass a
literal directly) — TK-13 owns the value, this module only enforces it.
"""

from __future__ import annotations

from wombat.domain.daily_ledger import DailyLedger
from wombat.rating.params import EventClass

_LEDGER_PREFIX = "ceiling"


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


__all__ = ["CeilingLedger"]
