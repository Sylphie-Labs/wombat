"""external_store — Postgres persistence for caller-projected external-source items (TK-244,
DEC-45).

Owns ``wombat_external_items`` (``source`` TEXT NOT NULL, ``item_key`` TEXT NOT NULL, ``payload``
JSONB NOT NULL, ``occurs_at`` TIMESTAMPTZ NULL, ``fetched_at`` TIMESTAMPTZ NOT NULL,
``first_seen_at`` TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY (``source``, ``item_key``), plus
an index on (``source``, ``occurs_at``)). ``ensure_schema(conn)`` is the packaged, idempotent
``CREATE TABLE/INDEX IF NOT EXISTS`` (NG-3: no migration framework — the sibling precedent,
``migrations/008_external_items.sql``), wired as ``schema_preflight.ensure_all_schemas``'s SEVENTH
entry.

``ExternalItemStore`` is a ``dsn``-injected psycopg reader/writer (the Q-46 lazy-connection
convention, mirroring ``settings_store.SettingsStore`` exactly): ``upsert_many(source, items,
fetched_at)`` upserts a batch of items for one source, keyed on (``source``, ``item_key``) —
re-fetching the same item updates ``payload``/``fetched_at`` but never touches ``first_seen_at``
(write-once, ON CONFLICT DO UPDATE deliberately omits it). ``get_window(source, start, end)``
and ``get_recent(source, limit)`` return only that source's rows, ordered by ``occurs_at``.
``prune_older_than(days)`` deletes rows by ``fetched_at`` age and returns the deleted count.

``EXTERNAL_ITEMS_PRUNE_DAYS = 30`` (ruling v2.68 r3) ships here for TK-245's ``serve()`` line to
reference.

This store NEVER projects: every payload arrives caller-projected, already shaped by the source
integration that calls ``upsert_many``. In particular, a full message body has no home here
(DEC-45(d)) — this module never derives, stores, or reads one.

STRUCTURAL: this module imports NOTHING from ``wombat.bootstrap`` or ``wombat.runtime``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from importlib import resources
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

_MIGRATION_PACKAGE = "wombat.migrations"
_MIGRATION_FILENAME = "008_external_items.sql"

TABLE = "wombat_external_items"

# Ruling v2.68 r3: this constant ships here for TK-245's serve() line to reference.
EXTERNAL_ITEMS_PRUNE_DAYS = 30


@dataclass(frozen=True)
class ExternalItem:
    """One caller-projected external item, as passed to ``ExternalItemStore.upsert_many``."""

    item_key: str
    payload: dict[str, Any]
    occurs_at: datetime | None


def ensure_schema(conn: psycopg.Connection[Any]) -> None:
    """Apply the packaged, idempotent ``wombat_external_items`` migration on ``conn``.

    Reads ``migrations/008_external_items.sql`` via ``importlib.resources`` and executes it as-is
    (``CREATE TABLE/INDEX IF NOT EXISTS`` — safe to call every process start, NG-3: no migration
    framework). Callers: tests and ``schema_preflight.ensure_all_schemas``.
    """
    sql = resources.files(_MIGRATION_PACKAGE).joinpath(_MIGRATION_FILENAME).read_text(
        encoding="utf-8"
    )
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()


class ExternalItemStore:
    """The Postgres-backed reader/writer over ``wombat_external_items`` (TK-244, Q-46
    conventions — mirrors ``settings_store.SettingsStore`` exactly).

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

    def upsert_many(
        self, source: str, items: list[ExternalItem], fetched_at: datetime
    ) -> None:
        """Upsert ``items`` for ``source``, keyed on (``source``, ``item_key``).

        A re-fetch of the same (``source``, ``item_key``) updates ``payload`` and ``fetched_at``
        to the passed values; ``first_seen_at`` is write-once (the ON CONFLICT DO UPDATE clause
        never touches it, so it keeps its original INSERT-time value).
        """
        if not items:
            return
        conn = self._connection()
        with conn.cursor() as cur:
            for item in items:
                cur.execute(
                    f"""
                    INSERT INTO {TABLE}
                        (source, item_key, payload, occurs_at, fetched_at, first_seen_at)
                    VALUES (%s, %s, %s, %s, %s, now())
                    ON CONFLICT (source, item_key) DO UPDATE SET
                        payload = EXCLUDED.payload,
                        occurs_at = EXCLUDED.occurs_at,
                        fetched_at = EXCLUDED.fetched_at
                    """,
                    (
                        source,
                        item.item_key,
                        Jsonb(item.payload),
                        item.occurs_at,
                        fetched_at,
                    ),
                )
        conn.commit()

    def get_window(
        self, source: str, start: datetime, end: datetime
    ) -> list[dict[str, Any]]:
        """Return ``source``'s rows with ``occurs_at`` in ``[start, end]``, ordered by
        ``occurs_at``."""
        conn = self._connection()
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT item_key, payload, occurs_at, fetched_at, first_seen_at
                FROM {TABLE}
                WHERE source = %s AND occurs_at >= %s AND occurs_at <= %s
                ORDER BY occurs_at
                """,
                (source, start, end),
            )
            rows = cur.fetchall()
        conn.commit()
        return [_row_to_dict(row) for row in rows]

    def get_recent(self, source: str, limit: int) -> list[dict[str, Any]]:
        """Return ``source``'s ``limit`` most recent rows by ``occurs_at``, ordered by
        ``occurs_at`` ascending."""
        conn = self._connection()
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT item_key, payload, occurs_at, fetched_at, first_seen_at
                FROM {TABLE}
                WHERE source = %s
                ORDER BY occurs_at DESC NULLS LAST
                LIMIT %s
                """,
                (source, limit),
            )
            rows = cur.fetchall()
        conn.commit()
        rows.sort(key=lambda row: (row[2] is None, row[2]))
        return [_row_to_dict(row) for row in rows]

    def prune_older_than(self, days: int) -> int:
        """Delete every row whose ``fetched_at`` is older than ``days`` days ago (across all
        sources). Returns the number of rows deleted."""
        conn = self._connection()
        with conn.cursor() as cur:
            cur.execute(
                f"""
                DELETE FROM {TABLE}
                WHERE fetched_at < now() - (%s * INTERVAL '1 day')
                """,
                (days,),
            )
            deleted = cur.rowcount
        conn.commit()
        return deleted


def _row_to_dict(row: tuple[Any, ...]) -> dict[str, Any]:
    item_key, payload, occurs_at, fetched_at, first_seen_at = row
    return {
        "item_key": item_key,
        "payload": payload,
        "occurs_at": occurs_at,
        "fetched_at": fetched_at,
        "first_seen_at": first_seen_at,
    }


__all__ = [
    "EXTERNAL_ITEMS_PRUNE_DAYS",
    "TABLE",
    "ExternalItem",
    "ExternalItemStore",
    "ensure_schema",
]
