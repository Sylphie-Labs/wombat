"""TK-29 — PgPendingJournal acceptance criteria (RISK-5, Q-70).

ALL DB tests in this module require a REAL Postgres and are gated on the ``WOMBAT_TEST_PG_DSN``
env var: absent it, tests are skipped LOUDLY (never faked, never CI-failed on a fresh clone), and
no pg connection is attempted anywhere (AC2 — the adapter is lazy: no connect at import or
construction). Spin up a throwaway Postgres locally:

    docker run --rm -d -p 5433:5432 -e POSTGRES_PASSWORD=wombat postgres:16
    WOMBAT_TEST_PG_DSN=postgresql://postgres:wombat@localhost:5433/postgres

Each DB test calls ``ensure_schema`` and truncates the table first (``clean_table`` fixture) so
a shared local Postgres is safe to reuse.

  AC1 DSN-gated round-trip: append a mix of PendingSetAdd/Remove/Clear, discard the adapter
      instance, construct a FRESH adapter on the SAME DSN, replay() returns the records in
      identical order; AND PendingSet.rebuild_from_journal over the pg replay yields the SAME
      pending-set state as rebuilding from the InMemoryPendingJournal double fed the same
      records (parity).
  AC2 DSN absent -> the test SKIPS (loud) and no pg connection is attempted anywhere (proven by
      the module-level skip decorator gating every DB test, and a no-DSN-required test asserting
      construction alone never connects).
  AC3 Protocol conformance: isinstance(PgPendingJournal(...), PendingJournal) is True
      (runtime_checkable), and the constructor takes an INJECTED dsn (no module-level DSN
      literal).
"""

from __future__ import annotations

import inspect
import os

import psycopg
import pytest

from wombat.gate.models import ItemKind
from wombat.gate.pending_journal_pg import PgPendingJournal, ensure_schema
from wombat.gate.pending_set import (
    InMemoryPendingJournal,
    PendingJournal,
    PendingSet,
    PendingSetAdd,
    PendingSetClear,
    PendingSetRemove,
)

_DSN = os.environ.get("WOMBAT_TEST_PG_DSN")

_requires_pg = pytest.mark.skipif(
    not _DSN,
    reason=(
        "WOMBAT_TEST_PG_DSN is not set — skipping PgPendingJournal DB tests that require a "
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
            cur.execute("TRUNCATE TABLE pending_journal")
        conn.commit()


def _sample_records() -> list[PendingSetAdd | PendingSetRemove | PendingSetClear]:
    """A mix exercising all three record types, including a capacity-eviction-style pair."""
    return [
        PendingSetAdd(
            item_id="a", item_kind=ItemKind.BRIEF, urgency=0.9, load=0.2, added_at=100.0
        ),
        PendingSetAdd(
            item_id="b", item_kind=ItemKind.DRAFT, urgency=0.5, load=0.3, added_at=200.0
        ),
        PendingSetRemove(item_id="a"),
        PendingSetAdd(
            item_id="c", item_kind=ItemKind.GENERIC, urgency=0.1, load=0.05, added_at=300.0
        ),
        PendingSetClear(),
        PendingSetAdd(
            item_id="d", item_kind=ItemKind.REFLECTION, urgency=0.7, load=0.4, added_at=400.0
        ),
    ]


# --------------------------------------------------------------------------------------- AC1


@_requires_pg
def test_ac1_round_trip_replay_order_survives_a_fresh_adapter(clean_table: None) -> None:
    """append() a mix, discard the adapter, replay() on a FRESH adapter over the SAME DSN
    returns the identical records in identical order."""
    assert _DSN is not None
    records = _sample_records()

    writer = PgPendingJournal(_DSN)
    try:
        for record in records:
            writer.append(record)
    finally:
        writer.close()

    fresh = PgPendingJournal(_DSN)
    try:
        replayed = fresh.replay()
    finally:
        fresh.close()

    assert list(replayed) == records


@_requires_pg
def test_ac1_rebuild_from_pg_replay_matches_in_memory_double_parity(clean_table: None) -> None:
    """PendingSet.rebuild_from_journal over the pg replay yields the SAME state as rebuilding
    from InMemoryPendingJournal fed the identical records (parity)."""
    assert _DSN is not None
    records = _sample_records()

    pg_journal = PgPendingJournal(_DSN)
    memory_journal = InMemoryPendingJournal()
    try:
        for record in records:
            pg_journal.append(record)
            memory_journal.append(record)

        pg_rebuilt = PendingSet.rebuild_from_journal(pg_journal, max_pending=10)
        memory_rebuilt = PendingSet.rebuild_from_journal(memory_journal, max_pending=10)

        assert {item.item_id for item in pg_rebuilt.list()} == {
            item.item_id for item in memory_rebuilt.list()
        }
        assert pg_rebuilt.cumulative_load() == memory_rebuilt.cumulative_load()
        assert pg_rebuilt.oldest_added_at() == memory_rebuilt.oldest_added_at()
        assert len(pg_rebuilt) == len(memory_rebuilt)
    finally:
        pg_journal.close()


@_requires_pg
def test_ac1_added_at_null_on_add_row_replays_as_zero(clean_table: None) -> None:
    """A NULL added_at persisted for an 'add' row (a legacy-shaped record) replays as 0.0."""
    assert _DSN is not None
    conn = psycopg.connect(_DSN)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO pending_journal (record_type, item_id, item_kind, urgency, load, "
                "added_at) VALUES (%s, %s, %s, %s, %s, NULL)",
                ("add", "legacy", ItemKind.GENERIC.value, 0.5, 0.1),
            )
        conn.commit()
    finally:
        conn.close()

    journal = PgPendingJournal(_DSN)
    try:
        replayed = journal.replay()
    finally:
        journal.close()

    assert len(replayed) == 1
    record = replayed[0]
    assert isinstance(record, PendingSetAdd)
    assert record.added_at == 0.0
    assert record.item_kind is ItemKind.GENERIC


@_requires_pg
def test_ac1_one_insert_per_append_no_batching(clean_table: None) -> None:
    """Each append() commits exactly one row — no batching/coalescing across calls."""
    assert _DSN is not None
    journal = PgPendingJournal(_DSN)
    try:
        journal.append(
            PendingSetAdd(item_id="x", item_kind=ItemKind.GENERIC, urgency=0.1, load=0.1)
        )
        journal.append(PendingSetRemove(item_id="x"))
        journal.append(PendingSetClear())
    finally:
        journal.close()

    with psycopg.connect(_DSN) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM pending_journal")
        row = cur.fetchone()
        assert row is not None
        assert row[0] == 3


# --------------------------------------------------------------------------------------- AC2


def test_ac2_construction_never_connects_without_a_dsn_being_used() -> None:
    """Constructing PgPendingJournal does NOT attempt a pg connection (lazy connection only).

    Uses an obviously-invalid DSN — if construction attempted to connect, this would raise.
    It must not raise, proving the connection is lazy (only opened on first append/replay).
    """
    journal = PgPendingJournal("postgresql://nonexistent-host-should-never-be-dialed:1/db")
    assert journal is not None
    # No append()/replay() call — construction alone must not have touched the network.


# --------------------------------------------------------------------------------------- AC3


def test_ac3_pg_pending_journal_is_isinstance_of_pending_journal_protocol() -> None:
    """PgPendingJournal satisfies the runtime_checkable PendingJournal Protocol."""
    journal = PgPendingJournal("postgresql://unused/db")
    assert isinstance(journal, PendingJournal)


def test_ac3_constructor_takes_an_injected_dsn_no_module_level_literal() -> None:
    """The constructor's sole required arg is an injected ``dsn`` — no module-level DSN literal."""
    import wombat.gate.pending_journal_pg as module

    for name, value in vars(module).items():
        if name.startswith("_"):
            continue
        assert not (
            isinstance(value, str) and value.startswith("postgresql://")
        ), f"found a module-level DSN literal: {name} = {value!r}"

    signature = inspect.signature(PgPendingJournal.__init__)
    params = list(signature.parameters.values())
    assert params[1].name == "dsn"
    assert params[1].default is inspect.Parameter.empty  # required, injected — not defaulted
