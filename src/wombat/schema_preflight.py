"""ensure_all_schemas — the ONE pre-flight that applies every packaged migration (TK-203, CR3-1,
Q-104).

LIVE INCIDENT (2026-07-09): wombat's first-ever production boot against a brand-new Postgres
crashed at the eager pending-journal replay (``bootstrap.assemble_runtime`` -> ``PendingSet.
rebuild_from_journal``) with ``psycopg.errors.UndefinedTable: relation 'pending_journal' does not
exist``. No composition path ran the packaged migrations before that first pg read — every pg
module assumes its own table already exists (Q-46: schema application is the caller's concern,
never automatic inside a module's own read/write path).

This module closes that gap structurally: ``ensure_all_schemas(dsn)`` opens ONE psycopg
connection and runs the SIX packaged ``ensure_schema(conn)`` functions this product ships —
``wombat.queue``, ``wombat.domain.daily_ledger``, ``wombat.gate.pending_journal_pg``,
``wombat.behavior.event_log``, ``wombat.trail.schema``, ``wombat.settings_store`` (TK-240) — each
a ``CREATE TABLE/INDEX IF NOT EXISTS`` (NG-3: no migration framework, no version table), so
calling this on an already-current database is a safe no-op. ``bootstrap.assemble_runtime`` calls
this as the FIRST pg act on the ``replay_pending=True`` posture, before the TK-166 eager replay
(Q-104 ruling).
"""

from __future__ import annotations

import psycopg

from .behavior.event_log import ensure_schema as ensure_behavior_event_log_schema
from .domain.daily_ledger import ensure_schema as ensure_daily_ledger_schema
from .gate.pending_journal_pg import ensure_schema as ensure_pending_journal_schema
from .queue import ensure_schema as ensure_queue_schema
from .settings_store import ensure_schema as ensure_settings_store_schema
from .trail.schema import ensure_schema as ensure_action_trail_schema


def ensure_all_schemas(dsn: str) -> None:
    """Apply every packaged ``ensure_schema`` migration on ``dsn``, idempotently (CR3-1, Q-104).

    Opens ONE psycopg connection (context-managed — always closed), runs the six packaged
    ``ensure_schema(conn)`` functions in sequence, commits, and closes. Each is itself a
    ``CREATE ... IF NOT EXISTS`` (NG-3: no migration framework), so a second call against an
    already-current database raises nothing and changes nothing (idempotent). Deliberately never
    calls ``settings_store.import_legacy_settings_file`` (DEC-44) — the one-time legacy import is
    a separate, explicit-opt-in call the composition root makes on its own.
    """
    with psycopg.connect(dsn) as conn:
        ensure_queue_schema(conn)
        ensure_daily_ledger_schema(conn)
        ensure_pending_journal_schema(conn)
        ensure_behavior_event_log_schema(conn)
        ensure_action_trail_schema(conn)
        ensure_settings_store_schema(conn)
        conn.commit()
