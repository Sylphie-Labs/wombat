"""TK-3 — SourceRegistry acceptance criteria (EP-3, Q-32, Q-36/Q-46, ASMP-2).

AC1-4 are unit tests against an INJECTED ``Enqueuer`` seam (Q-36/Q-46) — a fake that records
calls, never a real Postgres. ONE additional test exercises the same end-to-end flow against a
real ``wombat.queue.WombatQueue`` and is gated on ``WOMBAT_TEST_PG_DSN`` (absent it, skipped
loudly), matching the pattern in ``tests/unit/test_queue.py`` / ``test_daily_ledger.py``:

    docker run --rm -d -p 5433:5432 -e POSTGRES_PASSWORD=wombat postgres:16
    WOMBAT_TEST_PG_DSN=postgresql://postgres:wombat@localhost:5433/postgres

Poll intervals are kept very small (hundredths of a second) and waits are event-driven
(poll/counter until a condition or a bounded timeout) so the suite stays fast and
non-flaky without sleeping for real wall-clock production intervals.

  AC1 a stub source registered + start() -> start() called, poll() runs, its event becomes a
      QueueItem enqueued (end-to-end).
  AC2 stop() -> the source's stop() is called; no further poll() within 2x its poll_interval.
  AC3 two sources with different intervals -> each is polled on its own interval.
  AC4 poll() raises -> logged with the source id, that source is marked degraded, the registry
      keeps polling the other source without crashing.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import psycopg
import pytest

from wombat.queue import EnqueueResult, QueueItem, WombatQueue, ensure_schema
from wombat.sources.base import SourceEvent
from wombat.sources.registry import SourceRegistry

_DSN = os.environ.get("WOMBAT_TEST_PG_DSN")

_requires_pg = pytest.mark.skipif(
    not _DSN,
    reason=(
        "WOMBAT_TEST_PG_DSN is not set — skipping the real-WombatQueue SourceRegistry test. "
        "Start one with:\n"
        "  docker run --rm -d -p 5433:5432 -e POSTGRES_PASSWORD=wombat postgres:16\n"
        "then export WOMBAT_TEST_PG_DSN=postgresql://postgres:wombat@localhost:5433/postgres"
    ),
)


@dataclass
class _StubSource:
    """A minimal InputSource stub: fixed/derived events per poll, optional injected failure."""

    id: str
    poll_interval_seconds: float
    events_by_call: list[list[SourceEvent]] = field(default_factory=list)
    fail_with: Exception | None = None
    start_called: int = 0
    stop_called: int = 0
    poll_count: int = 0

    async def start(self) -> None:
        self.start_called += 1

    async def stop(self) -> None:
        self.stop_called += 1

    async def poll(self) -> list[SourceEvent]:
        self.poll_count += 1
        if self.fail_with is not None:
            raise self.fail_with
        if not self.events_by_call:
            return []
        index = min(self.poll_count - 1, len(self.events_by_call) - 1)
        return self.events_by_call[index]


class _FakeEnqueuer:
    """Records every enqueue() call — the injected seam AC1 verifies end-to-end against."""

    def __init__(self) -> None:
        self.items: list[QueueItem] = []

    def enqueue(self, item: QueueItem) -> EnqueueResult:
        self.items.append(item)
        return EnqueueResult.QUEUED


async def _wait_until(
    predicate: Callable[[], bool], *, timeout: float = 2.0, interval: float = 0.005
) -> None:
    """Poll ``predicate`` until true or ``timeout`` elapses (event-driven, no fixed sleeps)."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() >= deadline:
            raise AssertionError(f"condition not met within {timeout}s")
        await asyncio.sleep(interval)


async def test_ac1_registered_source_start_poll_enqueue_end_to_end() -> None:
    """start() calls the source's start(); its polled event lands as an enqueued QueueItem."""
    enqueuer = _FakeEnqueuer()
    registry = SourceRegistry(enqueuer)
    stub = _StubSource(
        id="test",
        poll_interval_seconds=0.01,
        events_by_call=[[SourceEvent(event_key="e1", payload={"x": 1})]],
    )
    registry.register(stub)

    await registry.start()
    try:
        await _wait_until(lambda: len(enqueuer.items) >= 1)
    finally:
        await registry.stop()

    assert stub.start_called == 1
    assert enqueuer.items[0].idempotency_key == "test:e1"
    assert enqueuer.items[0].payload == {"x": 1}


async def test_ac2_stop_halts_polling_within_2x_interval() -> None:
    """stop() calls the source's stop(); no further poll() within 2x its poll_interval."""
    enqueuer = _FakeEnqueuer()
    registry = SourceRegistry(enqueuer)
    stub = _StubSource(id="test", poll_interval_seconds=0.02)
    registry.register(stub)

    await registry.start()
    await _wait_until(lambda: stub.poll_count >= 1)
    await registry.stop()

    assert stub.stop_called == 1
    count_at_stop = stub.poll_count

    await asyncio.sleep(2 * stub.poll_interval_seconds)
    assert stub.poll_count == count_at_stop


async def test_ac3_two_sources_polled_on_their_own_intervals() -> None:
    """Two sources with different poll_interval_seconds are each polled on their own cadence."""
    enqueuer = _FakeEnqueuer()
    registry = SourceRegistry(enqueuer)
    fast = _StubSource(id="fast", poll_interval_seconds=0.01)
    slow = _StubSource(id="slow", poll_interval_seconds=0.08)
    registry.register(fast)
    registry.register(slow)

    await registry.start()
    try:
        await asyncio.sleep(0.25)
    finally:
        await registry.stop()

    assert fast.poll_count >= 5
    assert slow.poll_count >= 1
    # fast (10ms) must have been polled substantially more often than slow (80ms) — each ran on
    # its OWN interval, not the other's.
    assert fast.poll_count > slow.poll_count * 2


async def test_ac4_poll_error_is_logged_degrades_source_others_keep_polling(
    caplog: Any,
) -> None:
    """A raising poll() is logged with the source id, marks that source degraded, and the
    registry keeps polling the other source without crashing."""
    enqueuer = _FakeEnqueuer()
    registry = SourceRegistry(enqueuer)
    bad = _StubSource(id="bad", poll_interval_seconds=0.01, fail_with=RuntimeError("boom"))
    good = _StubSource(id="good", poll_interval_seconds=0.01)
    registry.register(bad)
    registry.register(good)

    with caplog.at_level(logging.ERROR):
        await registry.start()
        try:
            await _wait_until(lambda: bad.poll_count >= 1 and good.poll_count >= 2)
        finally:
            await registry.stop()

    assert "bad" in registry.degraded_sources
    assert "good" not in registry.degraded_sources
    assert good.poll_count >= 2  # the registry kept polling the other source

    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert any("bad" in record.message for record in error_records)


@_requires_pg
async def test_ac1_end_to_end_against_a_real_wombat_queue() -> None:
    """The same AC1 flow, wired to a real WombatQueue instead of a fake — a row lands."""
    assert _DSN is not None
    with psycopg.connect(_DSN) as conn:
        ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE wombat_queue")
        conn.commit()

    queue = WombatQueue(_DSN, max_size=10)
    try:
        registry = SourceRegistry(queue)
        stub = _StubSource(
            id="real",
            poll_interval_seconds=0.01,
            events_by_call=[[SourceEvent(event_key="e1", payload={"n": 1})]],
        )
        registry.register(stub)

        await registry.start()
        try:
            await _wait_until(lambda: stub.poll_count >= 1)
            # Give the enqueue() call (issued right after poll()) a moment to land.
            await asyncio.sleep(0.02)
        finally:
            await registry.stop()

        drained = queue.drain()
        assert [item.idempotency_key for item in drained] == ["real:e1"]
        assert drained[0].payload == {"n": 1}
    finally:
        queue.close()
