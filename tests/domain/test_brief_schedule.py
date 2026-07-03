"""TK-97 — pure-unit tests for the brief-schedule domain (EP-1, Q-80).

No DB, no clock read: ``next_fire_at``/``is_due`` are pure functions of their (aware) ``now``
argument, and ``BriefRunLedger`` is exercised over a tiny fake ``DailyLedger`` (the row lifecycle
itself is TK-152's own tested concern). Covers DST spring-forward + fall-back (both directions),
the fall-back ambiguous-hour + spring-forward gap fold=0 determinism, the midnight straddle
(``now``'s UTC date differs from its local date), and the already-past-today rollover.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

from wombat.domain.brief_schedule import (
    LEDGER_NAME,
    BriefRunLedger,
    is_due,
    next_fire_at,
)
from wombat.domain.daily_ledger import DailyLedgerRow

_CHI = ZoneInfo("America/Chicago")
_SEVEN_AM = time(7, 0)


def _utc(dt: datetime) -> datetime:
    return dt.astimezone(UTC)


# --- next_fire_at: the ordinary same-day / next-day cases ---------------------------------------


def test_next_fire_at_before_todays_time_returns_today() -> None:
    now = datetime(2026, 7, 3, 6, 0, tzinfo=_CHI)  # 06:00, brief at 07:00
    result = next_fire_at(now, _CHI, _SEVEN_AM)
    assert result.astimezone(_CHI) == datetime(2026, 7, 3, 7, 0, tzinfo=_CHI)


def test_next_fire_at_after_todays_time_rolls_to_tomorrow() -> None:
    now = datetime(2026, 7, 3, 8, 0, tzinfo=_CHI)  # 08:00, already past 07:00
    result = next_fire_at(now, _CHI, _SEVEN_AM)
    assert result.astimezone(_CHI) == datetime(2026, 7, 4, 7, 0, tzinfo=_CHI)


def test_next_fire_at_exactly_at_time_is_strict_and_rolls_to_tomorrow() -> None:
    # Strictly-after: a fire landing exactly ON brief_time re-parks for the NEXT day, never the
    # same instant again (the exactly-once guard — the stage calls this right after firing).
    now = datetime(2026, 7, 3, 7, 0, tzinfo=_CHI)
    result = next_fire_at(now, _CHI, _SEVEN_AM)
    assert result.astimezone(_CHI) == datetime(2026, 7, 4, 7, 0, tzinfo=_CHI)


# --- DST: spring-forward and fall-back, both directions ----------------------------------------


def test_next_fire_at_across_spring_forward_lands_at_local_seven_am() -> None:
    # 2026-03-08 America/Chicago springs forward (02:00 CST -> 03:00 CDT). The next 07:00 after
    # Mar 7 08:00 is Mar 8 07:00 CDT == 12:00 UTC (a 23-hour gap from Mar 7 07:00 CST).
    now = datetime(2026, 3, 7, 8, 0, tzinfo=_CHI)
    result = next_fire_at(now, _CHI, _SEVEN_AM)
    assert result.astimezone(_CHI) == datetime(2026, 3, 8, 7, 0, tzinfo=_CHI)
    assert _utc(result) == datetime(2026, 3, 8, 12, 0, tzinfo=UTC)


def test_next_fire_at_across_fall_back_lands_at_local_seven_am() -> None:
    # 2026-11-01 America/Chicago falls back (02:00 CDT -> 01:00 CST). The next 07:00 after
    # Oct 31 08:00 is Nov 1 07:00 CST == 13:00 UTC (a 25-hour gap from Oct 31 07:00 CDT).
    now = datetime(2026, 10, 31, 8, 0, tzinfo=_CHI)
    result = next_fire_at(now, _CHI, _SEVEN_AM)
    assert result.astimezone(_CHI) == datetime(2026, 11, 1, 7, 0, tzinfo=_CHI)
    assert _utc(result) == datetime(2026, 11, 1, 13, 0, tzinfo=UTC)


def test_next_fire_at_fall_back_ambiguous_hour_uses_fold_zero_earlier_instant() -> None:
    # 01:30 occurs twice on 2026-11-01 (01:30 CDT then 01:30 CST). fold=0 picks the EARLIER
    # (CDT, 06:30 UTC) — deterministic by construction, never the later 07:30 UTC occurrence.
    now = datetime(2026, 11, 1, 0, 0, tzinfo=_CHI)
    result = next_fire_at(now, _CHI, time(1, 30))
    assert result.fold == 0
    assert _utc(result) == datetime(2026, 11, 1, 6, 30, tzinfo=UTC)


def test_next_fire_at_spring_forward_gap_hour_uses_fold_zero_deterministically() -> None:
    # 02:30 does not exist on 2026-03-08 (spring gap). fold=0 resolves it against the pre-gap
    # offset (CST, -6) -> a deterministic 08:30 UTC, never a crash.
    now = datetime(2026, 3, 8, 0, 0, tzinfo=_CHI)
    result = next_fire_at(now, _CHI, time(2, 30))
    assert result.fold == 0
    assert _utc(result) == datetime(2026, 3, 8, 8, 30, tzinfo=UTC)


# --- Midnight straddle: now's UTC date differs from its local date ------------------------------


def test_next_fire_at_midnight_straddle_uses_local_civil_date() -> None:
    # now = 2026-07-03 02:00 UTC == 2026-07-02 21:00 CDT: the UTC date (Jul 3) differs from the
    # local date (Jul 2). Today's local 07:00 already passed locally -> next fire = Jul 3 07:00 CDT.
    now = datetime(2026, 7, 3, 2, 0, tzinfo=UTC)
    result = next_fire_at(now, _CHI, _SEVEN_AM)
    assert result.astimezone(_CHI) == datetime(2026, 7, 3, 7, 0, tzinfo=_CHI)


def test_is_due_midnight_straddle_reads_local_day_not_utc_day() -> None:
    # Same straddle instant: locally it is 21:00 on Jul 2, which IS past that day's 07:00, so the
    # brief is due. A naive UTC-date reading (Jul 3 07:00 not yet reached in UTC) would say False.
    now = datetime(2026, 7, 3, 2, 0, tzinfo=UTC)
    assert is_due(now, _CHI, _SEVEN_AM) is True


# --- is_due: the basic before/at/after cases ---------------------------------------------------


def test_is_due_before_time_is_false() -> None:
    assert is_due(datetime(2026, 7, 3, 6, 59, tzinfo=_CHI), _CHI, _SEVEN_AM) is False


def test_is_due_exactly_at_time_is_true() -> None:
    assert is_due(datetime(2026, 7, 3, 7, 0, tzinfo=_CHI), _CHI, _SEVEN_AM) is True


def test_is_due_after_time_is_true() -> None:
    assert is_due(datetime(2026, 7, 3, 9, 0, tzinfo=_CHI), _CHI, _SEVEN_AM) is True


# --- BriefRunLedger over a tiny fake DailyLedger -----------------------------------------------


class _FakeDailyLedger:
    """A minimal stand-in for ``DailyLedger``: one integer counter keyed by ledger_name, enough to
    exercise ``BriefRunLedger``'s ran_today/mark_ran logic without a real Postgres."""

    def __init__(self) -> None:
        self._values: dict[str, int] = {}

    def current_row(self, ledger_name: str) -> DailyLedgerRow:
        value = self._values.get(ledger_name, 0)
        return DailyLedgerRow(ledger_name=ledger_name, wombat_date=date(2026, 7, 3), value=value)

    def increment(self, ledger_name: str, amount: int = 1) -> DailyLedgerRow:
        self._values[ledger_name] = self._values.get(ledger_name, 0) + amount
        return self.current_row(ledger_name)


def test_brief_run_ledger_starts_not_run_today() -> None:
    ledger = BriefRunLedger(_FakeDailyLedger())  # type: ignore[arg-type]
    assert ledger.ran_today() is False


def test_brief_run_ledger_mark_ran_flips_ran_today_and_uses_the_brief_run_row() -> None:
    fake = _FakeDailyLedger()
    ledger = BriefRunLedger(fake)  # type: ignore[arg-type]

    post = ledger.mark_ran()

    assert post == 1
    assert ledger.ran_today() is True
    # The fence rides the fixed "brief:run" row, never the mouth's "spend:tokens" row.
    assert LEDGER_NAME == "brief:run"
    assert fake._values == {"brief:run": 1}


def test_brief_run_ledger_ran_today_true_for_any_positive_value() -> None:
    fake = _FakeDailyLedger()
    ledger = BriefRunLedger(fake)  # type: ignore[arg-type]
    ledger.mark_ran()
    ledger.mark_ran()  # a double-mark still reads as "ran" (value >= 1), never resets
    assert ledger.ran_today() is True
    assert fake._values["brief:run"] == 2
