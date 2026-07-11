"""TK-2 — WombatQueue acceptance criteria (Q-46).

ALL tests in this module require a REAL Postgres and are gated on the ``WOMBAT_TEST_PG_DSN``
env var: absent it, the whole module is skipped LOUDLY at collection time (never faked, never
CI-failed on a fresh clone). Spin up a throwaway Postgres locally:

    docker run --rm -d -p 5433:5432 -e POSTGRES_PASSWORD=wombat postgres:16
    WOMBAT_TEST_PG_DSN=postgresql://postgres:wombat@localhost:5433/postgres

Each test calls ``ensure_schema`` and cleans up its own rows (truncate) so a shared local
Postgres is safe to reuse.

  AC1 double-enqueue same idempotency_key -> one row, second call ALREADY_QUEUED (DB count).
  AC2 enqueue at capacity -> QueueFullError, no row added.
  AC3 at-least-once across restart -> drain (lease, no ack) -> fresh WombatQueue (new epoch,
      the restart) -> drain again redelivers -> ack removes exactly once, second ack a no-op.
  AC4 empty queue -> drain() returns [] immediately.
  AC4 (TK-173, CR-16) a duplicate key enqueued at capacity is a no-op (ALREADY_QUEUED, no
      raise); a NEW key at capacity still raises QueueFullError.

TK-230 (DEC-41): ``pending_count()`` mirrors ``drain()``'s eligibility predicate exactly (a
read-only count, no lease taken) — the runtime pump's peek.
"""

from __future__ import annotations

import os

import psycopg
import pytest

from wombat.queue import EnqueueResult, QueueFullError, QueueItem, WombatQueue, ensure_schema

_DSN = os.environ.get("WOMBAT_TEST_PG_DSN")

if not _DSN:
    pytest.skip(
        "WOMBAT_TEST_PG_DSN is not set — skipping WombatQueue tests that require a real "
        "throwaway Postgres. Start one with:\n"
        "  docker run --rm -d -p 5433:5432 -e POSTGRES_PASSWORD=wombat postgres:16\n"
        "then export WOMBAT_TEST_PG_DSN=postgresql://postgres:wombat@localhost:5433/postgres",
        allow_module_level=True,
    )


@pytest.fixture
def clean_table() -> None:
    """Ensure the schema exists and the table is empty before each test."""
    assert _DSN is not None
    with psycopg.connect(_DSN) as conn:
        ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE wombat_queue")
        conn.commit()


def _count(idempotency_key: str | None = None) -> int:
    assert _DSN is not None
    with psycopg.connect(_DSN) as conn, conn.cursor() as cur:
        if idempotency_key is None:
            cur.execute("SELECT count(*) FROM wombat_queue")
        else:
            cur.execute(
                "SELECT count(*) FROM wombat_queue WHERE idempotency_key = %s",
                (idempotency_key,),
            )
        row = cur.fetchone()
        assert row is not None
        return int(row[0])


def test_ac1_double_enqueue_same_key_is_a_noop(clean_table: None) -> None:
    """A second enqueue with the same idempotency_key adds no row and returns ALREADY_QUEUED."""
    assert _DSN is not None
    queue = WombatQueue(_DSN, max_size=10)
    try:
        first = queue.enqueue(QueueItem(idempotency_key="dedup-key", payload={"n": 1}))
        second = queue.enqueue(QueueItem(idempotency_key="dedup-key", payload={"n": 2}))

        assert first is EnqueueResult.QUEUED
        assert second is EnqueueResult.ALREADY_QUEUED
        assert _count("dedup-key") == 1
    finally:
        queue.close()


def test_ac2_enqueue_at_capacity_raises_and_adds_no_row(clean_table: None) -> None:
    """Filling the queue to max_size, then enqueuing once more, raises QueueFullError."""
    assert _DSN is not None
    queue = WombatQueue(_DSN, max_size=3)
    try:
        for i in range(3):
            result = queue.enqueue(QueueItem(idempotency_key=f"cap-{i}", payload={"i": i}))
            assert result is EnqueueResult.QUEUED
        assert _count() == 3

        with pytest.raises(QueueFullError):
            queue.enqueue(QueueItem(idempotency_key="cap-overflow", payload={"i": 99}))

        assert _count() == 3  # no row added by the refused enqueue
    finally:
        queue.close()


def test_ac4_duplicate_key_at_capacity_is_a_noop_new_key_still_raises(clean_table: None) -> None:
    """TK-173 (CR-16): a queue AT max_size already containing key K -> enqueuing K again is an
    idempotent no-op (ALREADY_QUEUED, no raise). A genuinely NEW key at that same capacity still
    raises QueueFullError, unchanged."""
    assert _DSN is not None
    queue = WombatQueue(_DSN, max_size=3)
    try:
        for i in range(3):
            result = queue.enqueue(QueueItem(idempotency_key=f"cap-{i}", payload={"i": i}))
            assert result is EnqueueResult.QUEUED
        assert _count() == 3

        # Re-enqueuing an already-queued key at capacity is a no-op, not a refusal.
        dup = queue.enqueue(QueueItem(idempotency_key="cap-0", payload={"i": 0}))
        assert dup is EnqueueResult.ALREADY_QUEUED
        assert _count() == 3

        # A genuinely new key at capacity still raises and adds no row.
        with pytest.raises(QueueFullError):
            queue.enqueue(QueueItem(idempotency_key="cap-overflow", payload={"i": 99}))
        assert _count() == 3
    finally:
        queue.close()


def test_ac3_at_least_once_across_restart(clean_table: None) -> None:
    """Un-acked leased items are redelivered by a fresh WombatQueue (a restart / new epoch)."""
    assert _DSN is not None
    queue = WombatQueue(_DSN, max_size=10)
    try:
        for i in range(3):
            queue.enqueue(QueueItem(idempotency_key=f"restart-{i}", payload={"i": i}))

        first_drain = queue.drain()
        assert {item.idempotency_key for item in first_drain} == {
            "restart-0",
            "restart-1",
            "restart-2",
        }
        assert [item.idempotency_key for item in first_drain] == [
            "restart-0",
            "restart-1",
            "restart-2",
        ]  # FIFO order

        # No re-surfacing within the same run: calling drain() again before acking, on the
        # SAME instance/epoch, must not re-return rows we already leased ourselves.
        assert queue.drain() == []

        # No ack — simulate a kill mid-drain. A fresh WombatQueue on the SAME dsn is the
        # restart: a new epoch, so the (still-leased-by-the-dead-epoch) rows are foreign
        # leases and get reclaimed + redelivered.
        restarted = WombatQueue(_DSN, max_size=10)
        try:
            assert restarted.epoch != queue.epoch

            redrained = restarted.drain()
            assert {item.idempotency_key for item in redrained} == {
                "restart-0",
                "restart-1",
                "restart-2",
            }

            for item in redrained:
                assert item.item_id is not None
                restarted.ack(item.item_id)
                restarted.ack(item.item_id)  # second ack for the same id is a harmless no-op

            assert _count() == 0
            assert restarted.drain() == []  # acked items never resurface again
        finally:
            restarted.close()
    finally:
        queue.close()


def test_ac4_empty_queue_drain_returns_empty_list_immediately(clean_table: None) -> None:
    """drain() on an empty queue returns [] immediately."""
    assert _DSN is not None
    queue = WombatQueue(_DSN, max_size=10)
    try:
        assert queue.drain() == []
    finally:
        queue.close()


def test_pending_count_mirrors_drains_own_eligibility_predicate(clean_table: None) -> None:
    """TK-230 (DEC-41): ``pending_count()`` reads EXACTLY ``drain()``'s own eligibility predicate
    — a plain count, no lease taken. Rows leased by THIS epoch (already in-flight/parked) are
    excluded, so a run this instance already holds never re-fires the runtime pump."""
    assert _DSN is not None
    queue = WombatQueue(_DSN, max_size=10)
    try:
        assert queue.pending_count() == 0  # empty queue

        for i in range(3):
            queue.enqueue(QueueItem(idempotency_key=f"pending-{i}", payload={"i": i}))
        assert queue.pending_count() == 3

        # draining leases all 3 under THIS epoch -> they are now excluded from pending_count.
        drained = queue.drain()
        assert len(drained) == 3
        assert queue.pending_count() == 0

        # a fresh, unleased row is pending again.
        queue.enqueue(QueueItem(idempotency_key="pending-new", payload={}))
        assert queue.pending_count() == 1
    finally:
        queue.close()


def test_pending_count_counts_a_foreign_epochs_leased_rows_as_pending(
    clean_table: None,
) -> None:
    """A row leased by a DIFFERENT (dead, single-host v1) epoch still counts as pending — mirrors
    ``drain()`` reclaiming it, so a restarted pump keeps draining orphaned leases too."""
    assert _DSN is not None
    queue = WombatQueue(_DSN, max_size=10)
    try:
        queue.enqueue(QueueItem(idempotency_key="foreign-lease", payload={}))
        queue.drain()  # leases the row under `queue`'s epoch, never acked (simulated crash)

        restarted = WombatQueue(_DSN, max_size=10)
        try:
            assert restarted.epoch != queue.epoch
            assert restarted.pending_count() == 1  # the foreign lease is still eligible
        finally:
            restarted.close()
    finally:
        queue.close()


def test_drain_limit_leases_only_n_rows_fifo_leaving_the_rest_unleased(
    clean_table: None,
) -> None:
    """drain(limit=2) of 3 enqueued items leases+returns exactly the oldest 2 (FIFO); the 3rd
    stays UNLEASED and is returned by a later drain() on the SAME instance/epoch (Q-47/TK-5)."""
    assert _DSN is not None
    queue = WombatQueue(_DSN, max_size=10)
    try:
        for i in range(3):
            queue.enqueue(QueueItem(idempotency_key=f"limit-{i}", payload={"i": i}))

        first = queue.drain(limit=2)
        assert [item.idempotency_key for item in first] == ["limit-0", "limit-1"]

        # The 3rd row is still unleased in the DB — proves limit leased ONLY those N rows, not
        # drain-all followed by in-memory slicing (which would have leased all 3).
        with psycopg.connect(_DSN) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT leased_by FROM wombat_queue WHERE idempotency_key = %s",
                ("limit-2",),
            )
            row = cur.fetchone()
            assert row is not None
            assert row[0] is None

        # A subsequent drain() on the SAME instance/epoch picks up the still-unleased 3rd row.
        second = queue.drain()
        assert [item.idempotency_key for item in second] == ["limit-2"]
    finally:
        queue.close()
