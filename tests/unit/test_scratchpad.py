"""TK-247 — scratchpad acceptance criteria (DEC-46).

DB tests (AC1/AC2) require a REAL Postgres and are gated on ``WOMBAT_TEST_PG_DSN`` (the same
convention as ``tests/unit/test_settings_store.py`` / ``tests/unit/test_external_store.py`` /
``tests/unit/test_schema_preflight.py``): absent it, tests are skipped LOUDLY.

  AC1 pinned shape + idempotent ``ensure_schema``; the preflight carries exactly TEN entries;
      ``put``/``get_scope``/``delete_scope`` over two scopes — a re-put of the same (scope_key,
      entry_key) upserts with ``updated_at`` bumped while ``created_at`` stays byte-unchanged,
      ``get_scope`` returns only its own scope, ``delete_scope`` removes exactly its own scope.
  AC2 rows straddling the purge horizon: ``purge_stale`` deletes only older-than-horizon rows.
  AC3 structural: no ``bootstrap``/``runtime`` import.
"""

from __future__ import annotations

import ast
import inspect
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest

from wombat import schema_preflight, scratchpad
from wombat.scratchpad import ScratchpadStore, ensure_schema

_DSN = os.environ.get("WOMBAT_TEST_PG_DSN")

_requires_pg = pytest.mark.skipif(
    not _DSN,
    reason=(
        "WOMBAT_TEST_PG_DSN is not set — skipping scratchpad DB tests that require a real "
        "throwaway Postgres. Start one with:\n"
        "  docker run --rm -d -p 5433:5432 -e POSTGRES_PASSWORD=wombat postgres:16\n"
        "then export WOMBAT_TEST_PG_DSN=postgresql://postgres:wombat@localhost:5433/postgres"
    ),
)


@pytest.fixture
def fresh_table() -> None:
    """Drop ``wombat_scratchpad``, simulating a brand-new empty Postgres."""
    assert _DSN is not None
    with psycopg.connect(_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS wombat_scratchpad CASCADE")
        conn.commit()


def _columns(dsn: str) -> dict[str, str]:
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name = 'wombat_scratchpad'"
        )
        return {row[0]: row[1] for row in cur.fetchall()}


def _created_at(dsn: str, scope_key: str, entry_key: str) -> datetime:
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT created_at FROM wombat_scratchpad WHERE scope_key = %s AND entry_key = %s",
            (scope_key, entry_key),
        )
        row = cur.fetchone()
        assert row is not None
        value: datetime = row[0]
        return value


# --------------------------------------------------------------------------------------- AC1


@_requires_pg
def test_ac1_ensure_schema_creates_pinned_shape_and_is_idempotent(fresh_table: None) -> None:
    assert _DSN is not None
    with psycopg.connect(_DSN) as conn:
        ensure_schema(conn)
        ensure_schema(conn)  # must not raise, must not change anything

    cols = _columns(_DSN)
    assert cols["scope_key"] == "text"
    assert cols["entry_key"] == "text"
    assert cols["value"] == "jsonb"
    assert cols["created_at"] == "timestamp with time zone"
    assert cols["updated_at"] == "timestamp with time zone"


def test_ac1_ensure_all_schemas_carries_exactly_ten_entries() -> None:
    source = inspect.getsource(schema_preflight.ensure_all_schemas)
    calls = [
        line.strip()
        for line in source.splitlines()
        if line.strip().startswith("ensure_") and line.strip().endswith("_schema(conn)")
    ]
    assert len(calls) == 10
    assert "ensure_scratchpad_schema(conn)" in source


@_requires_pg
def test_ac1_put_get_scope_delete_scope_over_two_scopes(fresh_table: None) -> None:
    assert _DSN is not None
    with psycopg.connect(_DSN) as conn:
        ensure_schema(conn)
        conn.commit()

    store = ScratchpadStore(_DSN)
    try:
        store.put("scope-a", "k1", {"v": 1})
        created_at = _created_at(_DSN, "scope-a", "k1")

        # Re-put the same (scope_key, entry_key): upserts value, bumps updated_at, leaves
        # created_at byte-unchanged.
        store.put("scope-a", "k1", {"v": 2})
        assert _created_at(_DSN, "scope-a", "k1") == created_at

        store.put("scope-a", "k2", {"v": 3})
        store.put("scope-b", "k1", {"v": 99})

        scope_a = store.get_scope("scope-a")
        assert scope_a == {"k1": {"v": 2}, "k2": {"v": 3}}

        scope_b = store.get_scope("scope-b")
        assert scope_b == {"k1": {"v": 99}}

        store.delete_scope("scope-a")
        assert store.get_scope("scope-a") == {}
        # scope-b is untouched by scope-a's deletion.
        assert store.get_scope("scope-b") == {"k1": {"v": 99}}
    finally:
        store.close()


# --------------------------------------------------------------------------------------- AC2


@_requires_pg
def test_ac2_purge_stale_deletes_only_rows_older_than_the_horizon(fresh_table: None) -> None:
    assert _DSN is not None
    with psycopg.connect(_DSN) as conn:
        ensure_schema(conn)
        conn.commit()

    store = ScratchpadStore(_DSN)
    try:
        store.put("scope-a", "fresh", {"v": 1})
        store.put("scope-a", "stale", {"v": 2})
        store.put("scope-b", "fresh", {"v": 3})

        # Age "stale" past the horizon via a direct UPDATE (put() always sets updated_at=now()).
        old_updated_at = datetime.now(UTC) - timedelta(
            days=scratchpad.SCRATCHPAD_PURGE_DAYS + 1
        )
        with psycopg.connect(_DSN) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE wombat_scratchpad SET updated_at = %s "
                "WHERE scope_key = %s AND entry_key = %s",
                (old_updated_at, "scope-a", "stale"),
            )
            conn.commit()

        deleted = store.purge_stale(scratchpad.SCRATCHPAD_PURGE_DAYS)
        assert deleted == 1

        assert store.get_scope("scope-a") == {"fresh": {"v": 1}}
        assert store.get_scope("scope-b") == {"fresh": {"v": 3}}
    finally:
        store.close()


# --------------------------------------------------------------------------------------- AC3


def test_ac3_scratchpad_imports_nothing_from_bootstrap_or_runtime() -> None:
    source = Path(scratchpad.__file__).read_text(encoding="utf-8")
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
