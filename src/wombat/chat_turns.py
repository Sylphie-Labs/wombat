"""chat_turns — the 7-day rolling ledger of the user's chat/voice utterances (TK-295, DEC-65e).

Owns ``wombat_chat_turns`` (``id`` BIGSERIAL PRIMARY KEY, ``text`` TEXT NOT NULL, ``voice`` BOOLEAN
NOT NULL, ``captured_at`` TIMESTAMPTZ NOT NULL, indexed on ``captured_at``). ``ensure_schema(conn)``
is the packaged, idempotent ``CREATE TABLE IF NOT EXISTS`` (NG-3: no migration framework — the
scratchpad/user_facts sibling precedent, ``migrations/012_chat_turns.sql``), wired as
``schema_preflight.ensure_all_schemas``'s ELEVENTH entry.

``ChatTurnStore`` is a ``dsn``-injected psycopg reader/writer (the Q-46 lazy-connection
convention, mirroring ``scratchpad.ScratchpadStore``/``user_facts.UserFactsStore`` exactly — zero
I/O at construction): ``record_turn(text, voice, captured_at)`` inserts one row.
``turns_since(cutoff)`` returns every row whose ``captured_at`` is at or after ``cutoff``,
ascending by ``captured_at``. ``purge_older_than(days)`` deletes rows by ``captured_at`` age and
returns the deleted count.

WHY THIS EXISTS (DEC-65e): the queue DELETEs a source item on ack, so nothing the user says
survives past that turn today — organic getting-to-know needs a durable record of the user's OWN
words. ``_RETENTION_DAYS = 7`` (pinned, no knob) documents the retention window this store is
designed for; ``bootstrap.serve()`` calls ``purge_older_than(7)`` exactly once at boot (the
``ScratchpadStore``/``ExternalItemStore`` purge/prune-on-boot precedent).

DREAM-EXTRACTION INPUT ONLY (DEC-64's multi-turn-history rejection stands, restated here): rows in
this table are NEVER rendered into any prompt, NEVER a conversation-history window, and NEVER a
gate/scoring input. The nightly dream pass (TK-297) is this store's ONLY organic reader. This
module itself enforces nothing about that — it is a plain store, not a policy boundary.

CON-1/CON-6 CUSTODY NOTE: this ledger holds ONLY the user's own words. No assistant-reply logging,
no correlation ids — the write seam (``sources.bootstrap``'s registry sink tap) enforces this by
construction, never this module.

STRUCTURAL: this module imports NOTHING from ``wombat.bootstrap`` or ``wombat.runtime``.
"""

from __future__ import annotations

from datetime import datetime
from importlib import resources
from typing import Any

import psycopg

_MIGRATION_PACKAGE = "wombat.migrations"
_MIGRATION_FILENAME = "012_chat_turns.sql"

TABLE = "wombat_chat_turns"

# DEC-65e: the pinned retention window this store is designed for — no knob. Documented here for
# reference; the boot purge call (bootstrap.serve()) passes the day count explicitly.
_RETENTION_DAYS = 7


def ensure_schema(conn: psycopg.Connection[Any]) -> None:
    """Apply the packaged, idempotent ``wombat_chat_turns`` migration on ``conn``.

    Reads ``migrations/012_chat_turns.sql`` via ``importlib.resources`` and executes it as-is
    (``CREATE TABLE/INDEX IF NOT EXISTS`` — safe to call every process start, NG-3: no migration
    framework). Callers: tests and ``schema_preflight.ensure_all_schemas``.
    """
    sql = resources.files(_MIGRATION_PACKAGE).joinpath(_MIGRATION_FILENAME).read_text(
        encoding="utf-8"
    )
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()


class ChatTurnStore:
    """The Postgres-backed reader/writer over ``wombat_chat_turns`` (TK-295, Q-46 conventions —
    mirrors ``scratchpad.ScratchpadStore``/``user_facts.UserFactsStore`` exactly).

    ``dsn`` is an injected constructor arg (no module-level DSN literal); this class owns exactly
    ONE lazy psycopg (v3) connection, opened on first use — ``close()`` releases it, no pooling
    (single-host, DEC-6). Schema is applied separately via module-level ``ensure_schema`` — never
    invoked automatically inside any method here.
    """

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

    def record_turn(self, text: str, voice: bool, captured_at: datetime) -> None:
        """Insert one row — the user's own utterance text, whether it arrived spoken (``voice``)
        or typed, at ``captured_at``."""
        conn = self._connection()
        with conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO {TABLE} (text, voice, captured_at) VALUES (%s, %s, %s)",
                (text, voice, captured_at),
            )
        conn.commit()

    def turns_since(self, cutoff: datetime) -> list[dict[str, Any]]:
        """Return every row whose ``captured_at`` is at or after ``cutoff``, ascending by
        ``captured_at``."""
        conn = self._connection()
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT id, text, voice, captured_at
                FROM {TABLE}
                WHERE captured_at >= %s
                ORDER BY captured_at ASC
                """,
                (cutoff,),
            )
            rows = cur.fetchall()
        conn.commit()
        return [_row_to_dict(row) for row in rows]

    def purge_older_than(self, days: int) -> int:
        """Delete every row whose ``captured_at`` is older than ``days`` days ago. Returns the
        number of rows deleted."""
        conn = self._connection()
        with conn.cursor() as cur:
            cur.execute(
                f"""
                DELETE FROM {TABLE}
                WHERE captured_at < now() - (%s * INTERVAL '1 day')
                """,
                (days,),
            )
            deleted = cur.rowcount
        conn.commit()
        return deleted


def _row_to_dict(row: tuple[Any, ...]) -> dict[str, Any]:
    turn_id, text, voice, captured_at = row
    return {
        "id": turn_id,
        "text": text,
        "voice": voice,
        "captured_at": captured_at,
    }


__all__ = [
    "TABLE",
    "ChatTurnStore",
    "ensure_schema",
]
