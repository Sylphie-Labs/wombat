"""TK-295 — wombat_chat_turns migration + ChatTurnStore acceptance criteria (DEC-65e).

DB tests require a REAL Postgres and are gated on ``WOMBAT_TEST_PG_DSN`` (the same convention as
``tests/unit/test_scratchpad.py`` / ``tests/unit/test_user_facts.py``): absent it, tests are
skipped LOUDLY. NEVER point this at a live database.

  AC1 ``ensure_all_schemas`` runs twice: ``wombat_chat_turns`` exists idempotently as the
      ELEVENTH preflight entry; ``record_turn``/``turns_since``/``purge_older_than`` round-trip
      with ascending order and cutoff semantics.
  AC4 (structural) import + construction do zero IO; the module imports nothing from
      ``wombat.bootstrap`` or ``wombat.runtime``.
"""

from __future__ import annotations

import ast
import inspect
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest

from wombat import chat_turns, schema_preflight
from wombat.chat_turns import ChatTurnStore, ensure_schema

_DSN = os.environ.get("WOMBAT_TEST_PG_DSN")

_requires_pg = pytest.mark.skipif(
    not _DSN,
    reason=(
        "WOMBAT_TEST_PG_DSN is not set — skipping chat_turns DB tests that require a real "
        "throwaway Postgres. Start one with:\n"
        "  docker run --rm -d -p 5433:5432 -e POSTGRES_PASSWORD=wombat postgres:16\n"
        "then export WOMBAT_TEST_PG_DSN=postgresql://postgres:wombat@localhost:5433/postgres"
    ),
)


@pytest.fixture
def fresh_table() -> None:
    """Drop ``wombat_chat_turns``, simulating a brand-new empty Postgres."""
    assert _DSN is not None
    with psycopg.connect(_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS wombat_chat_turns CASCADE")
        conn.commit()


def _columns(dsn: str) -> dict[str, str]:
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name = 'wombat_chat_turns'"
        )
        return {row[0]: row[1] for row in cur.fetchall()}


# --------------------------------------------------------------------------------------- AC1


@_requires_pg
def test_ac1_ensure_schema_creates_pinned_shape_and_is_idempotent(fresh_table: None) -> None:
    assert _DSN is not None
    with psycopg.connect(_DSN) as conn:
        ensure_schema(conn)
        ensure_schema(conn)  # must not raise, must not change anything

    cols = _columns(_DSN)
    assert cols["id"] == "bigint"
    assert cols["text"] == "text"
    assert cols["voice"] == "boolean"
    assert cols["captured_at"] == "timestamp with time zone"


def test_ac1_ensure_all_schemas_carries_exactly_twelve_entries() -> None:
    source = inspect.getsource(schema_preflight.ensure_all_schemas)
    calls = [
        line.strip()
        for line in source.splitlines()
        if line.strip().startswith("ensure_") and line.strip().endswith("_schema(conn)")
    ]
    assert len(calls) == 12
    assert "ensure_chat_turns_schema(conn)" in source


@_requires_pg
def test_ac1_record_turns_since_purge_round_trip(fresh_table: None) -> None:
    assert _DSN is not None
    with psycopg.connect(_DSN) as conn:
        ensure_schema(conn)
        conn.commit()

    store = ChatTurnStore(_DSN)
    try:
        now = datetime.now(UTC)
        # All three rows are well inside the 7-day retention window (relative to "now", never a
        # hardcoded calendar date) so the later purge_older_than(7) call touches ONLY the fourth,
        # deliberately-old row added below.
        t0 = now - timedelta(days=3)
        t1 = now - timedelta(days=2)
        t2 = now - timedelta(hours=1)
        # Insert out of order to prove turns_since sorts, not just preserves insert order.
        store.record_turn("third utterance", True, t2)
        store.record_turn("first utterance", False, t0)
        store.record_turn("second utterance", True, t1)

        # Cutoff semantics: at-or-after t0 returns all three, ascending by captured_at.
        rows = store.turns_since(t0)
        assert [row["text"] for row in rows] == [
            "first utterance",
            "second utterance",
            "third utterance",
        ]
        assert [row["voice"] for row in rows] == [False, True, True]

        # Cutoff strictly after t0 excludes the first row.
        rows_after_t0 = store.turns_since(t0 + timedelta(seconds=1))
        assert [row["text"] for row in rows_after_t0] == ["second utterance", "third utterance"]

        # purge_older_than: only rows older than the window are removed.
        old_captured = now - timedelta(days=10)
        store.record_turn("very old utterance", False, old_captured)
        deleted = store.purge_older_than(7)
        assert deleted == 1
        remaining = {row["text"] for row in store.turns_since(t0)}
        assert "very old utterance" not in remaining
        assert "first utterance" in remaining
    finally:
        store.close()


# --------------------------------------------------------------------------------------- AC4


def test_ac4_construction_does_zero_io() -> None:
    """Constructing a ChatTurnStore over a bogus DSN must not connect (Q-46: lazy connection)."""
    store = ChatTurnStore("postgresql://nonexistent-host-should-never-be-dialed:1/db")
    assert store._conn is None  # no I/O happened at construction


def test_ac4_chat_turns_imports_nothing_from_bootstrap_or_runtime() -> None:
    source = Path(chat_turns.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)
            imported_modules.update(f"{node.module}.{alias.name}" for alias in node.names)
    assert not any("bootstrap" in mod for mod in imported_modules)
    assert not any(mod == "runtime" or mod.endswith(".runtime") for mod in imported_modules)
