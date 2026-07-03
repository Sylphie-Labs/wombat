"""action_trail_projection — the typed read contract (TK-146, EP-27, Q-63).

Exports the closed vocabularies (``ActionType``, ``TrailStatus``), the typed row
(``TrailRow``), table/column-name string constants, and ``ensure_schema(conn)`` for the
``action_trail_projection`` table (``migrations/004_action_trail.sql``). TK-147's reader
builds against this module instead of raw string literals (Q-63 TK-147 seam) — this ticket
(TK-146) is write-only and exposes no query surface of its own.

NO pg enum types (migration-hostile, Q-63) — ``action_type`` and ``status`` are TEXT columns
in Postgres; the closed vocabularies are enforced writer-side as these str-Enums, mirroring
``EventClass`` in ``rating/params.py``. Later dispatch tickets ADD members deliberately.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from importlib import resources
from typing import Any

import psycopg

_MIGRATION_PACKAGE = "wombat.migrations"
_MIGRATION_FILENAME = "004_action_trail.sql"

# --- table/column-name constants (so TK-147 is not coupled to raw literals) ---------------

TABLE = "action_trail_projection"

COL_ACTION_ID = "action_id"
COL_SEQ = "seq"
COL_ACTION_TYPE = "action_type"
COL_HUMAN_SUMMARY = "human_summary"
COL_TARGET = "target"
COL_PROPOSED_AT = "proposed_at"
COL_STATUS = "status"
COL_DISPATCHED_AT = "dispatched_at"
COL_CANCELLED_AT = "cancelled_at"


class ActionType(Enum):
    """Closed set of proposed-side-effect kinds (Q-63). Later dispatch tickets add members."""

    DRAFT_EMAIL = "draft_email"
    FORM_SUBMIT = "form_submit"
    LOGIN_HANDOFF = "login_handoff"
    BLOCKED_BY_TAINT = "blocked_by_taint"


class TrailStatus(Enum):
    """Closed set of ``action_trail_projection.status`` values (Q-63)."""

    PENDING = "pending"
    DISPATCHED = "dispatched"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class TrailRow:
    """One ``action_trail_projection`` row — the typed row TK-147's reader returns."""

    action_id: str
    seq: int
    action_type: str
    human_summary: str
    target: str
    proposed_at: datetime
    status: str
    dispatched_at: datetime | None
    cancelled_at: datetime | None


def ensure_schema(conn: psycopg.Connection[Any]) -> None:
    """Apply the packaged, idempotent ``action_trail_projection`` migration on ``conn``.

    Reads ``migrations/004_action_trail.sql`` via ``importlib.resources`` and executes it
    as-is (``CREATE TABLE IF NOT EXISTS`` — safe to call every process start, NG-3: no
    migration framework). Callers: tests and the composition root.
    """
    sql = resources.files(_MIGRATION_PACKAGE).joinpath(_MIGRATION_FILENAME).read_text(
        encoding="utf-8"
    )
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()
