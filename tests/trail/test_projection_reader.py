"""TK-147 — action-trail reader + human-readable append-only log renderer (EP-27, Q-63/Q-89).

ALL tests in this module require a REAL Postgres and are gated on the ``WOMBAT_TEST_PG_DSN``
env var: absent it, tests are skipped LOUDLY (never faked, never CI-failed on a fresh clone).
Spin up a throwaway Postgres locally:

    docker run --rm -d -p 5433:5432 -e POSTGRES_PASSWORD=wombat postgres:16
    WOMBAT_TEST_PG_DSN=postgresql://postgres:wombat@localhost:5433/postgres

Each test calls ``ensure_schema`` and truncates the table first (``clean_table`` fixture, same
convention as ``test_projection_writer.py``) so a shared local Postgres is safe to reuse. Rows
are written through the REAL ``ActionTrailWriter`` (never hand-inserted) and rendered through
the REAL ``ActionTrailReader``/``ActionTrailRenderer`` — this module proves the end-to-end
write -> read -> render wire, not a mocked slice of it.

  AC1 two proposals -> render() -> exactly two '[PROPOSED ...]' lines in seq order, ruled
      format, zero action_id/JSON substrings; a second render() appends nothing.
  AC2 mark_dispatched on row 1 -> render() -> ONE new indented '[DISPATCHED ...]' line at the
      tail; all prior lines byte-unchanged (prefix identity).
  AC3 mark_cancelled on row 2 -> same shape with '[CANCELLED ...]'.
  AC4 a NEW Renderer instance over the same log+sidecar -> render() re-emits nothing; the log
      is never truncated (existing content survives construction).
  AC5 record_refusal -> render() -> exactly one '[BLOCKED ...]' line; a subsequent render()
      adds nothing.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest

from wombat.trail.renderer import ActionTrailRenderer
from wombat.trail.schema import ActionType, ensure_schema
from wombat.trail.writer import ActionTrailWriter

_DSN = os.environ.get("WOMBAT_TEST_PG_DSN")

_requires_pg = pytest.mark.skipif(
    not _DSN,
    reason=(
        "WOMBAT_TEST_PG_DSN is not set — skipping ActionTrailReader/Renderer DB tests that "
        "require a real throwaway Postgres. Start one with:\n"
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


def _lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


# --------------------------------------------------------------------------------------- AC1


@_requires_pg
def test_ac1_two_proposals_render_as_proposed_lines_in_seq_order(
    clean_table: None, tmp_path: Path
) -> None:
    """Two proposals render as two ruled '[PROPOSED ...]' lines, in insertion order, with no
    action_id/JSON substrings; a second render() appends nothing (file bytes unchanged)."""
    assert _DSN is not None
    log_path = tmp_path / "wombat-trail.log"
    writer = ActionTrailWriter(_DSN)
    proposed_at_1 = datetime(2026, 7, 2, 15, 0, tzinfo=UTC)
    proposed_at_2 = datetime(2026, 7, 2, 15, 5, tzinfo=UTC)
    try:
        writer.record_proposal(
            action_id="draft-1",
            action_type=ActionType.DRAFT_EMAIL,
            human_summary="Draft a reply to Jane about the Q3 budget",
            target="jane@example.com",
            proposed_at=proposed_at_1,
        )
        writer.record_proposal(
            action_id="draft-2",
            action_type=ActionType.FORM_SUBMIT,
            human_summary="Submit the vendor renewal form",
            target="https://example.com/form",
            proposed_at=proposed_at_2,
        )

        renderer = ActionTrailRenderer(_DSN, log_path)
        try:
            renderer.render()

            lines = _lines(log_path)
            assert lines == [
                f"[PROPOSED {proposed_at_1.isoformat()}] draft_email: "
                "Draft a reply to Jane about the Q3 budget",
                f"[PROPOSED {proposed_at_2.isoformat()}] form_submit: "
                "Submit the vendor renewal form",
            ]

            full_text = log_path.read_text(encoding="utf-8")
            assert "draft-1" not in full_text
            assert "draft-2" not in full_text
            assert "{" not in full_text
            assert "}" not in full_text

            first_bytes = log_path.read_bytes()
            renderer.render()
            assert log_path.read_bytes() == first_bytes
        finally:
            renderer.close()
    finally:
        writer.close()


# --------------------------------------------------------------------------------------- AC2


@_requires_pg
def test_ac2_dispatch_appends_one_indented_line_prior_lines_unchanged(
    clean_table: None, tmp_path: Path
) -> None:
    """mark_dispatched on row 1 -> render() appends ONE indented '[DISPATCHED ...]' line at the
    tail, repeating action_type+summary; all prior bytes are unchanged (prefix identity)."""
    assert _DSN is not None
    log_path = tmp_path / "wombat-trail.log"
    writer = ActionTrailWriter(_DSN)
    proposed_at = datetime(2026, 7, 2, 15, 0, tzinfo=UTC)
    dispatched_at = datetime(2026, 7, 2, 15, 5, tzinfo=UTC)
    try:
        writer.record_proposal(
            action_id="draft-1",
            action_type=ActionType.DRAFT_EMAIL,
            human_summary="Draft a reply to Jane about the Q3 budget",
            target="jane@example.com",
            proposed_at=proposed_at,
        )

        renderer = ActionTrailRenderer(_DSN, log_path)
        try:
            renderer.render()
            prior_bytes = log_path.read_bytes()

            writer.mark_dispatched("draft-1", dispatched_at)
            renderer.render()

            new_bytes = log_path.read_bytes()
            assert new_bytes.startswith(prior_bytes)  # prior lines byte-unchanged

            appended = new_bytes[len(prior_bytes) :].decode("utf-8")
            assert appended == (
                f"  [DISPATCHED {dispatched_at.isoformat()}] draft_email: "
                "Draft a reply to Jane about the Q3 budget\n"
            )

            # A repeat render appends nothing further.
            after_repeat = log_path.read_bytes()
            renderer.render()
            assert log_path.read_bytes() == after_repeat
        finally:
            renderer.close()
    finally:
        writer.close()


# --------------------------------------------------------------------------------------- AC3


@_requires_pg
def test_ac3_cancel_appends_one_indented_line_prior_lines_unchanged(
    clean_table: None, tmp_path: Path
) -> None:
    """mark_cancelled on row 2 -> render() appends ONE indented '[CANCELLED ...]' line at the
    tail, repeating action_type+summary; all prior bytes are unchanged (prefix identity)."""
    assert _DSN is not None
    log_path = tmp_path / "wombat-trail.log"
    writer = ActionTrailWriter(_DSN)
    proposed_at = datetime(2026, 7, 2, 15, 0, tzinfo=UTC)
    cancelled_at = datetime(2026, 7, 2, 15, 10, tzinfo=UTC)
    try:
        writer.record_proposal(
            action_id="draft-2",
            action_type=ActionType.FORM_SUBMIT,
            human_summary="Submit the vendor renewal form",
            target="https://example.com/form",
            proposed_at=proposed_at,
        )

        renderer = ActionTrailRenderer(_DSN, log_path)
        try:
            renderer.render()
            prior_bytes = log_path.read_bytes()

            writer.mark_cancelled("draft-2", cancelled_at)
            renderer.render()

            new_bytes = log_path.read_bytes()
            assert new_bytes.startswith(prior_bytes)  # prior lines byte-unchanged

            appended = new_bytes[len(prior_bytes) :].decode("utf-8")
            assert appended == (
                f"  [CANCELLED {cancelled_at.isoformat()}] form_submit: "
                "Submit the vendor renewal form\n"
            )
        finally:
            renderer.close()
    finally:
        writer.close()


# --------------------------------------------------------------------------------------- AC4


@_requires_pg
def test_ac4_restart_new_renderer_instance_re_emits_nothing(
    clean_table: None, tmp_path: Path
) -> None:
    """A NEW Renderer instance over the same log+sidecar re-emits nothing on render(); the log
    is only ever opened in append mode — existing content survives construction untouched."""
    assert _DSN is not None
    log_path = tmp_path / "wombat-trail.log"
    writer = ActionTrailWriter(_DSN)
    proposed_at = datetime(2026, 7, 2, 15, 0, tzinfo=UTC)
    try:
        writer.record_proposal(
            action_id="draft-1",
            action_type=ActionType.DRAFT_EMAIL,
            human_summary="Draft a reply to Jane about the Q3 budget",
            target="jane@example.com",
            proposed_at=proposed_at,
        )

        first_renderer = ActionTrailRenderer(_DSN, log_path)
        try:
            first_renderer.render()
        finally:
            first_renderer.close()

        existing_bytes = log_path.read_bytes()
        assert existing_bytes  # something was actually rendered

        second_renderer = ActionTrailRenderer(_DSN, log_path)
        try:
            # Construction alone must not truncate/mutate the log.
            assert log_path.read_bytes() == existing_bytes
            second_renderer.render()
            assert log_path.read_bytes() == existing_bytes
        finally:
            second_renderer.close()
    finally:
        writer.close()


# --------------------------------------------------------------------------------------- AC5


@_requires_pg
def test_ac5_refusal_renders_one_blocked_line_once(clean_table: None, tmp_path: Path) -> None:
    """record_refusal -> render() -> exactly one '[BLOCKED ...]' line; a subsequent render()
    adds nothing (BLOCKED is absorbing, the TK-146 carry-forward rendered ONCE)."""
    assert _DSN is not None
    log_path = tmp_path / "wombat-trail.log"
    writer = ActionTrailWriter(_DSN)
    proposed_at = datetime(2026, 7, 2, 15, 0, tzinfo=UTC)
    try:
        writer.record_refusal(
            action_id="refused-1",
            human_summary="Blocked: tainted content in a would-be form submission",
            target="https://example.com/form",
            proposed_at=proposed_at,
        )

        renderer = ActionTrailRenderer(_DSN, log_path)
        try:
            renderer.render()

            lines = _lines(log_path)
            assert lines == [
                f"[BLOCKED {proposed_at.isoformat()}] blocked_by_taint: "
                "Blocked: tainted content in a would-be form submission"
            ]

            first_bytes = log_path.read_bytes()
            renderer.render()
            assert log_path.read_bytes() == first_bytes
        finally:
            renderer.close()
    finally:
        writer.close()
