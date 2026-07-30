"""TK-244 — external_store acceptance criteria (DEC-45).

DB tests (AC1/AC2) require a REAL Postgres and are gated on ``WOMBAT_TEST_PG_DSN`` (the same
convention as ``tests/unit/test_settings_store.py`` / ``tests/unit/test_schema_preflight.py``):
absent it, tests are skipped LOUDLY.

  AC1 pinned shape + idempotent ``ensure_schema``; ``upsert_many`` of the same (source, item_key)
      twice with a changed payload and a later ``fetched_at`` leaves exactly ONE row, with
      ``payload``/``fetched_at`` updated and ``first_seen_at`` byte-unchanged.
  AC2 rows across two sources straddling a window boundary and a prune horizon:
      ``get_window``/``get_recent`` return ONLY the asked source's in-window rows, ordered by
      ``occurs_at``; ``prune_older_than`` deletes ONLY ``fetched_at``-older rows and reports the
      count.
  AC3 structural: no ``bootstrap``/``runtime`` import; ``ensure_all_schemas`` carries exactly
      TEN ``ensure_schema`` calls (TK-247 added the eighth, wombat_scratchpad; TK-286 added the
      ninth, wombat_seen_events; TK-294 added the tenth, wombat_user_facts).
"""

from __future__ import annotations

import ast
import inspect
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest

from wombat import external_store, schema_preflight
from wombat.external_store import ExternalItem, ExternalItemStore, ensure_schema

_DSN = os.environ.get("WOMBAT_TEST_PG_DSN")

_requires_pg = pytest.mark.skipif(
    not _DSN,
    reason=(
        "WOMBAT_TEST_PG_DSN is not set — skipping external_store DB tests that require a real "
        "throwaway Postgres. Start one with:\n"
        "  docker run --rm -d -p 5433:5432 -e POSTGRES_PASSWORD=wombat postgres:16\n"
        "then export WOMBAT_TEST_PG_DSN=postgresql://postgres:wombat@localhost:5433/postgres"
    ),
)


@pytest.fixture
def fresh_table() -> None:
    """Drop ``wombat_external_items``, simulating a brand-new empty Postgres."""
    assert _DSN is not None
    with psycopg.connect(_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS wombat_external_items CASCADE")
        conn.commit()


def _columns(dsn: str) -> dict[str, str]:
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name = 'wombat_external_items'"
        )
        return {row[0]: row[1] for row in cur.fetchall()}


def _first_seen_at(dsn: str, source: str, item_key: str) -> datetime:
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT first_seen_at FROM wombat_external_items WHERE source = %s AND item_key = %s",
            (source, item_key),
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
    assert cols["source"] == "text"
    assert cols["item_key"] == "text"
    assert cols["payload"] == "jsonb"
    assert cols["occurs_at"] == "timestamp with time zone"
    assert cols["fetched_at"] == "timestamp with time zone"
    assert cols["first_seen_at"] == "timestamp with time zone"


@_requires_pg
def test_ac1_upsert_many_reupsert_updates_payload_fetched_at_keeps_first_seen_at(
    fresh_table: None,
) -> None:
    assert _DSN is not None
    with psycopg.connect(_DSN) as conn:
        ensure_schema(conn)
        conn.commit()

    store = ExternalItemStore(_DSN)
    try:
        occurs_at = datetime(2026, 1, 1, tzinfo=UTC)
        first_fetch = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        store.upsert_many(
            "gcal",
            [ExternalItem(item_key="evt-1", payload={"title": "Original"}, occurs_at=occurs_at)],
            fetched_at=first_fetch,
        )
        first_seen = _first_seen_at(_DSN, "gcal", "evt-1")

        second_fetch = datetime(2026, 1, 1, 13, 0, tzinfo=UTC)
        store.upsert_many(
            "gcal",
            [ExternalItem(item_key="evt-1", payload={"title": "Updated"}, occurs_at=occurs_at)],
            fetched_at=second_fetch,
        )

        rows = store.get_recent("gcal", limit=10)
        assert len(rows) == 1
        assert rows[0]["payload"] == {"title": "Updated"}
        assert rows[0]["fetched_at"] == second_fetch
        assert rows[0]["first_seen_at"] == first_seen
    finally:
        store.close()


# --------------------------------------------------------------------------------------- AC2


@_requires_pg
def test_ac2_window_recent_prune_are_per_source_and_correctly_scoped(
    fresh_table: None,
) -> None:
    assert _DSN is not None
    with psycopg.connect(_DSN) as conn:
        ensure_schema(conn)
        conn.commit()

    store = ExternalItemStore(_DSN)
    try:
        # fetched_at values are anchored to the real wall clock (not a fixed calendar date) so
        # prune_older_than's now()-based horizon behaves predictably regardless of when this test
        # runs; occurs_at stays relative to that same anchor for the window assertions.
        now = datetime.now(UTC)
        window_start = now
        window_end = now + timedelta(days=7)

        # source "a": one item inside the window, one just outside it.
        store.upsert_many(
            "a",
            [
                ExternalItem(item_key="a-in", payload={"n": 1}, occurs_at=now + timedelta(days=1)),
                ExternalItem(
                    item_key="a-out", payload={"n": 2}, occurs_at=now + timedelta(days=30)
                ),
            ],
            fetched_at=now,
        )
        # source "b": one item inside the same window — must never leak into "a"'s results.
        store.upsert_many(
            "b",
            [ExternalItem(item_key="b-in", payload={"n": 3}, occurs_at=now + timedelta(days=2))],
            fetched_at=now,
        )

        window_rows = store.get_window("a", window_start, window_end)
        assert [row["item_key"] for row in window_rows] == ["a-in"]

        recent_rows = store.get_recent("a", limit=10)
        assert [row["item_key"] for row in recent_rows] == ["a-in", "a-out"]

        # Prune horizon: age one of source "a"'s rows past 30 days via an old fetched_at, leave
        # the rest recent — only the aged row must be deleted, and only its count reported.
        old_fetch = now - timedelta(days=45)
        with psycopg.connect(_DSN) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE wombat_external_items SET fetched_at = %s "
                "WHERE source = %s AND item_key = %s",
                (old_fetch, "a", "a-out"),
            )
            conn.commit()

        deleted = store.prune_older_than(external_store.EXTERNAL_ITEMS_PRUNE_DAYS)
        assert deleted == 1

        remaining = {row["item_key"] for row in store.get_recent("a", limit=10)}
        assert remaining == {"a-in"}
        remaining_b = {row["item_key"] for row in store.get_recent("b", limit=10)}
        assert remaining_b == {"b-in"}
    finally:
        store.close()


# --------------------------------------------------------------------------------------- AC3


def test_ac3_external_store_imports_nothing_from_bootstrap_or_runtime() -> None:
    source = Path(external_store.__file__).read_text(encoding="utf-8")
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


def test_ac3_ensure_all_schemas_carries_exactly_ten_entries() -> None:
    source = inspect.getsource(schema_preflight.ensure_all_schemas)
    calls = [
        line.strip()
        for line in source.splitlines()
        if line.strip().startswith("ensure_") and line.strip().endswith("_schema(conn)")
    ]
    assert len(calls) == 10
    assert "ensure_external_items_schema(conn)" in source
