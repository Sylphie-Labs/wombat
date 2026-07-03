"""TK-146 — action-trail projection schema + writer acceptance criteria (EP-27, Q-63).

ALL tests in this module require a REAL Postgres and are gated on the ``WOMBAT_TEST_PG_DSN``
env var: absent it, tests are skipped LOUDLY (never faked, never CI-failed on a fresh clone).
Spin up a throwaway Postgres locally:

    docker run --rm -d -p 5433:5432 -e POSTGRES_PASSWORD=wombat postgres:16
    WOMBAT_TEST_PG_DSN=postgresql://postgres:wombat@localhost:5433/postgres

Each test calls ``ensure_schema`` and truncates the table first (``clean_table`` fixture) so a
shared local Postgres is safe to reuse.

  AC1 record_proposal writes {action_id, action_type, human_summary, target, proposed_at,
      status='pending'} — read back directly from the table.
  AC2 pending -> dispatched sets dispatched_at; a duplicate dispatch leaves status dispatched,
      dispatched_at unchanged, and adds NO second row (idempotent on action_id).
  AC3 a rejection (pending -> cancelled) populates cancelled_at.
  AC4 record_refusal writes an action_type='blocked_by_taint' row with status='blocked'.

Beyond the ACs (the ruled Q-63 semantics): illegal transitions raise TrailTransitionError
(regress, cross-terminal, transition-on-blocked, transition-on-missing); ALREADY_APPLIED on a
re-applied same-terminal transition; COALESCE first-write-wins timestamps; InsertResult
ALREADY_PRESENT on a replayed insert.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import psycopg
import pytest

from wombat.trail.schema import ActionType, ensure_schema
from wombat.trail.writer import (
    ActionTrailWriter,
    InsertResult,
    TrailTransitionError,
    TransitionResult,
)

_DSN = os.environ.get("WOMBAT_TEST_PG_DSN")

_requires_pg = pytest.mark.skipif(
    not _DSN,
    reason=(
        "WOMBAT_TEST_PG_DSN is not set — skipping ActionTrailWriter DB tests that require a "
        "real throwaway Postgres. Start one with:\n"
        "  docker run --rm -d -p 5433:5432 -e POSTGRES_PASSWORD=wombat postgres:16\n"
        "then export WOMBAT_TEST_PG_DSN=postgresql://postgres:wombat@localhost:5433/postgres"
    ),
)


@pytest.fixture
def clean_table() -> None:
    """Ensure the schema exists and the table is empty before each test."""
    assert _DSN is not None
    with psycopg.connect(_DSN) as conn:
        ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE action_trail_projection")
        conn.commit()


def _fetch_row(action_id: str) -> tuple[object, ...] | None:
    assert _DSN is not None
    with psycopg.connect(_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT action_id, seq, action_type, human_summary, target, proposed_at, "
            "status, dispatched_at, cancelled_at "
            "FROM action_trail_projection WHERE action_id = %s",
            (action_id,),
        )
        return cur.fetchone()


def _count(action_id: str | None = None) -> int:
    assert _DSN is not None
    with psycopg.connect(_DSN) as conn, conn.cursor() as cur:
        if action_id is None:
            cur.execute("SELECT count(*) FROM action_trail_projection")
        else:
            cur.execute(
                "SELECT count(*) FROM action_trail_projection WHERE action_id = %s",
                (action_id,),
            )
        row = cur.fetchone()
        assert row is not None
        return int(row[0])


# --------------------------------------------------------------------------------------- AC1


@_requires_pg
def test_ac1_record_proposal_writes_a_pending_row(clean_table: None) -> None:
    """A draft-email descriptor writes a row read back with the exact proposed fields."""
    assert _DSN is not None
    writer = ActionTrailWriter(_DSN)
    proposed_at = datetime(2026, 7, 2, 15, 0, tzinfo=UTC)
    try:
        result = writer.record_proposal(
            action_id="draft-1",
            action_type=ActionType.DRAFT_EMAIL,
            human_summary="Draft a reply to Jane about the Q3 budget",
            target="jane@example.com",
            proposed_at=proposed_at,
        )
        assert result is InsertResult.INSERTED

        row = _fetch_row("draft-1")
        assert row is not None
        action_id, seq, action_type, human_summary, target, row_proposed_at, status, \
            dispatched_at, cancelled_at = row
        assert action_id == "draft-1"
        assert isinstance(seq, int)
        assert action_type == "draft_email"
        assert human_summary == "Draft a reply to Jane about the Q3 budget"
        assert target == "jane@example.com"
        assert row_proposed_at == proposed_at
        assert status == "pending"
        assert dispatched_at is None
        assert cancelled_at is None
    finally:
        writer.close()


# --------------------------------------------------------------------------------------- AC2


@_requires_pg
def test_ac2_dispatch_sets_dispatched_at_and_duplicate_dispatch_is_idempotent(
    clean_table: None,
) -> None:
    """pending -> dispatched sets dispatched_at; a duplicate dispatch adds no second row."""
    assert _DSN is not None
    writer = ActionTrailWriter(_DSN)
    proposed_at = datetime(2026, 7, 2, 15, 0, tzinfo=UTC)
    dispatched_at = datetime(2026, 7, 2, 15, 5, tzinfo=UTC)
    try:
        writer.record_proposal(
            action_id="draft-2",
            action_type=ActionType.DRAFT_EMAIL,
            human_summary="Draft a reply",
            target="jane@example.com",
            proposed_at=proposed_at,
        )

        first = writer.mark_dispatched("draft-2", dispatched_at)
        assert first is TransitionResult.APPLIED

        row = _fetch_row("draft-2")
        assert row is not None
        assert row[6] == "dispatched"
        assert row[7] == dispatched_at

        # A duplicate dispatch write (e.g. a Sweeper re-drive) is a no-op.
        second = writer.mark_dispatched("draft-2", dispatched_at)
        assert second is TransitionResult.ALREADY_APPLIED

        row_after = _fetch_row("draft-2")
        assert row_after is not None
        assert row_after[6] == "dispatched"
        assert row_after[7] == dispatched_at
        assert _count("draft-2") == 1  # no second row
    finally:
        writer.close()


# --------------------------------------------------------------------------------------- AC3


@_requires_pg
def test_ac3_cancel_sets_cancelled_at(clean_table: None) -> None:
    """A rejection (pending -> cancelled) populates cancelled_at."""
    assert _DSN is not None
    writer = ActionTrailWriter(_DSN)
    proposed_at = datetime(2026, 7, 2, 15, 0, tzinfo=UTC)
    cancelled_at = datetime(2026, 7, 2, 15, 10, tzinfo=UTC)
    try:
        writer.record_proposal(
            action_id="draft-3",
            action_type=ActionType.DRAFT_EMAIL,
            human_summary="Draft a reply",
            target="jane@example.com",
            proposed_at=proposed_at,
        )

        result = writer.mark_cancelled("draft-3", cancelled_at)
        assert result is TransitionResult.APPLIED

        row = _fetch_row("draft-3")
        assert row is not None
        assert row[6] == "cancelled"
        assert row[8] == cancelled_at
        assert row[7] is None  # dispatched_at untouched
    finally:
        writer.close()


# --------------------------------------------------------------------------------------- AC4


@_requires_pg
def test_ac4_record_refusal_writes_a_blocked_row(clean_table: None) -> None:
    """A gate-refusal writes an action_type='blocked_by_taint' row with status='blocked'."""
    assert _DSN is not None
    writer = ActionTrailWriter(_DSN)
    proposed_at = datetime(2026, 7, 2, 15, 0, tzinfo=UTC)
    try:
        result = writer.record_refusal(
            action_id="refused-1",
            human_summary="Blocked: tainted content in a would-be form submission",
            target="https://example.com/form",
            proposed_at=proposed_at,
        )
        assert result is InsertResult.INSERTED

        row = _fetch_row("refused-1")
        assert row is not None
        assert row[2] == "blocked_by_taint"
        assert row[6] == "blocked"
        assert row[7] is None
        assert row[8] is None
    finally:
        writer.close()


# ------------------------------------------------------------------------- beyond-AC: insert


@_requires_pg
def test_record_proposal_replay_returns_already_present_and_adds_no_row(
    clean_table: None,
) -> None:
    """A replayed record_proposal (same action_id) is a no-op returning ALREADY_PRESENT."""
    assert _DSN is not None
    writer = ActionTrailWriter(_DSN)
    proposed_at = datetime(2026, 7, 2, 15, 0, tzinfo=UTC)
    try:
        first = writer.record_proposal(
            action_id="replay-1",
            action_type=ActionType.DRAFT_EMAIL,
            human_summary="original summary",
            target="jane@example.com",
            proposed_at=proposed_at,
        )
        assert first is InsertResult.INSERTED

        second = writer.record_proposal(
            action_id="replay-1",
            action_type=ActionType.DRAFT_EMAIL,
            human_summary="a DIFFERENT summary (must not overwrite)",
            target="jane@example.com",
            proposed_at=proposed_at,
        )
        assert second is InsertResult.ALREADY_PRESENT

        row = _fetch_row("replay-1")
        assert row is not None
        assert row[3] == "original summary"  # not overwritten
        assert _count("replay-1") == 1
    finally:
        writer.close()


@_requires_pg
def test_record_refusal_replay_returns_already_present_and_adds_no_row(
    clean_table: None,
) -> None:
    """A replayed record_refusal (same action_id) is a no-op returning ALREADY_PRESENT."""
    assert _DSN is not None
    writer = ActionTrailWriter(_DSN)
    proposed_at = datetime(2026, 7, 2, 15, 0, tzinfo=UTC)
    try:
        first = writer.record_refusal(
            action_id="refused-replay-1",
            human_summary="blocked",
            target="https://example.com/form",
            proposed_at=proposed_at,
        )
        assert first is InsertResult.INSERTED

        second = writer.record_refusal(
            action_id="refused-replay-1",
            human_summary="blocked",
            target="https://example.com/form",
            proposed_at=proposed_at,
        )
        assert second is InsertResult.ALREADY_PRESENT
        assert _count("refused-replay-1") == 1
    finally:
        writer.close()


# ------------------------------------------------------------------- beyond-AC: COALESCE ts


@_requires_pg
def test_dispatch_timestamp_is_first_write_wins_via_coalesce(clean_table: None) -> None:
    """A second dispatch write with a DIFFERENT timestamp does not overwrite dispatched_at."""
    assert _DSN is not None
    writer = ActionTrailWriter(_DSN)
    proposed_at = datetime(2026, 7, 2, 15, 0, tzinfo=UTC)
    first_ts = datetime(2026, 7, 2, 15, 5, tzinfo=UTC)
    later_ts = datetime(2026, 7, 2, 16, 30, tzinfo=UTC)
    try:
        writer.record_proposal(
            action_id="coalesce-1",
            action_type=ActionType.DRAFT_EMAIL,
            human_summary="Draft a reply",
            target="jane@example.com",
            proposed_at=proposed_at,
        )

        assert writer.mark_dispatched("coalesce-1", first_ts) is TransitionResult.APPLIED
        assert writer.mark_dispatched("coalesce-1", later_ts) is TransitionResult.ALREADY_APPLIED

        row = _fetch_row("coalesce-1")
        assert row is not None
        assert row[7] == first_ts  # unchanged by the second (later) write
    finally:
        writer.close()


@_requires_pg
def test_cancel_timestamp_is_first_write_wins_via_coalesce(clean_table: None) -> None:
    """A second cancel write with a DIFFERENT timestamp does not overwrite cancelled_at."""
    assert _DSN is not None
    writer = ActionTrailWriter(_DSN)
    proposed_at = datetime(2026, 7, 2, 15, 0, tzinfo=UTC)
    first_ts = datetime(2026, 7, 2, 15, 5, tzinfo=UTC)
    later_ts = datetime(2026, 7, 2, 16, 30, tzinfo=UTC)
    try:
        writer.record_proposal(
            action_id="coalesce-2",
            action_type=ActionType.DRAFT_EMAIL,
            human_summary="Draft a reply",
            target="jane@example.com",
            proposed_at=proposed_at,
        )

        assert writer.mark_cancelled("coalesce-2", first_ts) is TransitionResult.APPLIED
        assert writer.mark_cancelled("coalesce-2", later_ts) is TransitionResult.ALREADY_APPLIED

        row = _fetch_row("coalesce-2")
        assert row is not None
        assert row[8] == first_ts  # unchanged by the second (later) write
    finally:
        writer.close()


# --------------------------------------------------------- beyond-AC: illegal transitions


@_requires_pg
def test_illegal_transition_regress_dispatched_to_cancelled_raises(clean_table: None) -> None:
    """Attempting to cancel an already-DISPATCHED row (a regression) raises loudly."""
    assert _DSN is not None
    writer = ActionTrailWriter(_DSN)
    proposed_at = datetime(2026, 7, 2, 15, 0, tzinfo=UTC)
    try:
        writer.record_proposal(
            action_id="illegal-regress",
            action_type=ActionType.DRAFT_EMAIL,
            human_summary="Draft a reply",
            target="jane@example.com",
            proposed_at=proposed_at,
        )
        writer.mark_dispatched("illegal-regress", datetime(2026, 7, 2, 15, 5, tzinfo=UTC))

        with pytest.raises(TrailTransitionError):
            writer.mark_cancelled("illegal-regress", datetime(2026, 7, 2, 15, 10, tzinfo=UTC))

        row = _fetch_row("illegal-regress")
        assert row is not None
        assert row[6] == "dispatched"  # unchanged by the rejected attempt
        assert row[8] is None
    finally:
        writer.close()


@_requires_pg
def test_illegal_transition_cross_terminal_cancelled_to_dispatched_raises(
    clean_table: None,
) -> None:
    """Attempting to dispatch an already-CANCELLED row (cross-terminal) raises loudly."""
    assert _DSN is not None
    writer = ActionTrailWriter(_DSN)
    proposed_at = datetime(2026, 7, 2, 15, 0, tzinfo=UTC)
    try:
        writer.record_proposal(
            action_id="illegal-cross",
            action_type=ActionType.DRAFT_EMAIL,
            human_summary="Draft a reply",
            target="jane@example.com",
            proposed_at=proposed_at,
        )
        writer.mark_cancelled("illegal-cross", datetime(2026, 7, 2, 15, 5, tzinfo=UTC))

        with pytest.raises(TrailTransitionError):
            writer.mark_dispatched("illegal-cross", datetime(2026, 7, 2, 15, 10, tzinfo=UTC))

        row = _fetch_row("illegal-cross")
        assert row is not None
        assert row[6] == "cancelled"  # unchanged by the rejected attempt
        assert row[7] is None
    finally:
        writer.close()


@_requires_pg
def test_illegal_transition_on_blocked_row_raises(clean_table: None) -> None:
    """Any transition attempted on a BLOCKED row (from record_refusal) raises loudly."""
    assert _DSN is not None
    writer = ActionTrailWriter(_DSN)
    proposed_at = datetime(2026, 7, 2, 15, 0, tzinfo=UTC)
    try:
        writer.record_refusal(
            action_id="illegal-blocked",
            human_summary="blocked",
            target="https://example.com/form",
            proposed_at=proposed_at,
        )

        with pytest.raises(TrailTransitionError):
            writer.mark_dispatched("illegal-blocked", datetime(2026, 7, 2, 15, 5, tzinfo=UTC))
        with pytest.raises(TrailTransitionError):
            writer.mark_cancelled("illegal-blocked", datetime(2026, 7, 2, 15, 5, tzinfo=UTC))

        row = _fetch_row("illegal-blocked")
        assert row is not None
        assert row[6] == "blocked"  # unchanged by either rejected attempt
    finally:
        writer.close()


@_requires_pg
def test_transition_on_missing_action_id_raises(clean_table: None) -> None:
    """A transition attempted against a nonexistent action_id raises loudly."""
    assert _DSN is not None
    writer = ActionTrailWriter(_DSN)
    try:
        with pytest.raises(TrailTransitionError):
            writer.mark_dispatched("does-not-exist", datetime(2026, 7, 2, 15, 5, tzinfo=UTC))
        with pytest.raises(TrailTransitionError):
            writer.mark_cancelled("does-not-exist", datetime(2026, 7, 2, 15, 5, tzinfo=UTC))
    finally:
        writer.close()
