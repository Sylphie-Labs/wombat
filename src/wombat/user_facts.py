"""user_facts — the durable what-wombat-knows-about-the-user store (TK-294, DEC-65d).

Owns ``wombat_user_facts`` (``fact_key`` TEXT PRIMARY KEY, ``fact`` TEXT NOT NULL, ``source`` TEXT
NOT NULL, ``first_seen_at`` TIMESTAMPTZ NOT NULL DEFAULT now(), ``updated_at`` TIMESTAMPTZ NOT NULL
DEFAULT now()). ``ensure_schema(conn)`` is the packaged, idempotent ``CREATE TABLE IF NOT EXISTS``
(NG-3: no migration framework — the settings_store/external_store/scratchpad/seen_ledger sibling
precedent, ``migrations/011_user_facts.sql``), wired as ``schema_preflight.ensure_all_schemas``'s
TENTH entry.

``UserFactsStore`` is a ``dsn``-injected psycopg reader/writer (the Q-46 lazy-connection
convention, mirroring ``scratchpad.ScratchpadStore``/``sources.seen_ledger.SeenLedger`` exactly —
zero I/O at construction). ``upsert_fact(fact_key, fact, source)`` upserts one row keyed on the
caller-supplied ``fact_key`` (TK-297 derives it deterministically from normalized fact text): a
re-upsert of the same ``fact_key`` updates ``fact``/``source``/``updated_at`` while
``first_seen_at`` stays write-once (ON CONFLICT DO UPDATE deliberately omits it).
``list_facts(limit)`` returns the ``limit`` most recently updated rows, ``updated_at`` DESC.
``delete_fact(fact_key)`` removes exactly that row. ``count()`` returns the total row count.

HARD CAP (DEC-63 no-knob precedent): ``_MAX_FACTS = 200`` is a pinned module constant, not a
setting. Upserting a NEW ``fact_key`` (never seen before) while the store is already at the cap
evicts the oldest-updated row(s) first — just enough to admit the new row without exceeding the
cap — logging exactly one loud WARNING naming the evicted key(s). Re-upserting an EXISTING
``fact_key`` never triggers eviction (the row count does not grow).

DURABLE FOREVER (DEC-65d): there is no purge-on-boot, no age-based pruning, and no TTL anywhere in
this module. This is deliberate — accumulating what wombat knows about the user is the point.
``scratchpad.ScratchpadStore`` (DEC-46) was NOT reused for this purpose precisely because its
14-day purge-on-boot lifecycle is the opposite of what this table needs.

CON-6 CUSTODY NOTE: rows in this table hold what the user SAID or DID, never wombat's inferred
motive or judgment about the user. This module stores whatever ``fact``/``source`` a caller passes
verbatim — it enforces nothing about content. TK-297 owns enforcing this custody boundary at the
only organic write path into this store.

``source`` is the DEC-66 provenance spine (``dream`` | ``derived`` | ``behavior`` | ``told``).
This module never collapses, defaults, or validates it — every caller must pass an explicit value.

STRUCTURAL: this module imports NOTHING from ``wombat.bootstrap`` or ``wombat.runtime``. This
ticket (TK-294) wires nothing — no runtime/bootstrap composition, no reads, no writes beyond the
seams above. TK-296 reads this store; TK-297 writes it via the organic extraction path.
"""

from __future__ import annotations

import logging
from importlib import resources
from typing import Any

import psycopg

_log = logging.getLogger(__name__)

_MIGRATION_PACKAGE = "wombat.migrations"
_MIGRATION_FILENAME = "011_user_facts.sql"

TABLE = "wombat_user_facts"

# DEC-63 no-knob precedent: pinned hard cap, not a setting.
_MAX_FACTS = 200


def ensure_schema(conn: psycopg.Connection[Any]) -> None:
    """Apply the packaged, idempotent ``wombat_user_facts`` migration on ``conn``.

    Reads ``migrations/011_user_facts.sql`` via ``importlib.resources`` and executes it as-is
    (``CREATE TABLE IF NOT EXISTS`` — safe to call every process start, NG-3: no migration
    framework). Callers: tests and ``schema_preflight.ensure_all_schemas``.
    """
    sql = resources.files(_MIGRATION_PACKAGE).joinpath(_MIGRATION_FILENAME).read_text(
        encoding="utf-8"
    )
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()


class UserFactsStore:
    """The Postgres-backed reader/writer over ``wombat_user_facts`` (TK-294, Q-46 conventions —
    mirrors ``scratchpad.ScratchpadStore``/``sources.seen_ledger.SeenLedger`` exactly).

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

    def upsert_fact(self, fact_key: str, fact: str, source: str) -> None:
        """Upsert one row at ``fact_key``.

        A re-upsert of an EXISTING ``fact_key`` updates ``fact``/``source`` and bumps
        ``updated_at`` to now; ``first_seen_at`` is write-once. Upserting a NEW ``fact_key`` while
        the store is already at the ``_MAX_FACTS`` cap evicts the oldest-updated row(s) first (just
        enough to admit this row), logging one loud WARNING naming the evicted key(s).
        """
        conn = self._connection()
        with conn.cursor() as cur:
            cur.execute(f"SELECT 1 FROM {TABLE} WHERE fact_key = %s", (fact_key,))
            is_new_key = cur.fetchone() is None

            if is_new_key:
                cur.execute(f"SELECT COUNT(*) FROM {TABLE}")
                count_row = cur.fetchone()
                current_count = count_row[0] if count_row is not None else 0
                if current_count >= _MAX_FACTS:
                    overflow = current_count - _MAX_FACTS + 1
                    cur.execute(
                        f"""
                        DELETE FROM {TABLE}
                        WHERE fact_key IN (
                            SELECT fact_key FROM {TABLE} ORDER BY updated_at ASC LIMIT %s
                        )
                        RETURNING fact_key
                        """,
                        (overflow,),
                    )
                    evicted = [row[0] for row in cur.fetchall()]
                    _log.warning(
                        "UserFactsStore: at the %d-row cap, evicting %d oldest-updated fact(s) "
                        "to admit new fact_key %r: %s",
                        _MAX_FACTS,
                        len(evicted),
                        fact_key,
                        evicted,
                    )

            cur.execute(
                f"""
                INSERT INTO {TABLE} (fact_key, fact, source, first_seen_at, updated_at)
                VALUES (%s, %s, %s, now(), now())
                ON CONFLICT (fact_key) DO UPDATE SET
                    fact = EXCLUDED.fact,
                    source = EXCLUDED.source,
                    updated_at = now()
                """,
                (fact_key, fact, source),
            )
        conn.commit()

    def list_facts(self, limit: int) -> list[dict[str, Any]]:
        """Return the ``limit`` most recently updated rows, ``updated_at`` DESC."""
        conn = self._connection()
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT fact_key, fact, source, first_seen_at, updated_at
                FROM {TABLE}
                ORDER BY updated_at DESC
                LIMIT %s
                """,
                (limit,),
            )
            rows = cur.fetchall()
        conn.commit()
        return [_row_to_dict(row) for row in rows]

    def delete_fact(self, fact_key: str) -> None:
        """Remove exactly ``fact_key``'s row, if present."""
        conn = self._connection()
        with conn.cursor() as cur:
            cur.execute(f"DELETE FROM {TABLE} WHERE fact_key = %s", (fact_key,))
        conn.commit()

    def count(self) -> int:
        """Return the total number of rows in the store."""
        conn = self._connection()
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM {TABLE}")
            row = cur.fetchone()
        conn.commit()
        return int(row[0]) if row is not None else 0


def _row_to_dict(row: tuple[Any, ...]) -> dict[str, Any]:
    fact_key, fact, source, first_seen_at, updated_at = row
    return {
        "fact_key": fact_key,
        "fact": fact,
        "source": source,
        "first_seen_at": first_seen_at,
        "updated_at": updated_at,
    }


__all__ = [
    "TABLE",
    "UserFactsStore",
    "ensure_schema",
]
