"""TK-5 — DrainQueueStage acceptance criteria (Q-47, first cog-worx Stage integration).

PURE stage tests (no Postgres) inject a tiny FAKE queue + the reusable ``StageContextFake``
(``tests/support/stage_context_fake.py``) and assert the Transition/Done shapes (TK-230, DEC-41:
an empty drain is ``Done``, never ``Wait`` — see below), the Artifact kind/produced_by/data
round-trip, and that the stage touches ONLY ``ctx.clock()``.

ONE gated integration test runs a real ``WombatQueue`` against a throwaway Postgres (gated on
``WOMBAT_TEST_PG_DSN`` — absent it, this single test SKIPS while the pure tests above still run):

    docker run --rm -d -p 5433:5432 -e POSTGRES_PASSWORD=wombat postgres:16
    WOMBAT_TEST_PG_DSN=postgresql://postgres:wombat@localhost:5433/postgres

It proves AC1 (2 downstream, the 3rd remains in the queue) and AC3 (at-least-once: a fresh-epoch
WombatQueue redelivers the un-acked 3rd item) through the stage, not by re-simulating the engine.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime

import psycopg
import pytest
from cogworx.loop.graph import StageGraph
from cogworx.loop.result import Done, StageResult, Transition
from cogworx.loop.stage import StageContext

# tests/support is a sibling package of tests/unit under the tests/ package root (TK-15).
from tests.support.stage_context_fake import StageContextFake
from wombat.pathways.drain_pathway import build_drain_pathway
from wombat.queue import QueueItem, WombatQueue, ensure_schema
from wombat.stages.artifacts import (
    DRAIN_HEARTBEAT,
    DRAINED_BATCH,
    queue_items_from_artifact_data,
    queue_items_to_artifact_data,
)
from wombat.stages.drain_queue import DrainQueueStage

_FIXED_NOW = datetime(2026, 7, 2, 12, 0, 0, tzinfo=UTC)

_DSN = os.environ.get("WOMBAT_TEST_PG_DSN")


@dataclass
class _FakeQueue:
    """A bare stub satisfying DrainQueueStage's ``drain(limit)`` seam — no Postgres involved."""

    canned: list[QueueItem]
    seen_limit: int | None = None

    def drain(self, limit: int | None = None) -> list[QueueItem]:
        self.seen_limit = limit
        return self.canned


# --- round-trip: the artifact helpers are the ONLY (de)serialization path between stages -------


def test_queue_items_artifact_data_round_trip_is_lossless() -> None:
    items = [
        QueueItem(idempotency_key="x", payload={"a": [1, 2], "b": "s"}, item_id=7),
        QueueItem(idempotency_key="y", payload={}, item_id=None),
    ]

    data = queue_items_to_artifact_data(items)

    assert data == {
        "items": [
            {"idempotency_key": "x", "payload": {"a": [1, 2], "b": "s"}, "item_id": 7},
            {"idempotency_key": "y", "payload": {}, "item_id": None},
        ]
    }
    assert queue_items_from_artifact_data(data) == items


# --- pure stage tests ---------------------------------------------------------------------------


async def test_items_present_yields_transition_to_gate_with_drained_batch_artifact() -> None:
    items = [
        QueueItem(idempotency_key="a", payload={"n": 1}, item_id=1),
        QueueItem(idempotency_key="b", payload={"n": 2}, item_id=2),
    ]
    queue = _FakeQueue(canned=items)
    stage = DrainQueueStage(queue, batch_size=2, poll_interval_seconds=5.0)
    ctx = StageContextFake(now_fn=lambda: _FIXED_NOW)

    result = await stage.run(ctx)

    assert queue.seen_limit == 2  # batch_size flows straight into the drain(limit=) call
    assert isinstance(result, Transition)
    assert result.to == "gate"
    assert result.output.kind == DRAINED_BATCH
    assert result.output.produced_by == "drain_queue"
    assert queue_items_from_artifact_data(result.output.data) == items


async def test_empty_queue_yields_done_with_drain_heartbeat_artifact() -> None:
    """TK-230 (DEC-41, CRF-2): an empty drain returns ``Done``, NEVER ``Wait`` — the stage never
    self-parks any more (idling-on-empty is now the runtime pump's job, ``wombat.runtime``)."""
    queue = _FakeQueue(canned=[])
    stage = DrainQueueStage(queue, batch_size=5, poll_interval_seconds=30.0)
    ctx = StageContextFake(now_fn=lambda: _FIXED_NOW)

    result = await stage.run(ctx)

    assert isinstance(result, Done)
    assert result.kind == "done"
    assert result.output.kind == DRAIN_HEARTBEAT
    assert result.output.produced_by == "drain_queue"
    assert result.output.data == {}


async def test_stage_touches_no_ctx_member_beyond_clock() -> None:
    """A ctx that raises on everything but clock()/last_output() must not blow up the stage —
    proving the stage's ctx surface really is just ``ctx.clock()`` (Q-47)."""
    queue = _FakeQueue(canned=[QueueItem(idempotency_key="only", payload={}, item_id=1)])
    stage = DrainQueueStage(queue, batch_size=1, poll_interval_seconds=1.0)
    ctx = StageContextFake(now_fn=lambda: _FIXED_NOW)

    # Would raise NotImplementedError (via StageContextFake) if the stage reached for ctx.model,
    # ctx.journal, ctx.emit, etc. Also proves the stage never acks: _FakeQueue defines no ack
    # method at all, so a stray ack() call would raise AttributeError.
    result = await stage.run(ctx)

    assert isinstance(result, Transition)


def test_stage_context_fake_raises_on_every_member_except_clock_and_last_output() -> None:
    ctx = StageContextFake(now_fn=lambda: _FIXED_NOW)

    assert ctx.clock() == _FIXED_NOW
    for member in ("model", "journal", "graph", "latent", "budget"):
        with pytest.raises(NotImplementedError):
            getattr(ctx, member)


# --- partial pathway assembly --------------------------------------------------------------------


@dataclass
class _SinkStage:
    """A TEST-LOCAL terminal stage — TK-5 ships no placeholder/sink stage in ``src`` (that's
    TK-7's full real assembly); this fake only satisfies StageGraph's termination-by-construction
    check so build_drain_pathway has a valid 'gate' edge target to wire DrainQueueStage into."""

    name: str = "gate"
    transitions: tuple[str, ...] = ()

    async def run(self, ctx: StageContext) -> StageResult:  # pragma: no cover - never invoked
        raise NotImplementedError


def test_build_drain_pathway_wires_drain_queue_stage_to_its_declared_transition() -> None:
    drain_stage = DrainQueueStage(_FakeQueue(canned=[]), batch_size=1, poll_interval_seconds=1.0)
    sink = _SinkStage()

    graph = build_drain_pathway(drain_stage, sink)

    assert isinstance(graph, StageGraph)
    assert graph.entry == "drain_queue"
    assert graph.transitions_from("drain_queue") == ("gate",)
    assert graph.is_terminal("gate")


# --- gated integration test (real Postgres) -----------------------------------------------------


@pytest.mark.skipif(
    _DSN is None,
    reason=(
        "WOMBAT_TEST_PG_DSN is not set — skipping the DrainQueueStage integration test that "
        "requires a real throwaway Postgres. Start one with:\n"
        "  docker run --rm -d -p 5433:5432 -e POSTGRES_PASSWORD=wombat postgres:16\n"
        "then export WOMBAT_TEST_PG_DSN=postgresql://postgres:wombat@localhost:5433/postgres"
    ),
)
async def test_gated_stage_drains_2_of_3_and_the_3rd_redelivers_after_restart() -> None:
    assert _DSN is not None
    with psycopg.connect(_DSN) as conn:
        ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE wombat_queue")
        conn.commit()

    queue = WombatQueue(_DSN, max_size=10)
    try:
        for i in range(3):
            queue.enqueue(QueueItem(idempotency_key=f"int-{i}", payload={"i": i}))

        stage = DrainQueueStage(queue, batch_size=2, poll_interval_seconds=5.0)
        ctx = StageContextFake(now_fn=lambda: _FIXED_NOW)

        result = await stage.run(ctx)

        assert isinstance(result, Transition)
        drained = queue_items_from_artifact_data(result.output.data)
        assert [item.idempotency_key for item in drained] == ["int-0", "int-1"]  # AC1: 2 downstream

        # AC1 "rest remain": the 3rd item is still unleased in the DB — the stage's batch_size
        # limit leased ONLY the 2 handed downstream, orphaning nothing.
        with psycopg.connect(_DSN) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT leased_by FROM wombat_queue WHERE idempotency_key = %s", ("int-2",)
            )
            row = cur.fetchone()
            assert row is not None
            assert row[0] is None

        # AC3 at-least-once through the stage: the stage never acks, so on a real restart (a
        # fresh WombatQueue = a new epoch) the SAME rows this queue instance held leased —
        # including the 2 already handed downstream and never acked — are foreign leases from
        # a (per DEC-6, single-host/single-process) necessarily-dead prior process, and are all
        # reclaimed. This is the TK-2-proven queue mechanism; the 3rd item, never even drained,
        # is naturally part of that same redelivery.
        restarted = WombatQueue(_DSN, max_size=10)
        try:
            assert restarted.epoch != queue.epoch
            redrained = restarted.drain()
            assert {item.idempotency_key for item in redrained} == {"int-0", "int-1", "int-2"}
        finally:
            restarted.close()
    finally:
        queue.close()
