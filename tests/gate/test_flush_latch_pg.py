"""TK-287 (DEC-63b) AC4 — ``FlushDayLatch`` restart-durability against a REAL Postgres.

ALL tests here require a REAL Postgres and are gated on the ``WOMBAT_TEST_PG_DSN`` env var:
absent it, tests are skipped LOUDLY (never faked, never CI-failed on a fresh clone), mirroring
``tests/gate/test_pending_journal_pg.py``. Spin up a throwaway Postgres locally:

    docker run --rm -d -p 5433:5432 -e POSTGRES_PASSWORD=wombat postgres:16
    WOMBAT_TEST_PG_DSN=postgresql://postgres:wombat@localhost:5433/postgres

AC4: a real ``DailyLedger`` with today's flush already recorded, then a FRESH ``FlushDayLatch``
constructed over the SAME dsn -> ``allow()`` is ``False`` (a process restart does not re-arm the
latch — the count is durable in Postgres, not in-memory). The ``clean_flush_rows`` fixture
deletes ONLY ``ledger_name = 'flush:load'`` rows between tests — never a table ``TRUNCATE`` and
never anything touching the live database (throwaway pg only, per the operator's standing rule).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import psycopg
import pytest

from wombat.domain.daily_ledger import DailyLedger, ensure_schema
from wombat.gate.ceiling import FlushDayLatch

_DSN = os.environ.get("WOMBAT_TEST_PG_DSN")

_requires_pg = pytest.mark.skipif(
    not _DSN,
    reason=(
        "WOMBAT_TEST_PG_DSN is not set — skipping FlushDayLatch DB tests that require a real "
        "throwaway Postgres. Start one with:\n"
        "  docker run --rm -d -p 5433:5432 -e POSTGRES_PASSWORD=wombat postgres:16\n"
        "then export WOMBAT_TEST_PG_DSN=postgresql://postgres:wombat@localhost:5433/postgres"
    ),
)


@pytest.fixture
def clean_flush_rows() -> None:
    """Ensure the schema exists and delete ONLY this module's own 'flush:load' rows — never a
    table ``TRUNCATE`` (this suite touches nothing beyond its own ledger_name)."""
    assert _DSN is not None
    with psycopg.connect(_DSN) as conn:
        ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute("DELETE FROM daily_ledger WHERE ledger_name = 'flush:load'")
        conn.commit()


# --------------------------------------------------------------------------------------- AC4


@_requires_pg
def test_ac4_fresh_latch_over_the_same_dsn_does_not_re_arm_after_a_restart(
    clean_flush_rows: None,
) -> None:
    """A real ``DailyLedger`` with today's flush already recorded, then a FRESH ``FlushDayLatch``
    constructed over the SAME dsn -> ``allow()`` is ``False`` (a restart does not re-arm)."""
    assert _DSN is not None
    tz = ZoneInfo("UTC")
    fixed_instant = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)

    recording_ledger = DailyLedger(_DSN, tz=tz, clock=lambda: fixed_instant)
    try:
        recording_latch = FlushDayLatch(daily_ledger=recording_ledger)
        assert recording_latch.allow() is True
        recording_latch.record()
    finally:
        recording_ledger.close()

    # A FRESH DailyLedger + FRESH FlushDayLatch over the SAME dsn simulates a process restart.
    fresh_ledger = DailyLedger(_DSN, tz=tz, clock=lambda: fixed_instant)
    try:
        fresh_latch = FlushDayLatch(daily_ledger=fresh_ledger)
        assert fresh_latch.allow() is False
    finally:
        fresh_ledger.close()


@_requires_pg
def test_ac4_record_increments_the_underlying_flush_load_row(clean_flush_rows: None) -> None:
    """``record()`` books exactly one increment on the durable ``'flush:load'`` ledger row."""
    assert _DSN is not None
    tz = ZoneInfo("UTC")
    fixed_instant = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)
    daily_ledger = DailyLedger(_DSN, tz=tz, clock=lambda: fixed_instant)
    try:
        latch = FlushDayLatch(daily_ledger=daily_ledger)
        latch.record()
        row = daily_ledger.current_row("flush:load")
        assert row.value == 1
    finally:
        daily_ledger.close()


@_requires_pg
def test_ac4_a_new_wombat_day_reopens_the_latch(clean_flush_rows: None) -> None:
    """A new wombat-day resolves a fresh (0-value) row -- the latch reopens with no reset
    MUTATION needed (mirrors ``CeilingLedger``'s day-boundary precedent, DEC-21)."""
    assert _DSN is not None
    tz = ZoneInfo("UTC")
    day_holder = [datetime(2026, 7, 21, 12, 0, tzinfo=UTC)]
    daily_ledger = DailyLedger(_DSN, tz=tz, clock=lambda: day_holder[0])
    try:
        latch = FlushDayLatch(daily_ledger=daily_ledger)
        latch.record()
        assert latch.allow() is False

        day_holder[0] = datetime(2026, 7, 22, 0, 5, tzinfo=UTC)
        assert latch.allow() is True
    finally:
        daily_ledger.close()
