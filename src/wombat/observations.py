"""observations — the append-only ambient-observability ledger (TK-310, DEC-68(a)/(c)).

Owns ``wombat_observations`` (``id`` BIGSERIAL PRIMARY KEY, ``channel`` TEXT NOT NULL, ``kind``
TEXT NOT NULL, ``started_at`` TIMESTAMPTZ NOT NULL, ``ended_at`` TIMESTAMPTZ NOT NULL, ``payload``
JSONB NOT NULL, ``day_key`` DATE NOT NULL). ``ensure_schema(conn)`` is the packaged, idempotent
``CREATE TABLE/INDEX IF NOT EXISTS`` (NG-3: no migration framework — the chat_turns/user_facts
sibling precedent, ``migrations/013_observations.sql``), wired as
``schema_preflight.ensure_all_schemas``'s TWELFTH entry.

``ObservationStore`` is a ``dsn``-injected psycopg reader/writer (the Q-46 lazy-connection
convention, mirroring ``chat_turns.ChatTurnStore``/``user_facts.UserFactsStore`` exactly — zero I/O
at construction): ``append_segment(channel, kind, started_at, ended_at, payload, day_key)`` inserts
ONE closed-segment row (append-only — no update/upsert path exists here). ``get_window(channel,
start, end)`` returns that channel's rows with ``started_at`` in ``[start, end]``, ascending.
``prune_older_than(days)`` deletes rows by ``started_at`` age and returns the deleted count.

LEDGER VOCAB (DEC-68(a)/(c)): the screen channel (``observe_screen.ScreenActivityCollector``)
writes rows with ``channel='screen'``, ``kind='app_segment'``, ``payload={"app": ..., "title":
...}``; ``day_key`` is the tz-local civil date (DEC-21 ``wombat_today``) ``started_at`` falls on.
Webcam/mic channels are later tickets' concern — this module enforces no vocabulary itself, it is
a plain store.

``_OBSERVATION_RETENTION_DAYS = 21`` (DEC-63 no-knob precedent): a pinned module constant, not a
setting. ``runtime.serve()`` calls ``prune_older_than(_OBSERVATION_RETENTION_DAYS)`` exactly once
at boot, guarded on the store existing (the ``scratchpad_store``/``chat_turn_store`` boot-prune
precedent) — see ``runtime.py``.

``CurrentActivity`` is the in-memory, mutable, single-slot snapshot of what the user is presently
doing (``app``, ``title``, ``since``) — updated in place by ``observe_screen.
ScreenActivityCollector`` as segments open/close, mirroring the ``voice.reply_context.
LastSpokenRegister`` plain-mutable-object convention (no locking needed: every touch point runs on
the SAME event loop). ``in_call: bool = False`` ships FROM BIRTH (TK-310) so a later mic ticket
only ever flips this field — no cross-level re-touch of this dataclass's shape. This module writes
NO mic code and sets ``in_call`` nowhere.

DEC-68(a) STRUCTURAL: no raw capture (pixels/screenshots/OCR/audio) has any home in this table or
this module — only closed, bounded, channel/kind-specific projected segments.

STRUCTURAL: this module imports NOTHING from ``wombat.bootstrap`` or ``wombat.runtime``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from importlib import resources
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

_MIGRATION_PACKAGE = "wombat.migrations"
_MIGRATION_FILENAME = "013_observations.sql"

TABLE = "wombat_observations"

# DEC-63 no-knob precedent: pinned retention window, not a setting.
_OBSERVATION_RETENTION_DAYS = 21


def ensure_schema(conn: psycopg.Connection[Any]) -> None:
    """Apply the packaged, idempotent ``wombat_observations`` migration on ``conn``.

    Reads ``migrations/013_observations.sql`` via ``importlib.resources`` and executes it as-is
    (``CREATE TABLE/INDEX IF NOT EXISTS`` — safe to call every process start, NG-3: no migration
    framework). Callers: tests and ``schema_preflight.ensure_all_schemas``.
    """
    sql = resources.files(_MIGRATION_PACKAGE).joinpath(_MIGRATION_FILENAME).read_text(
        encoding="utf-8"
    )
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()


class ObservationStore:
    """The Postgres-backed reader/writer over ``wombat_observations`` (TK-310, Q-46 conventions —
    mirrors ``chat_turns.ChatTurnStore``/``user_facts.UserFactsStore`` exactly).

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

    def append_segment(
        self,
        channel: str,
        kind: str,
        started_at: datetime,
        ended_at: datetime,
        payload: dict[str, Any],
        day_key: date,
    ) -> None:
        """Insert ONE closed-segment row. Append-only — there is no update/upsert path here; a
        segment, once written, is never revised."""
        conn = self._connection()
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO {TABLE} (channel, kind, started_at, ended_at, payload, day_key)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (channel, kind, started_at, ended_at, Jsonb(payload), day_key),
            )
        conn.commit()

    def get_window(self, channel: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
        """Return ``channel``'s rows with ``started_at`` in ``[start, end]``, ordered by
        ``started_at``."""
        conn = self._connection()
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT id, channel, kind, started_at, ended_at, payload, day_key
                FROM {TABLE}
                WHERE channel = %s AND started_at >= %s AND started_at <= %s
                ORDER BY started_at
                """,
                (channel, start, end),
            )
            rows = cur.fetchall()
        conn.commit()
        return [_row_to_dict(row) for row in rows]

    def prune_older_than(self, days: int) -> int:
        """Delete every row whose ``started_at`` is older than ``days`` days ago (across all
        channels). Returns the number of rows deleted."""
        conn = self._connection()
        with conn.cursor() as cur:
            cur.execute(
                f"""
                DELETE FROM {TABLE}
                WHERE started_at < now() - (%s * INTERVAL '1 day')
                """,
                (days,),
            )
            deleted = cur.rowcount
        conn.commit()
        return deleted


def _row_to_dict(row: tuple[Any, ...]) -> dict[str, Any]:
    obs_id, channel, kind, started_at, ended_at, payload, day_key = row
    return {
        "id": obs_id,
        "channel": channel,
        "kind": kind,
        "started_at": started_at,
        "ended_at": ended_at,
        "payload": payload,
        "day_key": day_key,
    }


@dataclass
class CurrentActivity:
    """The in-memory, mutable, single-slot snapshot of what the user is presently doing (TK-310).

    Updated in place by ``observe_screen.ScreenActivityCollector`` as segments open/close — no
    persistence, resets every process boot (mirrors ``voice.reply_context.LastSpokenRegister``'s
    plain-mutable-object convention; no locking needed, single event loop).

    ``in_call`` ships FROM BIRTH (``False`` always, here) so a later mic ticket only ever flips
    this field in place — this module and ``observe_screen.py`` write NO mic code and never touch
    it.
    """

    app: str | None = None
    title: str | None = None
    since: datetime | None = None
    in_call: bool = False


__all__ = [
    "TABLE",
    "CurrentActivity",
    "ObservationStore",
    "ensure_schema",
]
