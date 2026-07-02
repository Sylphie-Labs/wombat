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
