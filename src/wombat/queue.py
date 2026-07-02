"""WombatQueue — durable bounded Postgres-backed queue (TK-2, EP-2, Q-46).

Idempotent enqueue + leased drain + ack over a wombat-owned Postgres table (``wombat_queue``,
``migrations/001_wombat_queue.sql``), so queue state survives a process restart
(at-least-once delivery). FIFO only — no priority ordering (non_goal). No cross-process
locking — single-host, single-process v1 (DEC-6).

Restart/lease mechanism (AC3): each ``WombatQueue`` instance generates a per-instance
``epoch`` (a uuid4) at construction. ``drain()`` leases every row NOT already leased by THIS
epoch — which both claims fresh rows and RECLAIMS rows leased by a DIFFERENT epoch (a foreign
lease can only be a dead prior process's orphaned lease, since v1 is single-host,
single-process). Rows already leased by our OWN epoch (already returned by an earlier
``drain()`` in this same run) are never re-returned. A restart constructs a fresh
``WombatQueue`` — a new epoch — so drain() reclaims and redelivers whatever the prior process
had leased but not yet acked. No lease timeouts, no clock reads, no cross-process locks.

The queue owns exactly ONE lazy psycopg (v3) connection, opened on first use; ``close()``
releases it. No pooling (single-host, DEC-6). ``dsn`` is an injected constructor arg — there is
no module-level DSN literal; sourcing it is the composition root's concern.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from importlib import resources
from typing import Any
from uuid import uuid4

import psycopg

_MIGRATION_PACKAGE = "wombat.migrations"
_MIGRATION_FILENAME = "001_wombat_queue.sql"


class EnqueueResult(Enum):
    """The outcome of a single ``WombatQueue.enqueue`` call."""

    QUEUED = "queued"
    ALREADY_QUEUED = "already_queued"


class QueueFullError(Exception):
    """Raised by ``enqueue`` when the queue is already at ``max_size``. No row is added."""


@dataclass(frozen=True, slots=True)
class QueueItem:
    """A single queue row.

    ``item_id`` is server-assigned by Postgres on insert, so it is ``None`` on the item you
    pass to ``enqueue`` and populated on every item ``drain()`` returns.
    """

    idempotency_key: str
    payload: dict[str, Any]
    item_id: int | None = None


def ensure_schema(conn: psycopg.Connection[Any]) -> None:
    """Apply the packaged, idempotent ``wombat_queue`` migration on the given connection.

    Reads ``migrations/001_wombat_queue.sql`` via ``importlib.resources`` and executes it
    as-is (``CREATE TABLE/INDEX IF NOT EXISTS`` — safe to call every process start, NG-3: no
    migration framework). Callers: tests and the composition root.
    """
    sql = resources.files(_MIGRATION_PACKAGE).joinpath(_MIGRATION_FILENAME).read_text(
        encoding="utf-8"
    )
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()


class WombatQueue:
    """A durable, bounded, FIFO, at-least-once queue over the ``wombat_queue`` table."""

    def __init__(self, dsn: str, *, max_size: int) -> None:
        self._dsn = dsn
        self._max_size = max_size
        self._conn: psycopg.Connection[Any] | None = None
        # The restart-detection identity (Q-46 lease mechanism) — see module docstring.
        self.epoch = uuid4()

    def _connection(self) -> psycopg.Connection[Any]:
        if self._conn is None:
            self._conn = psycopg.connect(self._dsn)
        return self._conn

    def close(self) -> None:
        """Release the lazily-opened connection, if one was ever opened."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def enqueue(self, item: QueueItem) -> EnqueueResult:
        """Idempotently enqueue ``item``.

        Capacity is checked BEFORE the insert: at/above ``max_size``, raises
        ``QueueFullError`` and adds no row (AC2). Otherwise inserts via
        ``INSERT ... ON CONFLICT (idempotency_key) DO NOTHING`` — a conflict (an existing row
        for the same ``idempotency_key``) is a no-op and returns ``ALREADY_QUEUED`` (AC1);
        a fresh row returns ``QUEUED``.
        """
        conn = self._connection()
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM wombat_queue")
            row = cur.fetchone()
            count = row[0] if row is not None else 0
            if count >= self._max_size:
                conn.rollback()
                raise QueueFullError(
                    f"wombat_queue is at capacity ({self._max_size}); enqueue refused"
                )

            cur.execute(
                """
                INSERT INTO wombat_queue (idempotency_key, payload)
                VALUES (%s, %s)
                ON CONFLICT (idempotency_key) DO NOTHING
                """,
                (item.idempotency_key, json.dumps(item.payload)),
            )
            inserted = cur.rowcount
        conn.commit()
        return EnqueueResult.QUEUED if inserted else EnqueueResult.ALREADY_QUEUED

    def drain(self) -> list[QueueItem]:
        """Lease and return ready rows, FIFO, in one atomic lease-and-fetch.

        Leases (``leased_by = this epoch``) every row NOT already leased by this epoch —
        claiming unleased rows and reclaiming rows orphaned by a different (dead, single-host
        v1) epoch — and returns exactly the rows just (re)leased, oldest first. Rows already
        leased by our own epoch from an earlier ``drain()`` this run are left untouched and
        not re-returned. An empty/fully-leased-by-us queue returns ``[]`` immediately.
        """
        conn = self._connection()
        epoch_str = str(self.epoch)
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH leased AS (
                    UPDATE wombat_queue
                    SET leased_by = %s
                    WHERE leased_by IS DISTINCT FROM %s
                    RETURNING id, idempotency_key, payload, created_at
                )
                SELECT id, idempotency_key, payload
                FROM leased
                ORDER BY created_at, id
                """,
                (epoch_str, epoch_str),
            )
            rows = cur.fetchall()
        conn.commit()
        return [
            QueueItem(item_id=row[0], idempotency_key=row[1], payload=json.loads(row[2]))
            for row in rows
        ]

    def ack(self, item_id: int) -> None:
        """Delete the leased row for ``item_id`` exactly once. A second ack is a no-op."""
        conn = self._connection()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM wombat_queue WHERE id = %s", (item_id,))
        conn.commit()
