"""seen_ledger — the persisted exactly-once dedup ledger around the registry's enqueue seam
(TK-286, DEC-63a).

LIVE DEFECT (logs/runtime-20260720-192648.log, 2026-07-20): the same gmail message flushed 5x
~10min apart. ``GmailPoller`` re-emits every in-window message on each 300s poll (by design — it
has no cursor, non_goal), and ``WombatQueue.enqueue``'s ``ON CONFLICT (idempotency_key) DO
NOTHING`` dedup only holds while the row is LIVE in ``wombat_queue`` — ``ack()`` DELETEs the row
(``queue.py``), so the very next poll re-inserts cleanly and the item re-enters the whole pipeline.

This module closes that gap at the ONE seam every source shares: the registry's enqueue
(``sources.registry.SourceRegistry`` -> ``Enqueuer.enqueue``). Unlike ``wombat_queue``, this
ledger's rows are NEVER deleted on ack — a source item, once seen, stays seen for the life of the
database (no pruning in v1, non_goal).

Owns ``wombat_seen_events`` (``idempotency_key`` TEXT PRIMARY KEY, ``payload_fingerprint`` TEXT
NOT NULL, ``first_seen_at``/``last_seen_at`` TIMESTAMPTZ). ``ensure_schema(conn)`` is the packaged,
idempotent ``CREATE TABLE IF NOT EXISTS`` (NG-3: no migration framework — the settings_store/
external_store/scratchpad sibling precedent, ``migrations/010_seen_events.sql``), wired as
``schema_preflight.ensure_all_schemas``'s NINTH entry.

``SeenLedger`` is a ``dsn``-injected psycopg reader/writer (the Q-46 lazy-connection convention,
mirroring ``external_store.ExternalItemStore``/``scratchpad.ScratchpadStore`` exactly — zero I/O
at construction). ``fingerprint(payload)`` is a pure sha256 hash over
``json.dumps(payload, sort_keys=True)`` — stable regardless of key order, so the SAME logical
payload always hashes the same. ``seen(key)`` returns the stored fingerprint for ``key``, or
``None`` if never seen. ``record(key, fingerprint)`` upserts (key, fingerprint, last_seen_at=now())
— ``first_seen_at`` is write-once (ON CONFLICT DO UPDATE deliberately omits it).

``DedupingEnqueuer`` wraps an inner ``Enqueuer`` (``sources.registry.Enqueuer`` protocol) with a
``SeenLedger``: an item whose ``idempotency_key`` is already recorded with an UNCHANGED
``fingerprint`` is a structural no-op — ``inner.enqueue`` is never called, and
``EnqueueResult.ALREADY_QUEUED`` is returned directly (DEBUG-logged). A never-seen key, OR a known
key whose payload fingerprint has CHANGED (e.g. an updated calendar event legitimately re-entering
the pipeline), is passed through to ``inner.enqueue`` verbatim; the ledger is updated ONLY on a
non-raising return (``QUEUED`` or ``ALREADY_QUEUED``) — any raise (``QueueFullError`` etc.)
propagates UNRECORDED, so a later poll retries the same event against ``inner`` again
(at-least-once delivery is preserved; this ledger only prevents a SUCCESSFUL enqueue from
recurring).

DEC-57 (chat-always-answers) pin: a chat turn's ``idempotency_key`` is minted from a fresh
ephemeral natural id (``domain.item_identity.new_ephemeral_natural_id``, uuid4-based) every turn,
so it is NEVER-SEEN by construction — the first (and only) enqueue of a chat turn always passes
through to ``inner`` unchanged. This module does nothing chat-specific; the guarantee falls out of
chat's own key minting plus this class's never-seen-passes-through behavior.
"""

from __future__ import annotations

import hashlib
import json
import logging
from importlib import resources
from typing import Any, Protocol

import psycopg

from wombat.queue import EnqueueResult, QueueItem
from wombat.sources.registry import Enqueuer

_log = logging.getLogger(__name__)

_MIGRATION_PACKAGE = "wombat.migrations"
_MIGRATION_FILENAME = "010_seen_events.sql"

TABLE = "wombat_seen_events"


def ensure_schema(conn: psycopg.Connection[Any]) -> None:
    """Apply the packaged, idempotent ``wombat_seen_events`` migration on ``conn``.

    Reads ``migrations/010_seen_events.sql`` via ``importlib.resources`` and executes it as-is
    (``CREATE TABLE IF NOT EXISTS`` — safe to call every process start, NG-3: no migration
    framework). Callers: tests and ``schema_preflight.ensure_all_schemas``.
    """
    sql = resources.files(_MIGRATION_PACKAGE).joinpath(_MIGRATION_FILENAME).read_text(
        encoding="utf-8"
    )
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()


def fingerprint(payload: dict[str, Any]) -> str:
    """A stable sha256 hash of ``payload`` — the SAME logical payload always hashes the same,
    regardless of key order (``json.dumps(payload, sort_keys=True)``)."""
    encoded = json.dumps(payload, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class SeenLedger:
    """The Postgres-backed reader/writer over ``wombat_seen_events`` (TK-286, Q-46 conventions —
    mirrors ``external_store.ExternalItemStore``/``scratchpad.ScratchpadStore`` exactly).

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

    def seen(self, idempotency_key: str) -> str | None:
        """Return the stored ``payload_fingerprint`` for ``idempotency_key``, or ``None`` if this
        key has never been recorded."""
        conn = self._connection()
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT payload_fingerprint FROM {TABLE} WHERE idempotency_key = %s",
                (idempotency_key,),
            )
            row = cur.fetchone()
        conn.commit()
        return row[0] if row is not None else None

    def record(self, idempotency_key: str, payload_fingerprint: str) -> None:
        """Upsert (``idempotency_key``, ``payload_fingerprint``), bumping ``last_seen_at`` to now.
        ``first_seen_at`` is write-once (the ON CONFLICT DO UPDATE clause never touches it)."""
        conn = self._connection()
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO {TABLE}
                    (idempotency_key, payload_fingerprint, first_seen_at, last_seen_at)
                VALUES (%s, %s, now(), now())
                ON CONFLICT (idempotency_key) DO UPDATE SET
                    payload_fingerprint = EXCLUDED.payload_fingerprint,
                    last_seen_at = now()
                """,
                (idempotency_key, payload_fingerprint),
            )
        conn.commit()


class SeenLedgerLike(Protocol):
    """The two ``SeenLedger`` methods ``DedupingEnqueuer`` needs (mirrors ``registry.Enqueuer``'s
    minimal-seam convention, Q-36/Q-46) — lets unit tests inject an in-memory fake instead of a
    real ``SeenLedger``; the one DB-backed test wires the real thing."""

    def seen(self, idempotency_key: str) -> str | None: ...

    def record(self, idempotency_key: str, payload_fingerprint: str) -> None: ...


class DedupingEnqueuer:
    """Wraps an inner ``Enqueuer`` with a ``SeenLedger`` so a source item, once successfully
    enqueued, never re-enters ``inner`` on a later poll with an unchanged payload (TK-286,
    DEC-63a) — see the module docstring for the full contract.
    """

    def __init__(self, inner: Enqueuer, ledger: SeenLedgerLike) -> None:
        self._inner = inner
        self._ledger = ledger

    def enqueue(self, item: QueueItem) -> EnqueueResult:
        item_fingerprint = fingerprint(item.payload)
        stored_fingerprint = self._ledger.seen(item.idempotency_key)
        if stored_fingerprint is not None and stored_fingerprint == item_fingerprint:
            _log.debug(
                "DedupingEnqueuer: %r already seen with an unchanged payload; skipping inner "
                "enqueue and returning ALREADY_QUEUED",
                item.idempotency_key,
            )
            return EnqueueResult.ALREADY_QUEUED
        result = self._inner.enqueue(item)
        # Only reached on a non-raising return -- a raise (QueueFullError etc.) propagates
        # UNRECORDED so a later poll retries this exact event against inner again.
        self._ledger.record(item.idempotency_key, item_fingerprint)
        return result


__all__ = [
    "TABLE",
    "DedupingEnqueuer",
    "SeenLedger",
    "SeenLedgerLike",
    "ensure_schema",
    "fingerprint",
]
