"""ActionTrailReader — the read-only, ordered query surface over ``action_trail_projection``
(TK-147, EP-27, Q-63/Q-89).

Queries strictly through ``wombat.trail.schema``'s table/column-name constants (the Q-63 seam)
— never raw string literals — and returns the frozen ``TrailRow`` dataclass, ordered by
``seq`` ASCENDING (insertion order). Read-only: this module issues no writes and does not
import ``wombat.trail.writer``.

Accepts either a DSN string or an already-open ``psycopg.Connection`` at construction (Q-89
ruling 3's "pg conn-or-DSN" — lets ``ActionTrailRenderer`` share one connection across a
reader+writer test fixture without opening a second one). Given a DSN, the connection is
opened lazily on first use and owned (closed) by this reader; given an existing connection,
this reader never closes it — the caller retains ownership.
"""

from __future__ import annotations

from typing import Any

import psycopg

from wombat.trail.schema import (
    COL_ACTION_ID,
    COL_ACTION_TYPE,
    COL_CANCELLED_AT,
    COL_DISPATCHED_AT,
    COL_HUMAN_SUMMARY,
    COL_PROPOSED_AT,
    COL_SEQ,
    COL_STATUS,
    COL_TARGET,
    TABLE,
    TrailRow,
)

__all__ = ["ActionTrailReader"]

_SELECT_ROWS_BY_SEQ = (
    f"SELECT {COL_ACTION_ID}, {COL_SEQ}, {COL_ACTION_TYPE}, {COL_HUMAN_SUMMARY}, "
    f"{COL_TARGET}, {COL_PROPOSED_AT}, {COL_STATUS}, {COL_DISPATCHED_AT}, {COL_CANCELLED_AT} "
    f"FROM {TABLE} ORDER BY {COL_SEQ} ASC"
)


class ActionTrailReader:
    """Read-only, ``seq``-ordered query surface over ``action_trail_projection`` (Q-63)."""

    def __init__(self, dsn_or_conn: str | psycopg.Connection[Any]) -> None:
        if isinstance(dsn_or_conn, str):
            self._dsn: str | None = dsn_or_conn
            self._conn: psycopg.Connection[Any] | None = None
            self._owns_conn = True
        else:
            self._dsn = None
            self._conn = dsn_or_conn
            self._owns_conn = False

    def _connection(self) -> psycopg.Connection[Any]:
        if self._conn is None:
            assert self._dsn is not None
            self._conn = psycopg.connect(self._dsn)
        return self._conn

    def close(self) -> None:
        """Release the connection, but ONLY if this reader opened it itself (lazily, from a
        DSN) — a connection handed in at construction is caller-owned and is never closed here.
        """
        if self._owns_conn and self._conn is not None:
            self._conn.close()
            self._conn = None

    def rows(self) -> list[TrailRow]:
        """Every ``action_trail_projection`` row, ordered by ``seq`` ASCENDING (insertion order).

        Queries strictly via the ``wombat.trail.schema`` column/table constants — never raw
        string literals (the Q-63 seam) — and returns each row as a frozen ``TrailRow``.
        """
        conn = self._connection()
        with conn.cursor() as cur:
            cur.execute(_SELECT_ROWS_BY_SEQ)
            fetched = cur.fetchall()
        conn.commit()
        return [
            TrailRow(
                action_id=r[0],
                seq=r[1],
                action_type=r[2],
                human_summary=r[3],
                target=r[4],
                proposed_at=r[5],
                status=r[6],
                dispatched_at=r[7],
                cancelled_at=r[8],
            )
            for r in fetched
        ]
