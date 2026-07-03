"""ActionTrailWriter — the write-only surface over ``action_trail_projection`` (TK-146, Q-63).

Records proposed side-effects (draft emails, form submits, ...), transitions them
PENDING->DISPATCHED/CANCELLED idempotently, and records structural refusals
(``record_refusal``, TK-148's future call site). Does NOT render (TK-147) and does NOT
query/read (TK-147 owns reads) — this module is write-only.

Timestamps are CALLER-SUPPLIED aware-UTC ``datetime``s (stages pass ``ctx.clock()``); this
module never reads a clock.

All writes are SINGLE-STATEMENT SQL — no Python read-modify-write in the write path. This is
what makes a cog-worx Sweeper re-drive safe: a replayed call cannot double-apply or regress.
``record_proposal``/``record_refusal`` use ``INSERT ... ON CONFLICT (action_id) DO NOTHING``;
the two transitions use ONE guarded ``UPDATE ... WHERE action_id = %s AND status = 'pending'``
with ``COALESCE`` first-write-wins timestamps. When a guarded transition UPDATE affects 0
rows, a follow-up SELECT is used STRICTLY to discriminate the outcome (idempotent no-op vs a
genuine illegal transition) — never to decide what to write.

Legal transitions (monotonic, terminal-absorbing, Q-63): PENDING->DISPATCHED and
PENDING->CANCELLED only. DISPATCHED/CANCELLED/BLOCKED are absorbing. Re-applying the SAME
terminal transition is an idempotent no-op (``ALREADY_APPLIED``, timestamp unchanged, no
second row). Any OTHER transition — a cross-terminal move (e.g. cancelled->dispatched), an
attempted regression (e.g. dispatched->cancelled, "un-dispatching"), or any transition
attempted on a BLOCKED row — raises ``TrailTransitionError`` loudly, as does a transition
attempted against a missing ``action_id``.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

import psycopg

from wombat.trail.schema import ActionType, TrailStatus, ensure_schema

__all__ = [
    "ActionTrailWriter",
    "InsertResult",
    "TrailTransitionError",
    "TransitionResult",
    "ensure_schema",
]


class InsertResult(Enum):
    """The outcome of a single ``record_proposal``/``record_refusal`` call."""

    INSERTED = "inserted"
    ALREADY_PRESENT = "already_present"


class TransitionResult(Enum):
    """The outcome of a single ``mark_dispatched``/``mark_cancelled`` call."""

    APPLIED = "applied"
    ALREADY_APPLIED = "already_applied"


class TrailTransitionError(Exception):
    """Raised on an illegal ``action_trail_projection`` status transition.

    A programming error / inconsistency — never silent. Covers: a regression or
    cross-terminal move (e.g. dispatched->cancelled, cancelled->dispatched), any transition
    attempted on a BLOCKED row, and a transition attempted against a missing ``action_id``.
    """


class ActionTrailWriter:
    """The write-only surface over the ``action_trail_projection`` table (Q-63)."""

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

    def record_proposal(
        self,
        *,
        action_id: str,
        action_type: ActionType,
        human_summary: str,
        target: str,
        proposed_at: datetime,
    ) -> InsertResult:
        """Insert a new PENDING row for a proposed action.

        ``INSERT ... ON CONFLICT (action_id) DO NOTHING`` — a replayed insert (same
        ``action_id``, e.g. a Sweeper re-drive) is a no-op returning ``ALREADY_PRESENT``; a
        fresh row returns ``INSERTED``.
        """
        conn = self._connection()
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO action_trail_projection
                    (action_id, action_type, human_summary, target, proposed_at, status)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (action_id) DO NOTHING
                """,
                (
                    action_id,
                    action_type.value,
                    human_summary,
                    target,
                    proposed_at,
                    TrailStatus.PENDING.value,
                ),
            )
            inserted = cur.rowcount
        conn.commit()
        return InsertResult.INSERTED if inserted else InsertResult.ALREADY_PRESENT

    def record_refusal(
        self,
        *,
        action_id: str,
        human_summary: str,
        target: str,
        proposed_at: datetime,
    ) -> InsertResult:
        """Insert a TERMINAL BLOCKED row for a structural (taint-latch) refusal (TK-148 seam).

        Inserts directly at ``status='blocked'``, ``action_type='blocked_by_taint'`` — it
        NEVER transitions. Deliberately not rendered as cancelled (CON-4 audit honesty:
        structurally-refused is distinct from human-rejected). ``INSERT ... ON CONFLICT
        (action_id) DO NOTHING``, same idempotent-replay semantics as ``record_proposal``.
        """
        conn = self._connection()
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO action_trail_projection
                    (action_id, action_type, human_summary, target, proposed_at, status)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (action_id) DO NOTHING
                """,
                (
                    action_id,
                    ActionType.BLOCKED_BY_TAINT.value,
                    human_summary,
                    target,
                    proposed_at,
                    TrailStatus.BLOCKED.value,
                ),
            )
            inserted = cur.rowcount
        conn.commit()
        return InsertResult.INSERTED if inserted else InsertResult.ALREADY_PRESENT

    def mark_dispatched(self, action_id: str, dispatched_at: datetime) -> TransitionResult:
        """Transition ``action_id`` PENDING -> DISPATCHED (the only legal source state).

        ONE guarded ``UPDATE ... WHERE action_id = %s AND status = 'pending'`` with a
        ``COALESCE`` first-write-wins ``dispatched_at``. See module docstring for the full
        idempotency/error-discrimination contract.
        """
        conn = self._connection()
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE action_trail_projection
                SET status = %s, dispatched_at = COALESCE(dispatched_at, %s)
                WHERE action_id = %s AND status = %s
                RETURNING action_id
                """,
                (
                    TrailStatus.DISPATCHED.value,
                    dispatched_at,
                    action_id,
                    TrailStatus.PENDING.value,
                ),
            )
            applied = cur.fetchone() is not None
            conn.commit()
            if applied:
                return TransitionResult.APPLIED
            return self._discriminate_zero_rowcount(cur, action_id, TrailStatus.DISPATCHED)

    def mark_cancelled(self, action_id: str, cancelled_at: datetime) -> TransitionResult:
        """Transition ``action_id`` PENDING -> CANCELLED (the only legal source state).

        ONE guarded ``UPDATE ... WHERE action_id = %s AND status = 'pending'`` with a
        ``COALESCE`` first-write-wins ``cancelled_at``. See module docstring for the full
        idempotency/error-discrimination contract.
        """
        conn = self._connection()
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE action_trail_projection
                SET status = %s, cancelled_at = COALESCE(cancelled_at, %s)
                WHERE action_id = %s AND status = %s
                RETURNING action_id
                """,
                (
                    TrailStatus.CANCELLED.value,
                    cancelled_at,
                    action_id,
                    TrailStatus.PENDING.value,
                ),
            )
            applied = cur.fetchone() is not None
            conn.commit()
            if applied:
                return TransitionResult.APPLIED
            return self._discriminate_zero_rowcount(cur, action_id, TrailStatus.CANCELLED)

    def _discriminate_zero_rowcount(
        self,
        cur: psycopg.Cursor[Any],
        action_id: str,
        requested_status: TrailStatus,
    ) -> TransitionResult:
        """Discriminate a 0-rowcount guarded transition: idempotent no-op vs a real error.

        Called ONLY after the guarded UPDATE above already affected 0 rows — this SELECT is
        strictly for ERROR/IDEMPOTENCY DISCRIMINATION, never for deciding what to write; the
        guarded UPDATE itself remains the sole, single-statement, authoritative write.

        - No row for ``action_id`` at all: a transition against a missing action is an error.
        - The row is already in the SAME terminal status requested: idempotent re-apply,
          ``ALREADY_APPLIED`` (timestamps were already left untouched by the UPDATE's own
          ``WHERE status = 'pending'`` guard, since it matched 0 rows).
        - The row is in any OTHER status (a different terminal, or BLOCKED): an illegal
          transition (regression / cross-terminal / transition-on-blocked) — raise loudly.
        """
        cur.execute(
            "SELECT status FROM action_trail_projection WHERE action_id = %s",
            (action_id,),
        )
        row = cur.fetchone()
        self._connection().commit()
        if row is None:
            raise TrailTransitionError(
                f"cannot transition to {requested_status.value!r}: no action_trail_projection "
                f"row exists for action_id={action_id!r}"
            )
        current_status = row[0]
        if current_status == requested_status.value:
            return TransitionResult.ALREADY_APPLIED
        raise TrailTransitionError(
            f"illegal transition to {requested_status.value!r} for action_id={action_id!r}: "
            f"row is in status {current_status!r}, not 'pending'"
        )
