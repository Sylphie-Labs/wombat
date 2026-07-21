"""TK-286 — persisted SeenLedger + DedupingEnqueuer acceptance criteria (DEC-63a).

LIVE DEFECT this closes (logs/runtime-20260720-192648.log): the same gmail message flushed 5x
~10min apart -- GmailPoller re-emits every in-window message every 300s poll, and WombatQueue's
``ON CONFLICT (idempotency_key)`` dedup only holds while the row is LIVE (``ack()`` DELETEs it),
so each next poll re-inserts cleanly. ``DedupingEnqueuer`` closes this at the ONE seam every
source shares: the registry's enqueue.

Unit ACs (AC1, AC4, AC5, the DEC-57 pin) use an in-memory fake ``SeenLedger`` double and a fake
inner ``Enqueuer`` -- no Postgres required. AC2/AC3 are pg-gated on ``WOMBAT_TEST_PG_DSN`` (the
SAME convention as ``tests/gate/test_pending_journal_pg.py``): absent it, those tests SKIP loudly.
Spin up a throwaway Postgres locally:

    docker run --rm -d -p 5433:5432 -e POSTGRES_PASSWORD=wombat postgres:16
    WOMBAT_TEST_PG_DSN=postgresql://postgres:wombat@localhost:5433/postgres

  AC1 (unit): same (key, payload) enqueued twice -> inner called exactly once, second call
      returns ALREADY_QUEUED without touching inner. A never-seen key passes through with
      inner's result verbatim.
  AC2 (pg repro): real WombatQueue + real SeenLedger -- enqueue, drain, ack (row DELETEd),
      enqueue the identical event again -> pending_count stays 0, drain returns empty (the
      live repeat-flush loop is structurally dead).
  AC3 (pg restart): a FRESH SeenLedger constructed over the SAME dsn still skips the identical
      event (the ledger survives a process restart -- it is Postgres-backed, not in-memory).
  AC4 (unit): a known key with a CHANGED payload -> inner IS called, and the new fingerprint is
      recorded (a legitimately-updated calendar event can still re-enter).
  AC5 (unit): inner raises QueueFullError -> the raise propagates, NOTHING is recorded, and a
      retry of the identical event still reaches inner (at-least-once is preserved).
  DEC-57 pin: a uuid4-style chat key is never-seen by construction -- its first enqueue always
      passes through to inner (chat-always-answers stays byte-untouched by this ticket).
"""

from __future__ import annotations

import os
import uuid

import psycopg
import pytest

from wombat.queue import EnqueueResult, QueueFullError, QueueItem, WombatQueue
from wombat.queue import ensure_schema as ensure_queue_schema
from wombat.sources.seen_ledger import DedupingEnqueuer, SeenLedger, ensure_schema, fingerprint

_DSN = os.environ.get("WOMBAT_TEST_PG_DSN")

_requires_pg = pytest.mark.skipif(
    not _DSN,
    reason=(
        "WOMBAT_TEST_PG_DSN is not set — skipping SeenLedger DB tests that require a real "
        "throwaway Postgres. Start one with:\n"
        "  docker run --rm -d -p 5433:5432 -e POSTGRES_PASSWORD=wombat postgres:16\n"
        "then export WOMBAT_TEST_PG_DSN=postgresql://postgres:wombat@localhost:5433/postgres"
    ),
)


@pytest.fixture
def clean_tables() -> None:
    """Ensure both schemas exist and their tables are empty before each pg test."""
    assert _DSN is not None
    with psycopg.connect(_DSN) as conn:
        ensure_schema(conn)
        ensure_queue_schema(conn)
        with conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE wombat_seen_events")
            cur.execute("TRUNCATE TABLE wombat_queue")
        conn.commit()


class _InMemorySeenLedger:
    """A fake ``SeenLedger`` double: same ``seen``/``record`` surface, no Postgres."""

    def __init__(self) -> None:
        self._rows: dict[str, str] = {}

    def seen(self, idempotency_key: str) -> str | None:
        return self._rows.get(idempotency_key)

    def record(self, idempotency_key: str, payload_fingerprint: str) -> None:
        self._rows[idempotency_key] = payload_fingerprint


class _FakeInner:
    """A fake inner ``Enqueuer``: records every call and returns a scripted result/raise."""

    def __init__(self) -> None:
        self.calls: list[QueueItem] = []
        self._next_result: EnqueueResult | Exception = EnqueueResult.QUEUED

    def script(self, result: EnqueueResult | Exception) -> None:
        self._next_result = result

    def enqueue(self, item: QueueItem) -> EnqueueResult:
        self.calls.append(item)
        if isinstance(self._next_result, Exception):
            raise self._next_result
        return self._next_result


# --------------------------------------------------------------------------------------- AC1


def test_ac1_repeated_identical_event_calls_inner_exactly_once() -> None:
    inner = _FakeInner()
    deduper = DedupingEnqueuer(inner, _InMemorySeenLedger())
    item = QueueItem(idempotency_key="gmail:msg-1", payload={"subject": "hello"})

    first = deduper.enqueue(item)
    second = deduper.enqueue(item)

    assert first is EnqueueResult.QUEUED
    assert second is EnqueueResult.ALREADY_QUEUED
    assert len(inner.calls) == 1


def test_ac1_never_seen_key_passes_through_with_inners_result_verbatim() -> None:
    inner = _FakeInner()
    inner.script(EnqueueResult.ALREADY_QUEUED)  # inner's own verbatim result
    deduper = DedupingEnqueuer(inner, _InMemorySeenLedger())
    item = QueueItem(idempotency_key="gmail:msg-2", payload={"subject": "world"})

    result = deduper.enqueue(item)

    assert result is EnqueueResult.ALREADY_QUEUED  # inner's result, not a ledger short-circuit
    assert len(inner.calls) == 1


# --------------------------------------------------------------------------------------- AC2


@_requires_pg
def test_ac2_ack_then_repoll_of_identical_event_stays_structurally_dead(
    clean_tables: None,
) -> None:
    """The live repro: enqueue, drain, ack (DELETEs the wombat_queue row), then enqueue the
    IDENTICAL event again -- pending_count stays 0 and drain returns empty."""
    assert _DSN is not None
    queue = WombatQueue(_DSN, max_size=100)
    ledger = SeenLedger(_DSN)
    try:
        deduper = DedupingEnqueuer(queue, ledger)
        item = QueueItem(
            idempotency_key="gmail:tk286-ac2", payload={"subject": "same message, again"}
        )

        assert deduper.enqueue(item) is EnqueueResult.QUEUED
        drained = queue.drain()
        assert len(drained) == 1
        drained_item_id = drained[0].item_id
        assert drained_item_id is not None
        queue.ack(drained_item_id)  # DELETEs the wombat_queue row -- the live defect's seam

        result = deduper.enqueue(item)  # the poller re-emits the SAME in-window event

        assert result is EnqueueResult.ALREADY_QUEUED
        assert queue.pending_count() == 0
        assert queue.drain() == []
    finally:
        queue.close()
        ledger.close()


# --------------------------------------------------------------------------------------- AC3


@_requires_pg
def test_ac3_fresh_ledger_over_same_dsn_still_skips_after_restart(clean_tables: None) -> None:
    """A restart constructs a FRESH SeenLedger over the SAME dsn -- the identical event is still
    skipped, proving the ledger is durable (Postgres-backed), not per-process in-memory."""
    assert _DSN is not None
    queue = WombatQueue(_DSN, max_size=100)
    first_ledger = SeenLedger(_DSN)
    try:
        item = QueueItem(idempotency_key="gcal:tk286-ac3", payload={"summary": "standup"})
        deduper = DedupingEnqueuer(queue, first_ledger)
        assert deduper.enqueue(item) is EnqueueResult.QUEUED
    finally:
        queue.close()
        first_ledger.close()

    fresh_queue = WombatQueue(_DSN, max_size=100)
    fresh_ledger = SeenLedger(_DSN)
    try:
        fresh_deduper = DedupingEnqueuer(fresh_queue, fresh_ledger)
        result = fresh_deduper.enqueue(item)
        assert result is EnqueueResult.ALREADY_QUEUED
    finally:
        fresh_queue.close()
        fresh_ledger.close()


# --------------------------------------------------------------------------------------- AC4


def test_ac4_known_key_with_changed_payload_re_enqueues_and_records_new_fingerprint() -> None:
    inner = _FakeInner()
    ledger = _InMemorySeenLedger()
    deduper = DedupingEnqueuer(inner, ledger)
    key = "gcal:tk286-ac4"

    first = deduper.enqueue(QueueItem(idempotency_key=key, payload={"summary": "standup 9am"}))
    second = deduper.enqueue(
        QueueItem(idempotency_key=key, payload={"summary": "standup 10am (moved)"})
    )

    assert first is EnqueueResult.QUEUED
    assert second is EnqueueResult.QUEUED  # NOT ALREADY_QUEUED -- the payload genuinely changed
    assert len(inner.calls) == 2
    assert ledger.seen(key) == fingerprint({"summary": "standup 10am (moved)"})


# --------------------------------------------------------------------------------------- AC5


def test_ac5_inner_raise_propagates_unrecorded_and_a_retry_reaches_inner_again() -> None:
    inner = _FakeInner()
    inner.script(QueueFullError("wombat_queue is at capacity"))
    ledger = _InMemorySeenLedger()
    deduper = DedupingEnqueuer(inner, ledger)
    item = QueueItem(idempotency_key="gmail:tk286-ac5", payload={"subject": "dropped"})

    with pytest.raises(QueueFullError):
        deduper.enqueue(item)

    assert ledger.seen(item.idempotency_key) is None  # NOTHING recorded on a raise
    assert len(inner.calls) == 1

    inner.script(EnqueueResult.QUEUED)  # capacity frees up before the next poll
    retried = deduper.enqueue(item)  # a later poll retries the SAME event

    assert retried is EnqueueResult.QUEUED
    assert len(inner.calls) == 2  # inner was reached again -- at-least-once preserved


# --------------------------------------------------------------------------------------- DEC-57


def test_dec57_pin_uuid4_style_chat_key_is_never_seen_and_passes_through() -> None:
    """A chat turn's idempotency_key is minted fresh (uuid4-based) every turn -- it is
    never-seen by construction, so its first (only) enqueue always reaches inner unchanged
    (DEC-57 chat-always-answers stays byte-untouched by this ticket)."""
    inner = _FakeInner()
    deduper = DedupingEnqueuer(inner, _InMemorySeenLedger())
    chat_key = f"chat:{uuid.uuid4().hex}"
    item = QueueItem(idempotency_key=chat_key, payload={"text": "what's on my calendar?"})

    result = deduper.enqueue(item)

    assert result is EnqueueResult.QUEUED
    assert len(inner.calls) == 1
