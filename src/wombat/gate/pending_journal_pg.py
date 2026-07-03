"""PgPendingJournal — the real Postgres ``PendingJournal`` adapter (TK-29, RISK-5, Q-70).

The pending set's durability (RISK-5) at the first live session. This module's ENTIRE
obligation is FIDELITY to the append order the caller (``PendingSet``, TK-25) emits — it
invents no ordering, no eviction logic, no identity. See ``wombat.gate.pending_set`` for the
``PendingJournal`` Protocol, the ``JournalRecord`` union, and ``InMemoryPendingJournal`` (the
v1 default + test double this adapter must match on replay/rebuild parity).

FIDELITY (Q-45/ASMP-2): ``append()`` issues exactly ONE INSERT, in its own implicit
transaction — never batched, buffered, reordered, or coalesced. A crash between two appends
durably persists EXACTLY the prefix that got committed, which is what makes the Q-45
Remove-before-Add eviction ordering honest: the ordering is emitted by the caller
``PendingSet``, and this adapter must not disturb it.

``replay()`` is a single ``SELECT ... ORDER BY seq ASC`` — ``seq`` (not ``created_at``) is the
sole replay order, dispatching each row back into the matching ``JournalRecord`` variant.
``item_kind`` is persisted as ``ItemKind.value`` and reconstructed via ``ItemKind(value)``
(Q-49 JSON-native discipline); a NULL ``added_at`` on an 'add' row defaults to 0.0, mirroring
``PendingSetAdd.added_at``'s own default, never raising.

ASMP-2: single live process — no locking/pooling/contention/retry machinery (NG-3). One lazy
psycopg (v3) connection over the INJECTED ``dsn`` (Q-46) — no module-level DSN literal, no
connection attempted at import or construction time.
"""

from __future__ import annotations

from collections.abc import Sequence
from importlib import resources
from typing import Any

import psycopg

from wombat.gate.models import ItemKind
from wombat.gate.pending_set import JournalRecord, PendingSetAdd, PendingSetClear, PendingSetRemove

_MIGRATION_PACKAGE = "wombat.migrations"
_MIGRATION_FILENAME = "005_pending_journal.sql"

_RECORD_TYPE_ADD = "add"
_RECORD_TYPE_REMOVE = "remove"
_RECORD_TYPE_CLEAR = "clear"


def ensure_schema(conn: psycopg.Connection[Any]) -> None:
    """Apply the packaged, idempotent ``pending_journal`` migration on the given connection.

    Reads ``migrations/005_pending_journal.sql`` via ``importlib.resources`` and executes it
    as-is (``CREATE TABLE IF NOT EXISTS`` — safe to call every process start, NG-3: no
    migration framework). Callers: tests and the composition root.
    """
    sql = resources.files(_MIGRATION_PACKAGE).joinpath(_MIGRATION_FILENAME).read_text(
        encoding="utf-8"
    )
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()


class PgPendingJournal:
    """The real Postgres ``PendingJournal`` adapter over the ``pending_journal`` table."""

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

    def append(self, record: JournalRecord) -> None:
        """Durably persist ``record`` as exactly ONE INSERT, in its own implicit transaction.

        Never batches, buffers, reorders, or coalesces — each call is a single INSERT
        committed before returning, so the append order the caller emits (e.g. Q-45's
        Remove-before-Add eviction pair) is preserved verbatim.
        """
        conn = self._connection()
        if isinstance(record, PendingSetAdd):
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO pending_journal
                        (record_type, item_id, item_kind, urgency, load, added_at)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        _RECORD_TYPE_ADD,
                        record.item_id,
                        record.item_kind.value,
                        record.urgency,
                        record.load,
                        record.added_at,
                    ),
                )
        elif isinstance(record, PendingSetRemove):
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO pending_journal (record_type, item_id) VALUES (%s, %s)",
                    (_RECORD_TYPE_REMOVE, record.item_id),
                )
        else:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO pending_journal (record_type) VALUES (%s)",
                    (_RECORD_TYPE_CLEAR,),
                )
        conn.commit()

    def replay(self) -> Sequence[JournalRecord]:
        """Return every ``pending_journal`` row, ordered strictly by ``seq ASC`` (oldest-first).

        Dispatches each row's ``record_type`` back into the matching ``JournalRecord``
        variant. A NULL ``added_at`` on an 'add' row defaults to 0.0 (never raises), and
        ``item_kind`` is reconstructed via ``ItemKind(value)``.
        """
        conn = self._connection()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT record_type, item_id, item_kind, urgency, load, added_at "
                "FROM pending_journal ORDER BY seq ASC"
            )
            rows = cur.fetchall()
        conn.commit()

        records: list[JournalRecord] = []
        for record_type, item_id, item_kind, urgency, load, added_at in rows:
            if record_type == _RECORD_TYPE_ADD:
                records.append(
                    PendingSetAdd(
                        item_id=item_id,
                        item_kind=ItemKind(item_kind),
                        urgency=urgency,
                        load=load,
                        added_at=added_at if added_at is not None else 0.0,
                    )
                )
            elif record_type == _RECORD_TYPE_REMOVE:
                records.append(PendingSetRemove(item_id=item_id))
            else:
                records.append(PendingSetClear())
        return tuple(records)
