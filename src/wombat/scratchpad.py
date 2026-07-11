"""scratchpad — Postgres-backed scoped working memory (TK-247, DEC-46).

Owns ``wombat_scratchpad`` (``scope_key`` TEXT NOT NULL, ``entry_key`` TEXT NOT NULL, ``value``
JSONB NOT NULL, ``created_at`` TIMESTAMPTZ NOT NULL DEFAULT now(), ``updated_at`` TIMESTAMPTZ NOT
NULL DEFAULT now(), PRIMARY KEY (``scope_key``, ``entry_key``)). ``ensure_schema(conn)`` is the
packaged, idempotent ``CREATE TABLE IF NOT EXISTS`` (NG-3: no migration framework — the settings_
store/external_store sibling precedent, ``migrations/009_wombat_scratchpad.sql``), wired as
``schema_preflight.ensure_all_schemas``'s EIGHTH entry.

``ScratchpadStore`` is a ``dsn``-injected psycopg reader/writer (the Q-46 lazy-connection
convention, mirroring ``settings_store.SettingsStore``/``external_store.ExternalItemStore``
exactly — zero I/O at construction): ``put(scope_key, entry_key, value)`` upserts one entry,
bumping ``updated_at`` on every call while ``created_at`` is write-once (ON CONFLICT DO UPDATE
deliberately omits it). ``get_scope(scope_key)`` returns that scope's entries as a plain
``{entry_key: value}`` dict. ``delete_scope(scope_key)`` removes exactly that scope's rows.
``purge_stale(older_than_days)`` deletes rows by ``updated_at`` age and returns the deleted count.

``SCRATCHPAD_PURGE_DAYS = 14`` (ruling v2.68 r3 mirror, DEC-46(d)) ships here for
``runtime.serve()`` to reference, called exactly once at boot.

Posture (DEC-46(e)): writing scratch NEVER surfaces anything (CON-3) — this module has no read
path back into any brief, gate, or user-facing surface. Scratch is data inside the CON-1 boundary,
never trusted instruction. This is NOT a second queue (ASMP-2's one draining WombatQueue is
untouched), NOT the memory graph (EntityKG), NOT a chat log, and NOT gate input — a purely scoped
key/value working-memory surface for persisting intermediate work across steps/runs. No cross-
process locking: single-process only (ASMP-2).

STRUCTURAL: this module imports NOTHING from ``wombat.bootstrap`` or ``wombat.runtime``.
"""

from __future__ import annotations

from importlib import resources
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

_MIGRATION_PACKAGE = "wombat.migrations"
_MIGRATION_FILENAME = "009_wombat_scratchpad.sql"

TABLE = "wombat_scratchpad"

# Ruling v2.68 r3 mirror (DEC-46(d)): this constant ships here for runtime.serve()'s boot-time
# purge_stale call to reference.
SCRATCHPAD_PURGE_DAYS = 14


def ensure_schema(conn: psycopg.Connection[Any]) -> None:
    """Apply the packaged, idempotent ``wombat_scratchpad`` migration on ``conn``.

    Reads ``migrations/009_wombat_scratchpad.sql`` via ``importlib.resources`` and executes it
    as-is (``CREATE TABLE IF NOT EXISTS`` — safe to call every process start, NG-3: no migration
    framework). Callers: tests and ``schema_preflight.ensure_all_schemas``.
    """
    sql = resources.files(_MIGRATION_PACKAGE).joinpath(_MIGRATION_FILENAME).read_text(
        encoding="utf-8"
    )
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()


class ScratchpadStore:
    """The Postgres-backed reader/writer over ``wombat_scratchpad`` (TK-247, Q-46 conventions —
    mirrors ``settings_store.SettingsStore``/``external_store.ExternalItemStore`` exactly).

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

    def put(self, scope_key: str, entry_key: str, value: Any) -> None:
        """Upsert one entry at (``scope_key``, ``entry_key``).

        A re-put of the same (``scope_key``, ``entry_key``) updates ``value`` and bumps
        ``updated_at`` to now; ``created_at`` is write-once (the ON CONFLICT DO UPDATE clause
        never touches it, so it keeps its original INSERT-time value).
        """
        conn = self._connection()
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO {TABLE} (scope_key, entry_key, value, created_at, updated_at)
                VALUES (%s, %s, %s, now(), now())
                ON CONFLICT (scope_key, entry_key) DO UPDATE SET
                    value = EXCLUDED.value,
                    updated_at = now()
                """,
                (scope_key, entry_key, Jsonb(value)),
            )
        conn.commit()

    def get_scope(self, scope_key: str) -> dict[str, Any]:
        """Return ``scope_key``'s entries as a plain ``{entry_key: value}`` dict."""
        conn = self._connection()
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT entry_key, value FROM {TABLE} WHERE scope_key = %s", (scope_key,)
            )
            rows = cur.fetchall()
        conn.commit()
        return {row[0]: row[1] for row in rows}

    def delete_scope(self, scope_key: str) -> None:
        """Remove exactly ``scope_key``'s rows."""
        conn = self._connection()
        with conn.cursor() as cur:
            cur.execute(f"DELETE FROM {TABLE} WHERE scope_key = %s", (scope_key,))
        conn.commit()

    def purge_stale(self, older_than_days: int) -> int:
        """Delete every row whose ``updated_at`` is older than ``older_than_days`` days ago
        (across all scopes). Returns the number of rows deleted."""
        conn = self._connection()
        with conn.cursor() as cur:
            cur.execute(
                f"""
                DELETE FROM {TABLE}
                WHERE updated_at < now() - (%s * INTERVAL '1 day')
                """,
                (older_than_days,),
            )
            deleted = cur.rowcount
        conn.commit()
        return deleted


__all__ = [
    "SCRATCHPAD_PURGE_DAYS",
    "TABLE",
    "ScratchpadStore",
    "ensure_schema",
]
