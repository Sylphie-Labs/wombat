"""TK-111 — BehaviorEventLog schema + writer acceptance criteria (EP-21, Q-98).

ALL DB tests in this module require a REAL Postgres and are gated on the ``WOMBAT_TEST_PG_DSN``
env var: absent it, tests are skipped LOUDLY (never faked, never CI-failed on a fresh clone), and
no pg connection is attempted anywhere (the store is lazy: no connect at construction). Spin up a
throwaway Postgres locally:

    docker run --rm -d -p 5433:5432 -e POSTGRES_PASSWORD=wombat postgres:16
    WOMBAT_TEST_PG_DSN=postgresql://postgres:wombat@localhost:5433/postgres

Each DB test calls ``ensure_schema`` and truncates the table first (``clean_table`` fixture) so a
shared local Postgres is safe to reuse.

  AC1 upsert writes {idempotency_key, event_type, source_id, timestamp_utc, outcome_label,
      duration_seconds} — read back directly from the table; re-running upsert with the SAME
      idempotency_key leaves the row count unchanged (idempotent) but updates the other columns.
  AC2 (structural, no pg needed) the migration has no motive/why column, and every column name is
      drawn from the closed TK-43 OUTCOME_* vocabulary + the row-mapping fields Q-98 ruled — never
      a free-form motive field. Also (AC4, NG-3): the only src/wombat importers of
      wombat.behavior.event_log are pathways/dream_pathway.py, bootstrap.py, and (TK-112)
      behavior/window_detector.py + behavior/stages/write_window_summaries.py.
  AC3 events_between returns rows ordered ASCENDING by timestamp_utc, human-readable (a
      dataclass, not raw tuples), over a >=7-day spread.
"""

from __future__ import annotations

import ast
import inspect
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest

from wombat.behavior.event_log import BehaviorEventLog, BehaviorEventRow, ensure_schema

_DSN = os.environ.get("WOMBAT_TEST_PG_DSN")

_requires_pg = pytest.mark.skipif(
    not _DSN,
    reason=(
        "WOMBAT_TEST_PG_DSN is not set — skipping BehaviorEventLog DB tests that require a "
        "real throwaway Postgres. Start one with:\n"
        "  docker run --rm -d -p 5433:5432 -e POSTGRES_PASSWORD=wombat postgres:16\n"
        "then export WOMBAT_TEST_PG_DSN=postgresql://postgres:wombat@localhost:5433/postgres"
    ),
)


@pytest.fixture
def clean_table() -> None:
    """Ensure the schema exists and the table is empty before each DB test."""
    assert _DSN is not None
    with psycopg.connect(_DSN) as conn:
        ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE wombat_behavior_events")
        conn.commit()


# --------------------------------------------------------------------------------------- AC1


@_requires_pg
def test_ac1_upsert_writes_the_mapped_columns(clean_table: None) -> None:
    assert _DSN is not None
    store = BehaviorEventLog(_DSN)
    resolved_at = datetime(2026, 7, 1, 9, 0, 0, tzinfo=UTC)
    try:
        store.upsert(
            idempotency_key="7:calendar:evt_abc",
            event_type="calendar_conflict",
            source_id="calendar",
            timestamp_utc=resolved_at,
            outcome_label="outcome_load_bearing",
        )

        with psycopg.connect(_DSN) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT idempotency_key, event_type, source_id, timestamp_utc, outcome_label, "
                "duration_seconds FROM wombat_behavior_events"
            )
            rows = cur.fetchall()
    finally:
        store.close()

    assert len(rows) == 1
    row = rows[0]
    assert row[0] == "7:calendar:evt_abc"
    assert row[1] == "calendar_conflict"
    assert row[2] == "calendar"
    assert row[3] == resolved_at
    assert row[4] == "outcome_load_bearing"
    assert row[5] is None  # v1: no duration signal exists yet


@_requires_pg
def test_ac1_upsert_is_idempotent_on_the_same_idempotency_key(clean_table: None) -> None:
    """Re-running upsert with the SAME idempotency_key never inserts a second row (AC1) — it
    updates the existing row's columns in place."""
    assert _DSN is not None
    store = BehaviorEventLog(_DSN)
    try:
        store.upsert(
            idempotency_key="7:calendar:evt_abc",
            event_type="calendar_conflict",
            source_id="calendar",
            timestamp_utc=datetime(2026, 7, 1, 9, 0, 0, tzinfo=UTC),
            outcome_label="outcome_load_bearing",
        )
        # A second night's re-run over the SAME terminal claim — a different label this time,
        # proving it's a genuine UPDATE, not a no-op.
        store.upsert(
            idempotency_key="7:calendar:evt_abc",
            event_type="calendar_conflict",
            source_id="calendar",
            timestamp_utc=datetime(2026, 7, 2, 9, 0, 0, tzinfo=UTC),
            outcome_label="outcome_ignored",
        )

        with psycopg.connect(_DSN) as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM wombat_behavior_events")
            count_row = cur.fetchone()
            assert count_row is not None
            assert count_row[0] == 1  # row count unchanged — no duplicate insert

            cur.execute(
                "SELECT outcome_label, timestamp_utc FROM wombat_behavior_events "
                "WHERE idempotency_key = %s",
                ("7:calendar:evt_abc",),
            )
            updated_row = cur.fetchone()
            assert updated_row is not None
            assert updated_row[0] == "outcome_ignored"
            assert updated_row[1] == datetime(2026, 7, 2, 9, 0, 0, tzinfo=UTC)
    finally:
        store.close()


def test_construction_never_connects_without_a_dsn_being_used() -> None:
    """Constructing BehaviorEventLog does NOT attempt a pg connection (lazy connection only,
    Q-46 conventions)."""
    store = BehaviorEventLog("postgresql://nonexistent-host-should-never-be-dialed:1/db")
    assert store is not None
    # No upsert()/events_between() call — construction alone must not have touched the network.


# --------------------------------------------------------------------------------------- AC2


def test_ac2_migration_has_no_motive_or_why_column() -> None:
    """Structural: the packaged migration DDL never declares a motive/why column (CON-6/NG-1).

    Strips ``--`` comment lines first — the migration's OWN prose deliberately documents the
    absence of a motive column (e.g. "there is no motive/why column"), which would otherwise
    false-positive a naive whole-file substring search."""
    sql_path = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "wombat"
        / "migrations"
        / "006_behavior_events.sql"
    )
    ddl_only = "\n".join(
        line
        for line in sql_path.read_text(encoding="utf-8").splitlines()
        if not line.strip().startswith("--")
    ).lower()
    assert "motive" not in ddl_only
    assert "why" not in ddl_only


def _targets_event_log_module(dotted_module: str) -> bool:
    """True iff ``dotted_module`` (an ``ast.ImportFrom.module``/``ast.alias.name`` string) names
    ``wombat.behavior.event_log`` — handles both absolute (``wombat.behavior.event_log``) and
    relative (``from .behavior.event_log import ...`` -> module ``"behavior.event_log"``, dots
    stripped by the parser) import spellings by matching the last two dotted components exactly
    (never a bare substring match, which could false-positive on an unrelated module name)."""
    return dotted_module.split(".")[-2:] == ["behavior", "event_log"]


def test_ac2_only_pathways_and_bootstrap_import_behavior_event_log() -> None:
    """AC4/NG-3: the only src/wombat importers of wombat.behavior.event_log are
    pathways/dream_pathway.py, bootstrap.py, and (TK-112) behavior/window_detector.py +
    behavior/stages/write_window_summaries.py — no dashboard/analytics consumer anywhere."""
    src_root = Path(__file__).resolve().parents[2] / "src" / "wombat"
    event_log_module = src_root / "behavior" / "event_log.py"

    importers: set[Path] = set()
    for path in src_root.rglob("*.py"):
        if path == event_log_module:
            continue  # the module itself never "imports" itself
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module is not None:
                if _targets_event_log_module(node.module):
                    importers.add(path)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if _targets_event_log_module(alias.name):
                        importers.add(path)

    assert importers == {
        src_root / "pathways" / "dream_pathway.py",
        src_root / "bootstrap.py",
        src_root / "behavior" / "window_detector.py",
        src_root / "behavior" / "stages" / "write_window_summaries.py",
    }


# --------------------------------------------------------------------------------------- AC3


@_requires_pg
def test_ac3_events_between_returns_rows_ordered_ascending_and_human_readable(
    clean_table: None,
) -> None:
    assert _DSN is not None
    store = BehaviorEventLog(_DSN)
    base = datetime(2026, 7, 1, 9, 0, 0, tzinfo=UTC)
    try:
        # Seeded out of order, spread over >= 7 days.
        store.upsert(
            idempotency_key="6:gmail:msg_c",
            event_type="draft_reply",
            source_id="gmail",
            timestamp_utc=base + timedelta(days=6),
            outcome_label="outcome_ignored",
        )
        store.upsert(
            idempotency_key="8:calendar:evt_a",
            event_type="calendar_conflict",
            source_id="calendar",
            timestamp_utc=base,
            outcome_label="outcome_load_bearing",
        )
        store.upsert(
            idempotency_key="6:gmail:msg_b",
            event_type="draft_reply",
            source_id="gmail",
            timestamp_utc=base + timedelta(days=3),
            outcome_label="outcome_regretted",
        )

        rows = store.events_between(base, base + timedelta(days=7))
    finally:
        store.close()

    assert len(rows) == 3
    assert all(isinstance(row, BehaviorEventRow) for row in rows)  # human-readable typed rows
    timestamps = [row.timestamp_utc for row in rows]
    assert timestamps == sorted(timestamps)  # ascending order
    assert [row.idempotency_key for row in rows] == [
        "8:calendar:evt_a",
        "6:gmail:msg_b",
        "6:gmail:msg_c",
    ]


@_requires_pg
def test_ac3_events_between_excludes_rows_outside_the_range(clean_table: None) -> None:
    assert _DSN is not None
    store = BehaviorEventLog(_DSN)
    base = datetime(2026, 7, 1, 9, 0, 0, tzinfo=UTC)
    try:
        store.upsert(
            idempotency_key="6:gmail:msg_in",
            event_type="draft_reply",
            source_id="gmail",
            timestamp_utc=base,
            outcome_label="outcome_ignored",
        )
        store.upsert(
            idempotency_key="6:gmail:msg_out",
            event_type="draft_reply",
            source_id="gmail",
            timestamp_utc=base + timedelta(days=30),
            outcome_label="outcome_ignored",
        )

        rows = store.events_between(base, base + timedelta(days=1))
    finally:
        store.close()

    assert [row.idempotency_key for row in rows] == ["6:gmail:msg_in"]


def test_ensure_schema_signature_takes_a_connection() -> None:
    """``ensure_schema`` is a module-level function over an injected connection (Q-46
    convention), mirroring every other Q-46 adapter (``PgPendingJournal``, ``DailyLedger``,
    ``ActionTrailWriter``)."""
    params = list(inspect.signature(ensure_schema).parameters.values())
    assert params[0].name == "conn"
