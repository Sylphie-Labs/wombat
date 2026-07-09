"""BehaviorEventLog — the write (+ range-read) seam over ``wombat_behavior_events`` (TK-111,
EP-21, Q-98).

Q-46 conventions verbatim (mirrors ``PgPendingJournal``/``ActionTrailWriter``/``DailyLedger``):
``dsn`` is an injected constructor arg (no module-level DSN literal), this class owns exactly ONE
lazy psycopg (v3) connection opened on first use (``close()``-able, no pooling — single-host,
DEC-6, no connection attempted at construction), and schema is applied via a packaged, idempotent
``.sql`` file executed by module-level ``ensure_schema(conn)`` (callers: tests + the composition
root; never invoked automatically inside ``upsert``, mirroring every other Q-46 adapter).

Written ONLY by ``DreamBehaviorLogStage`` (``pathways/dream_pathway.py``), the nightly dream
pass's writer (no hot-path call site, EP-13). ``upsert`` is the sole write path — ``INSERT ...
ON CONFLICT (idempotency_key) DO UPDATE`` (AC1 idempotency: re-running the nightly pass over the
SAME terminal claim resolves to the SAME row, never a duplicate). ``events_between`` is the sole
read path, ordered ascending by ``timestamp_utc`` (AC3) — TK-112's future window detector is the
only intended reader; there is NO dashboard/analytics query anywhere in this module (NG-3, AC4
enforced by ``tests/behavior/test_event_log.py``'s import-surface test).

MOTIVE-FREE (CON-6/NG-1, Q-98 ruling f): this module never accepts or stores a motive/why field —
the migration has no such column, and ``outcome_label`` is expected to be one of TK-43's closed
``OUTCOME_*`` predicate values (enforced by the caller, ``DreamBehaviorLogStage``, which only ever
reads terminal ``OUTCOME_*`` claims off the entity KG; this module itself stays a plain typed SQL
adapter with no second runtime schema-violation mechanism of its own).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from importlib import resources
from typing import Any

import psycopg

_MIGRATION_PACKAGE = "wombat.migrations"
_MIGRATION_FILENAME = "006_behavior_events.sql"


def ensure_schema(conn: psycopg.Connection[Any]) -> None:
    """Apply the packaged, idempotent ``wombat_behavior_events`` migration on ``conn``.

    Reads ``migrations/006_behavior_events.sql`` via ``importlib.resources`` and executes it
    as-is (``CREATE TABLE IF NOT EXISTS`` — safe to call every process start, NG-3: no migration
    framework). Callers: tests and the composition root.
    """
    sql = resources.files(_MIGRATION_PACKAGE).joinpath(_MIGRATION_FILENAME).read_text(
        encoding="utf-8"
    )
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()


@dataclass(frozen=True, slots=True)
class BehaviorEventRow:
    """One ``wombat_behavior_events`` row, as returned by ``events_between`` (AC3)."""

    idempotency_key: str
    event_type: str
    source_id: str
    timestamp_utc: datetime
    outcome_label: str
    duration_seconds: float | None


class BehaviorEventLog:
    """The Postgres-backed append-only behavioral event log (TK-111, Q-98)."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
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

    def upsert(
        self,
        *,
        idempotency_key: str,
        event_type: str,
        source_id: str,
        timestamp_utc: datetime,
        outcome_label: str,
        duration_seconds: float | None = None,
    ) -> None:
        """Write one behavioral event row, keyed on the canonical TK-12 ``idempotency_key``.

        ``INSERT ... ON CONFLICT (idempotency_key) DO UPDATE`` — a re-run of the nightly pass
        over the SAME terminal claim resolves to the SAME row (AC1: idempotent, row count
        unchanged on re-run), never a duplicate insert.
        """
        conn = self._connection()
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO wombat_behavior_events
                    (idempotency_key, event_type, source_id, timestamp_utc, outcome_label,
                     duration_seconds)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (idempotency_key) DO UPDATE SET
                    event_type = EXCLUDED.event_type,
                    source_id = EXCLUDED.source_id,
                    timestamp_utc = EXCLUDED.timestamp_utc,
                    outcome_label = EXCLUDED.outcome_label,
                    duration_seconds = EXCLUDED.duration_seconds
                """,
                (
                    idempotency_key,
                    event_type,
                    source_id,
                    timestamp_utc,
                    outcome_label,
                    duration_seconds,
                ),
            )
        conn.commit()

    def events_between(self, start: datetime, end: datetime) -> Sequence[BehaviorEventRow]:
        """Return every row with ``timestamp_utc`` in ``[start, end]``, ordered ASCENDING by
        ``timestamp_utc`` (AC3: human-readable, date-range queryable). TK-112's future window
        detector is the intended reader — no dashboard/analytics call site exists (NG-3).
        """
        conn = self._connection()
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT idempotency_key, event_type, source_id, timestamp_utc, outcome_label,
                       duration_seconds
                FROM wombat_behavior_events
                WHERE timestamp_utc >= %s AND timestamp_utc <= %s
                ORDER BY timestamp_utc ASC
                """,
                (start, end),
            )
            rows = cur.fetchall()
        conn.commit()
        return tuple(
            BehaviorEventRow(
                idempotency_key=row[0],
                event_type=row[1],
                source_id=row[2],
                timestamp_utc=row[3],
                outcome_label=row[4],
                duration_seconds=row[5],
            )
            for row in rows
        )


__all__ = ["BehaviorEventLog", "BehaviorEventRow", "ensure_schema"]
