"""TK-9 — DailySpendLedger acceptance criteria (thin wrapper over DailyLedger, Q-68).

Unit tests (a fake ``DailyLedger`` double) always run. The DSN-gated wrapper test exercises the
real wire against a throwaway Postgres, mirroring ``test_daily_ledger.py``'s gating idiom (AC
3/5 — load-on-start + the wombat-day boundary come from ``DailyLedger`` by construction; this
just proves ``DailySpendLedger`` doesn't get in the way of that):

    docker run --rm -d -p 5433:5432 -e POSTGRES_PASSWORD=wombat postgres:16
    WOMBAT_TEST_PG_DSN=postgresql://postgres:wombat@localhost:5433/postgres
"""

from __future__ import annotations

import os
from datetime import date, datetime
from zoneinfo import ZoneInfo

import psycopg
import pytest

from wombat.cost.daily_spend_ledger import LEDGER_NAME, DailySpendLedger
from wombat.domain.daily_ledger import DailyLedger, DailyLedgerRow, ensure_schema

_DSN = os.environ.get("WOMBAT_TEST_PG_DSN")
_FIXED_DATE = date(2026, 7, 2)

_requires_pg = pytest.mark.skipif(
    not _DSN,
    reason=(
        "WOMBAT_TEST_PG_DSN is not set — skipping DailySpendLedger DB tests that require a real "
        "throwaway Postgres. Start one with:\n"
        "  docker run --rm -d -p 5433:5432 -e POSTGRES_PASSWORD=wombat postgres:16\n"
        "then export WOMBAT_TEST_PG_DSN=postgresql://postgres:wombat@localhost:5433/postgres"
    ),
)


class _FakeDailyLedger:
    """A minimal in-memory double of the two ``DailyLedger`` methods ``DailySpendLedger`` calls."""

    def __init__(self, value: int = 0) -> None:
        self.value = value

    def current_row(self, ledger_name: str) -> DailyLedgerRow:
        assert ledger_name == LEDGER_NAME
        return DailyLedgerRow(ledger_name=ledger_name, wombat_date=_FIXED_DATE, value=self.value)

    def increment(self, ledger_name: str, amount: int = 1) -> DailyLedgerRow:
        assert ledger_name == LEDGER_NAME
        self.value += amount
        return DailyLedgerRow(ledger_name=ledger_name, wombat_date=_FIXED_DATE, value=self.value)


# --------------------------------------------------------------------------------- unit tests


def test_uses_the_fixed_spend_tokens_ledger_name() -> None:
    assert LEDGER_NAME == "spend:tokens"


def test_tokens_spent_today_reads_zero_when_no_spend_yet() -> None:
    ledger = DailySpendLedger(_FakeDailyLedger())  # type: ignore[arg-type]

    assert ledger.tokens_spent_today() == 0


def test_add_tokens_increments_and_is_reflected_by_a_subsequent_read() -> None:
    ledger = DailySpendLedger(_FakeDailyLedger())  # type: ignore[arg-type]

    total = ledger.add_tokens(150)

    assert total == 150
    assert ledger.tokens_spent_today() == 150


def test_add_tokens_is_cumulative_across_calls() -> None:
    ledger = DailySpendLedger(_FakeDailyLedger())  # type: ignore[arg-type]

    ledger.add_tokens(100)
    ledger.add_tokens(42)

    assert ledger.tokens_spent_today() == 142


# ------------------------------------------------------------------------------- DB test (Q-46)


@pytest.fixture
def clean_table() -> None:
    assert _DSN is not None
    with psycopg.connect(_DSN) as conn:
        ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE daily_ledger")
        conn.commit()


@_requires_pg
def test_spend_recorded_and_reread_survives_a_fresh_wrapper_over_real_postgres(
    clean_table: None,
) -> None:
    """A spend recorded through one ``DailySpendLedger`` is re-read by a FRESH one on the same
    DSN — load-on-start/restart durability, which ``DailyLedger`` (TK-152) already proves; this
    confirms the thin wrapper doesn't lose it."""
    assert _DSN is not None
    fixed_instant = datetime(2026, 7, 2, 12, 0, tzinfo=ZoneInfo("UTC"))

    daily_ledger = DailyLedger(_DSN, tz=ZoneInfo("UTC"), clock=lambda: fixed_instant)
    try:
        ledger = DailySpendLedger(daily_ledger)
        assert ledger.tokens_spent_today() == 0
        ledger.add_tokens(500)
        assert ledger.tokens_spent_today() == 500
    finally:
        daily_ledger.close()

    # A FRESH DailyLedger/DailySpendLedger on the same DSN simulates a process restart.
    restarted = DailyLedger(_DSN, tz=ZoneInfo("UTC"), clock=lambda: fixed_instant)
    try:
        reread = DailySpendLedger(restarted)
        assert reread.tokens_spent_today() == 500  # NOT reset to zero — load-on-start
    finally:
        restarted.close()
