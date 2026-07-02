"""TK-152 — shared DailyLedger primitive acceptance criteria (EP-2, Q-46/DEC-21/Q-15).

Pure tests (AC1, AC4) exercise ``wombat_today`` directly — no DB, no wall-clock read — and
always run.

DB tests (AC2, AC3) exercise ``DailyLedger`` against a REAL Postgres and are gated on the
``WOMBAT_TEST_PG_DSN`` env var: absent it, they are skipped LOUDLY (never faked, never
CI-failed on a fresh clone). Spin up a throwaway Postgres locally:

    docker run --rm -d -p 5433:5432 -e POSTGRES_PASSWORD=wombat postgres:16
    WOMBAT_TEST_PG_DSN=postgresql://postgres:wombat@localhost:5433/postgres

Each DB test truncates the table before it runs (via the ``clean_table`` fixture) so a shared
local Postgres is safe to reuse.

  AC1 wombat_today(fixed_instant, tz) returns the civil date in that zone — all three ledger
      consumers ('spend', 'ceiling', 'brief') resolve the IDENTICAL wombat_date for the SAME
      fixed instant.
  AC2 current_row('spend') after a restart (a fresh DailyLedger on the same DSN) reloads the
      persisted row — counter preserved, not reset to zero.
  AC3 a clock at 23:30 local, then 00:30 local the next day, yields two distinct wombat_date
      rows — no skipped day, no double-count, exactly one new row for the new day.
  AC4 two different configured timezones resolve an instant to different civil dates
      deterministically — no code path hard-codes UTC or local.
"""

from __future__ import annotations

import os
from datetime import datetime
from zoneinfo import ZoneInfo

import psycopg
import pytest

from wombat.domain.daily_ledger import DailyLedger, ensure_schema, wombat_today

_DSN = os.environ.get("WOMBAT_TEST_PG_DSN")

_requires_pg = pytest.mark.skipif(
    not _DSN,
    reason=(
        "WOMBAT_TEST_PG_DSN is not set — skipping DailyLedger DB tests that require a real "
        "throwaway Postgres. Start one with:\n"
        "  docker run --rm -d -p 5433:5432 -e POSTGRES_PASSWORD=wombat postgres:16\n"
        "then export WOMBAT_TEST_PG_DSN=postgresql://postgres:wombat@localhost:5433/postgres"
    ),
)


# --------------------------------------------------------------------------- pure tests (AC1/4)


def test_ac1_all_three_ledger_consumers_resolve_identical_wombat_date() -> None:
    """The same fixed instant resolves to the SAME wombat_date for every daily-counter consumer.

    Simulates the three real callers (mouth spend TK-9, surfacing ceiling TK-27/28, brief
    once-per-day TK-97) by ledger_name; each just calls the same ``wombat_today`` — there is
    no per-ledger date logic to diverge (Q-15).
    """
    tz = ZoneInfo("America/Chicago")
    fixed_instant = datetime(2026, 7, 2, 15, 0, tzinfo=ZoneInfo("UTC"))

    resolved = {
        ledger_name: wombat_today(fixed_instant, tz)
        for ledger_name in ("spend", "ceiling", "brief")
    }

    assert len(set(resolved.values())) == 1
    assert resolved["spend"] == datetime(2026, 7, 2).date()


def test_ac4_wombat_today_reflects_the_configured_zone_deterministically() -> None:
    """The same instant resolves to different civil dates under different configured zones.

    An instant just after midnight UTC is still 2026-07-02 in a zone west of UTC, but already
    2026-07-03 in a zone east of UTC — proving the boundary comes from the INJECTED tz, not a
    hard-coded UTC or local assumption.
    """
    instant = datetime(2026, 7, 2, 23, 30, tzinfo=ZoneInfo("UTC"))

    west_of_utc = wombat_today(instant, ZoneInfo("America/Chicago"))  # UTC-5 in July
    east_of_utc = wombat_today(instant, ZoneInfo("Pacific/Auckland"))  # UTC+12 in July

    assert west_of_utc == datetime(2026, 7, 2).date()
    assert east_of_utc == datetime(2026, 7, 3).date()
    assert west_of_utc != east_of_utc

    # Calling with the SAME instant but a different tz is deterministic and repeatable.
    assert wombat_today(instant, ZoneInfo("America/Chicago")) == west_of_utc


# ------------------------------------------------------------------------------- DB tests (AC2/3)


@pytest.fixture
def clean_table() -> None:
    """Ensure the schema exists and the table is empty before each DB test."""
    assert _DSN is not None
    with psycopg.connect(_DSN) as conn:
        ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE daily_ledger")
        conn.commit()


@_requires_pg
def test_ac2_current_row_reloads_persisted_row_across_restart(clean_table: None) -> None:
    """A restart (fresh DailyLedger, same DSN) reloads the row — counter preserved, not reset."""
    assert _DSN is not None
    tz = ZoneInfo("America/Chicago")
    fixed_instant = datetime(2026, 7, 2, 12, 0, tzinfo=ZoneInfo("UTC"))

    ledger = DailyLedger(_DSN, tz=tz, clock=lambda: fixed_instant)
    try:
        ledger.current_row("spend")  # creates today's row at 0
        incremented = ledger.increment("spend", amount=5)
        assert incremented.value == 5
    finally:
        ledger.close()

    # A FRESH DailyLedger on the same DSN simulates a process restart.
    restarted = DailyLedger(_DSN, tz=tz, clock=lambda: fixed_instant)
    try:
        reloaded = restarted.current_row("spend")
        assert reloaded.value == 5  # NOT reset to zero — load-on-start
        assert reloaded.ledger_name == "spend"
        assert reloaded.wombat_date == wombat_today(fixed_instant, tz)
    finally:
        restarted.close()


@_requires_pg
def test_ac3_sleep_across_midnight_yields_one_new_row_for_the_new_day(
    clean_table: None,
) -> None:
    """23:30 -> 00:30 local (a sleep across midnight) yields two distinct, untouched rows.

    No skipped date, no double-counted prior day — the boundary is resolved at access time from
    the injected clock, not by a fired timer.
    """
    assert _DSN is not None
    tz = ZoneInfo("America/Chicago")
    before_midnight = datetime(2026, 7, 2, 23, 30, tzinfo=tz)
    after_midnight = datetime(2026, 7, 3, 0, 30, tzinfo=tz)

    ledger = DailyLedger(_DSN, tz=tz, clock=lambda: before_midnight)
    try:
        day_d = ledger.increment("ceiling", amount=1)
        assert day_d.wombat_date == wombat_today(before_midnight, tz)
        assert day_d.value == 1
    finally:
        ledger.close()

    # The host "wakes" after midnight — a fresh clock reading, same ledger_name.
    woken = DailyLedger(_DSN, tz=tz, clock=lambda: after_midnight)
    try:
        day_d_plus_1 = woken.current_row("ceiling")
        assert day_d_plus_1.wombat_date == wombat_today(after_midnight, tz)
        assert day_d_plus_1.wombat_date != day_d.wombat_date  # distinct wombat_date
        assert day_d_plus_1.value == 0  # a genuinely fresh row for the new day, not a carryover

        # The prior day's row is untouched — no double-count, no skip.
        with psycopg.connect(_DSN) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT value FROM daily_ledger WHERE ledger_name = %s AND wombat_date = %s",
                ("ceiling", day_d.wombat_date),
            )
            row = cur.fetchone()
            assert row is not None
            assert row[0] == 1

            cur.execute("SELECT count(*) FROM daily_ledger WHERE ledger_name = %s", ("ceiling",))
            count_row = cur.fetchone()
            assert count_row is not None
            assert count_row[0] == 2  # exactly one new row for the new day
    finally:
        woken.close()
