"""TK-294 — wombat_user_facts migration + UserFactsStore acceptance criteria (DEC-65d).

DB tests require a REAL Postgres and are gated on ``WOMBAT_TEST_PG_DSN`` (the same convention as
``tests/unit/test_scratchpad.py`` / ``tests/sources/test_seen_ledger.py``): absent it, tests are
skipped LOUDLY. NEVER point this at a live database.

  AC1 ``ensure_all_schemas`` runs twice: ``wombat_user_facts`` exists idempotently as the TENTH
      preflight entry.
  AC2 ``upsert_fact``/``list_facts``/``delete_fact``/``count`` round-trip; a re-upsert of the same
      ``fact_key`` updates ``fact``/``updated_at`` while ``first_seen_at`` is unchanged; with zero
      told-tier facts present, ``list`` ordering is ``updated_at`` DESC (byte-identical to
      pre-TK-316 behavior).
  AC3 at 200 rows (``_MAX_FACTS``), one more NEW ``fact_key`` evicts the oldest-updated row (one
      WARNING via caplog), ``count()`` stays at 200, and the new fact is present.
  AC4 import + construction do zero IO; the module imports nothing from ``wombat.bootstrap`` or
      ``wombat.runtime``.
  AC5 (TK-316, DEC-66 crowd-out guard) with told/dream/derived facts interleaved by recency,
      ``list_facts`` returns every told-tier fact first (recency-ordered within the tier), then
      every remaining fact by recency.
"""

from __future__ import annotations

import ast
import inspect
import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest

from wombat import schema_preflight, user_facts
from wombat.user_facts import _MAX_FACTS, UserFactsStore, ensure_schema

_DSN = os.environ.get("WOMBAT_TEST_PG_DSN")

_requires_pg = pytest.mark.skipif(
    not _DSN,
    reason=(
        "WOMBAT_TEST_PG_DSN is not set — skipping user_facts DB tests that require a real "
        "throwaway Postgres. Start one with:\n"
        "  docker run --rm -d -p 5433:5432 -e POSTGRES_PASSWORD=wombat postgres:16\n"
        "then export WOMBAT_TEST_PG_DSN=postgresql://postgres:wombat@localhost:5433/postgres"
    ),
)


@pytest.fixture
def fresh_table() -> None:
    """Drop ``wombat_user_facts``, simulating a brand-new empty Postgres."""
    assert _DSN is not None
    with psycopg.connect(_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS wombat_user_facts CASCADE")
        conn.commit()


def _columns(dsn: str) -> dict[str, str]:
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name = 'wombat_user_facts'"
        )
        return {row[0]: row[1] for row in cur.fetchall()}


def _first_seen_at(dsn: str, fact_key: str) -> datetime:
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT first_seen_at FROM wombat_user_facts WHERE fact_key = %s", (fact_key,)
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
    assert cols["fact_key"] == "text"
    assert cols["fact"] == "text"
    assert cols["source"] == "text"
    assert cols["first_seen_at"] == "timestamp with time zone"
    assert cols["updated_at"] == "timestamp with time zone"


def test_ac1_ensure_all_schemas_carries_exactly_twelve_entries() -> None:
    source = inspect.getsource(schema_preflight.ensure_all_schemas)
    calls = [
        line.strip()
        for line in source.splitlines()
        if line.strip().startswith("ensure_") and line.strip().endswith("_schema(conn)")
    ]
    assert len(calls) == 12  # TK-310 added the twelfth entry (wombat_observations)
    assert "ensure_user_facts_schema(conn)" in source


# --------------------------------------------------------------------------------------- AC2


@_requires_pg
def test_ac2_upsert_list_delete_count_round_trip(fresh_table: None) -> None:
    assert _DSN is not None
    with psycopg.connect(_DSN) as conn:
        ensure_schema(conn)
        conn.commit()

    store = UserFactsStore(_DSN)
    try:
        store.upsert_fact("fact-1", "likes tea", "derived")
        first_seen = _first_seen_at(_DSN, "fact-1")
        assert store.count() == 1

        # Re-upsert the same fact_key: updates fact + updated_at, leaves first_seen_at unchanged.
        store.upsert_fact("fact-1", "likes green tea", "derived")
        assert store.count() == 1
        assert _first_seen_at(_DSN, "fact-1") == first_seen

        store.upsert_fact("fact-2", "works remotely", "behavior")

        facts = store.list_facts(10)
        assert [row["fact_key"] for row in facts] == ["fact-2", "fact-1"]  # updated_at DESC
        assert facts[1]["fact"] == "likes green tea"
        assert facts[1]["source"] == "derived"

        store.delete_fact("fact-1")
        assert store.count() == 1
        assert [row["fact_key"] for row in store.list_facts(10)] == ["fact-2"]
    finally:
        store.close()


# --------------------------------------------------------------------------------------- AC3


@_requires_pg
def test_ac3_upsert_at_cap_evicts_oldest_updated_row_with_one_warning(
    fresh_table: None, caplog: pytest.LogCaptureFixture
) -> None:
    assert _DSN is not None
    with psycopg.connect(_DSN) as conn:
        ensure_schema(conn)
        conn.commit()

    store = UserFactsStore(_DSN)
    try:
        # Fill the store to _MAX_FACTS rows with strictly increasing updated_at, oldest first.
        base = datetime.now(UTC) - timedelta(days=_MAX_FACTS + 1)
        for i in range(_MAX_FACTS):
            store.upsert_fact(f"fact-{i}", f"fact number {i}", "derived")
        assert store.count() == _MAX_FACTS

        # Force a deterministic ordering: fact-0 is the oldest-updated row.
        with psycopg.connect(_DSN) as conn, conn.cursor() as cur:
            for i in range(_MAX_FACTS):
                cur.execute(
                    "UPDATE wombat_user_facts SET updated_at = %s WHERE fact_key = %s",
                    (base + timedelta(minutes=i), f"fact-{i}"),
                )
            conn.commit()

        caplog.set_level(logging.WARNING, logger="wombat.user_facts")
        store.upsert_fact("fact-new", "a brand new fact", "told")

        assert store.count() == _MAX_FACTS
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "evicting" in warnings[0].message

        remaining_keys = {row["fact_key"] for row in store.list_facts(_MAX_FACTS)}
        assert "fact-0" not in remaining_keys  # the oldest-updated row was evicted
        assert "fact-new" in remaining_keys
    finally:
        store.close()


# --------------------------------------------------------------------------------------- AC4


def test_ac4_construction_does_zero_io() -> None:
    """Constructing a UserFactsStore over a bogus DSN must not connect (Q-46: lazy connection)."""
    store = UserFactsStore("postgresql://nonexistent-host-should-never-be-dialed:1/db")
    assert store._conn is None  # no I/O happened at construction


def test_ac4_user_facts_imports_nothing_from_bootstrap_or_runtime() -> None:
    source = Path(user_facts.__file__).read_text(encoding="utf-8")
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


# --------------------------------------------------------------------------------------- AC5


@_requires_pg
def test_ac5_list_facts_orders_told_tier_first_then_recency(fresh_table: None) -> None:
    """TK-316, DEC-66 crowd-out guard: told-tier facts always sort ahead of every other tier;
    within a tier, ordering is ``updated_at`` DESC."""
    assert _DSN is not None
    with psycopg.connect(_DSN) as conn:
        ensure_schema(conn)
        conn.commit()

    store = UserFactsStore(_DSN)
    try:
        # 20 facts across told/dream/derived, interleaved by insertion order (round-robin).
        sources = ["told", "dream", "derived"]
        for i in range(20):
            source = sources[i % len(sources)]
            store.upsert_fact(f"fact-{i}", f"fact number {i}", source)
        assert store.count() == 20

        # Force a deterministic, strictly increasing updated_at across ALL 20 rows (fact-0
        # oldest, fact-19 newest) so tier vs. recency ordering are cleanly distinguishable.
        base = datetime.now(UTC) - timedelta(days=1)
        with psycopg.connect(_DSN) as conn, conn.cursor() as cur:
            for i in range(20):
                cur.execute(
                    "UPDATE wombat_user_facts SET updated_at = %s WHERE fact_key = %s",
                    (base + timedelta(minutes=i), f"fact-{i}"),
                )
            conn.commit()

        told_keys_by_recency = [
            f"fact-{i}" for i in reversed(range(20)) if sources[i % len(sources)] == "told"
        ]
        other_keys_by_recency = [
            f"fact-{i}" for i in reversed(range(20)) if sources[i % len(sources)] != "told"
        ]

        result_keys = [row["fact_key"] for row in store.list_facts(15)]

        assert result_keys[: len(told_keys_by_recency)] == told_keys_by_recency
        assert result_keys[len(told_keys_by_recency) :] == other_keys_by_recency[
            : 15 - len(told_keys_by_recency)
        ]
    finally:
        store.close()


@_requires_pg
def test_ac5_zero_told_facts_ordering_unchanged(fresh_table: None) -> None:
    """With no told-tier facts present, ordering stays plain ``updated_at`` DESC (byte-identical
    to pre-TK-316 behavior)."""
    assert _DSN is not None
    with psycopg.connect(_DSN) as conn:
        ensure_schema(conn)
        conn.commit()

    store = UserFactsStore(_DSN)
    try:
        store.upsert_fact("fact-a", "fact a", "dream")
        store.upsert_fact("fact-b", "fact b", "derived")
        store.upsert_fact("fact-c", "fact c", "behavior")

        assert [row["fact_key"] for row in store.list_facts(10)] == [
            "fact-c",
            "fact-b",
            "fact-a",
        ]
    finally:
        store.close()
