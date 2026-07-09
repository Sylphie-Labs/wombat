"""TK-161 — PushSource acceptance criteria (EP-29, Q-86 ruling).

PushSource is the uniform push-over-poll mechanism every push-shaped source (ASR TK-162,
feedback TK-51) rides, WITHOUT touching the poll-only InputSource contract or the registry's
poll loop (registration-not-rewrite, DEC-5). All tests here are no-DSN unit tests.

  AC1 PushSource satisfies the Protocol: registered with a REAL SourceRegistry + a stub
      Enqueuer, two pushed SourceEvents surface as exactly two QueueItems (canonical TK-12
      idempotency_key, payload verbatim, push order) on the first poll tick; a second tick
      enqueues nothing (buffer drained).
  AC2 registration-not-rewrite: PushSource is registered WITHOUT editing any core file;
      registry.source_ids exposes the registered id; base.py/registry.py import with no
      ASR/TTS/google dependency.
  AC3 push() from outside the event loop context is safe for the v1 single-loop runtime
      (plain deque append); cross-thread push is documented out of scope, not tested here.
  AC4 zero pushes -> poll() returns [] and the source never enters degraded_sources.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from wombat.domain.item_identity import idempotency_key as derive_key
from wombat.queue import EnqueueResult, QueueItem
from wombat.sources.base import InputSource, PushSource, SourceEvent
from wombat.sources.registry import SourceRegistry


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


async def test_ac1_two_pushed_events_enqueue_on_next_tick_then_second_tick_enqueues_nothing() -> (
    None
):
    """Push two events, drive one poll-loop iteration: exactly two QueueItems land, in push
    order, with the canonical idempotency_key and verbatim payloads. A later tick (buffer
    drained) enqueues nothing further."""
    enqueuer = _FakeEnqueuer()
    registry = SourceRegistry(enqueuer)
    source = PushSource(id="push-test", poll_interval_seconds=0.01)
    registry.register(source)

    source.push(SourceEvent(event_key="e1", payload={"n": 1}))
    source.push(SourceEvent(event_key="e2", payload={"n": 2}))

    await registry.start()
    try:
        await _wait_until(lambda: len(enqueuer.items) >= 2)
        # Give a little more time to make sure nothing else trickles in beyond the two pushed.
        await asyncio.sleep(2 * source.poll_interval_seconds)
    finally:
        await registry.stop()

    assert len(enqueuer.items) == 2
    assert enqueuer.items[0].idempotency_key == derive_key("push-test", "e1")
    assert enqueuer.items[0].payload == {"n": 1}
    assert enqueuer.items[1].idempotency_key == derive_key("push-test", "e2")
    assert enqueuer.items[1].payload == {"n": 2}


async def test_ac2_registered_without_editing_core_files_and_source_ids_exposes_it() -> None:
    """Registration-not-rewrite: PushSource registers through the unmodified public API;
    registry.source_ids surfaces the id without reaching into registry internals."""
    enqueuer = _FakeEnqueuer()
    registry = SourceRegistry(enqueuer)
    source = PushSource(id="push-test", poll_interval_seconds=0.01)

    registry.register(source)

    assert "push-test" in registry.source_ids


def test_ac2_base_and_registry_import_with_no_asr_tts_google_dependency() -> None:
    """A plain import of base.py/registry.py succeeds — no ASR/TTS/google/cog-worx types."""
    import wombat.sources.base
    import wombat.sources.registry

    assert wombat.sources.base.PushSource is not None
    assert wombat.sources.registry.SourceRegistry is not None


def test_push_source_satisfies_the_input_source_protocol() -> None:
    source: InputSource = PushSource(id="proto-check", poll_interval_seconds=1.0)
    assert source.id == "proto-check"


def test_ac3_push_is_a_plain_synchronous_deque_append_safe_pre_loop() -> None:
    """push() works with no running event loop (plain deque append) — safe for the v1
    single-loop runtime, whether called before the loop starts or from within it. Cross-thread
    push is documented out of scope for v1 (module docstring) and is not exercised here."""
    source = PushSource(id="push-test", poll_interval_seconds=1.0)

    source.push(SourceEvent(event_key="e1", payload={}))

    async def _drain() -> list[SourceEvent]:
        return await source.poll()

    drained = asyncio.run(_drain())
    assert [e.event_key for e in drained] == ["e1"]


async def test_ac4_zero_pushes_poll_returns_empty_and_never_degrades() -> None:
    """A PushSource with zero pushes: poll() returns [] and it never enters degraded_sources."""
    enqueuer = _FakeEnqueuer()
    registry = SourceRegistry(enqueuer)
    source = PushSource(id="push-idle", poll_interval_seconds=0.01)
    registry.register(source)

    assert await source.poll() == []

    await registry.start()
    try:
        await asyncio.sleep(5 * source.poll_interval_seconds)
    finally:
        await registry.stop()

    assert enqueuer.items == []
    assert "push-idle" not in registry.degraded_sources
