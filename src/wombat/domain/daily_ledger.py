"""DailyLedger — the shared wombat-day boundary + row lifecycle (TK-152, EP-2, Q-46/DEC-21).

wombat's three independent daily counters (mouth spend TK-9, surfacing ceiling TK-27/28, brief
once-per-day TK-97) must all agree on what "today" is (Q-15) — a laptop that sleeps across
midnight (CST-1/DEC-6) must never double-count or skip a day. ``wombat_today`` is the ONE
definition of the civil-day boundary; every daily counter resolves "today" through it instead of
hard-coding UTC or local time independently.

``wombat_today`` is pure: it takes an aware instant and a configured IANA zone and returns the
civil date in that zone. It performs no clock read and no I/O — callers supply the instant (via
an injected ``clock``) so the boundary is computed at ACCESS time, not by a fired timer (AC3).

``DailyLedger`` is the Postgres-backed row lifecycle over one row per ``(ledger_name,
wombat_date)`` (``daily_ledger``, ``migrations/003_daily_ledger.sql``), mirroring the ``Q-46``
conventions established by ``WombatQueue`` (TK-2): ``dsn`` is an injected constructor arg (no
module-level DSN literal), the ledger owns exactly ONE lazy psycopg (v3) connection opened on
first use (``close()``-able, no pooling — single-host, DEC-6), and schema is applied via a
packaged, idempotent ``.sql`` file executed by module-level ``ensure_schema(conn)``.

Load-on-start (AC2): if a row already exists for ``(ledger_name, today)`` it is reloaded as-is —
its counter value is preserved, never reset to zero. A new civil day produces a new
``(ledger_name, wombat_date)`` key, so "rollover" is implicit: the next ``current_row()`` call
after the boundary simply resolves a different key and gets (or creates) a fresh row. There is no
midnight timer anywhere.

This module owns only the day boundary and the row lifecycle. What a counter's ``value`` MEANS
(spend amount, ceiling count, brief-run flag) is entirely up to the caller (TK-9/27/28/97,
non_goal).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from importlib import resources
from typing import Any
from zoneinfo import ZoneInfo

import psycopg

_MIGRATION_PACKAGE = "wombat.migrations"
_MIGRATION_FILENAME = "003_daily_ledger.sql"


def _utc_now() -> datetime:
    """The real-clock default for ``DailyLedger``'s injected ``clock``."""
    return datetime.now(UTC)


def wombat_today(instant: datetime, tz: ZoneInfo) -> date:
    """Return the civil date of ``instant`` in ``tz`` — THE canonical wombat-day boundary.

    Pure function of its arguments: converts the given (aware) ``instant`` into ``tz`` and takes
    its ``.date()``. Performs no clock read and no other zoneinfo lookup — every daily-counter
    consumer (TK-9/27/97) must call this instead of independently hard-coding UTC or local time
    (Q-15), so the same instant always resolves to the same wombat_date everywhere.
    """
    if instant.tzinfo is None:
        raise ValueError("wombat_today() requires an aware instant (tzinfo is None)")
    return instant.astimezone(tz).date()


@dataclass(frozen=True, slots=True)
class DailyLedgerRow:
    """One ``daily_ledger`` row: a single counter for a single ledger on a single wombat-day."""

    ledger_name: str
    wombat_date: date
    value: int


def ensure_schema(conn: psycopg.Connection[Any]) -> None:
    """Apply the packaged, idempotent ``daily_ledger`` migration on the given connection.

    Reads ``migrations/003_daily_ledger.sql`` via ``importlib.resources`` and executes it as-is
    (``CREATE TABLE IF NOT EXISTS`` — safe to call every process start, NG-3: no migration
    framework). Callers: tests and the composition root.
    """
    sql = resources.files(_MIGRATION_PACKAGE).joinpath(_MIGRATION_FILENAME).read_text(
        encoding="utf-8"
    )
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()


class DailyLedger:
    """The shared row lifecycle over the ``daily_ledger`` table (day boundary only, Q-46)."""

    def __init__(
        self,
        dsn: str,
        *,
        tz: ZoneInfo,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._dsn = dsn
        self._tz = tz
        self._clock = clock
        self._conn: psycopg.Connection[Any] | None = None

    def _connection(self) -> psycopg.Connection[Any]:
        if self._conn is None:
            self._conn = psycopg.connect(self._dsn)
        return self._conn

    def close(self) -> None:
        """Release the lazily-opened connection, if one was ever opened."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def current_row(self, ledger_name: str) -> DailyLedgerRow:
        """Return today's row for ``ledger_name``, creating it at 0 if it doesn't exist yet.

        "Today" is resolved fresh on every call via ``wombat_today(self._clock(), self._tz)`` —
        the boundary is computed at ACCESS time, not by a fired timer (AC3). If a row already
        exists for ``(ledger_name, today)`` it is reloaded as-is: its counter value is preserved,
        never reset to zero (AC2, load-on-start).
        """
        today = wombat_today(self._clock(), self._tz)
        conn = self._connection()
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO daily_ledger (ledger_name, wombat_date, value)
                VALUES (%s, %s, 0)
                ON CONFLICT (ledger_name, wombat_date) DO NOTHING
                """,
                (ledger_name, today),
            )
            cur.execute(
                "SELECT ledger_name, wombat_date, value FROM daily_ledger "
                "WHERE ledger_name = %s AND wombat_date = %s",
                (ledger_name, today),
            )
            row = cur.fetchone()
        conn.commit()
        assert row is not None  # the INSERT above guarantees the row exists
        return DailyLedgerRow(ledger_name=row[0], wombat_date=row[1], value=row[2])

    def increment(self, ledger_name: str, amount: int = 1) -> DailyLedgerRow:
        """Atomically add ``amount`` to today's ``ledger_name`` row and return it.

        Creates the row (starting from ``amount``) if today's row doesn't exist yet. Row
        lifecycle only — what the resulting ``value`` MEANS is up to the caller (non_goal).
        """
        today = wombat_today(self._clock(), self._tz)
        conn = self._connection()
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO daily_ledger (ledger_name, wombat_date, value)
                VALUES (%s, %s, %s)
                ON CONFLICT (ledger_name, wombat_date)
                DO UPDATE SET value = daily_ledger.value + EXCLUDED.value
                RETURNING ledger_name, wombat_date, value
                """,
                (ledger_name, today, amount),
            )
            row = cur.fetchone()
        conn.commit()
        assert row is not None
        return DailyLedgerRow(ledger_name=row[0], wombat_date=row[1], value=row[2])
