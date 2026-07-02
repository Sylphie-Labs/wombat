"""TK-7 — the drain-pathway demo skeleton: a REAL cog-worx Engine, end-to-end (EP-4, Q-52).

The full stub-gate drain spine, runnable end-to-end: ``cold_boot_bundle()`` (in-memory journal/
graph/latent + an empty ``PathwayRegistry``) + a REAL ``Engine`` + ``build_drain_pathway`` wiring
``DrainQueueStage`` -> ``GateStage`` (``stub_evaluate``) -> ``ReviewOrSpeakStage`` ->
``ComposeDispatchRouter`` -> ``ComposeStage`` (a deterministic ``FakeModel`` registered via
``ModelRegistry.register_factory`` — NO network) over ONE real ``WombatQueue`` on a throwaway
Postgres.

ALL tests in this module require a real Postgres and are gated on ``WOMBAT_TEST_PG_DSN`` (Q-46):
absent it, the whole module is skipped LOUDLY at collection time (never faked, never CI-failed on
a fresh clone), mirroring ``tests/unit/test_queue.py``:

    docker run --rm -d -p 5511:5432 -e POSTGRES_PASSWORD=wombat postgres:16
    WOMBAT_TEST_PG_DSN=postgresql://postgres:wombat@localhost:5511/postgres

IDLE SCENARIO — route-guard fix (Q-53 rider on TK-7): the "idle" scenario (a second drive on an
empty queue parks the run on a ``Wait``) originally tripped a pre-existing defect in
``DrainQueueStage`` (TK-5): ``transitions = ("gate",)`` did not declare a self-edge, yet its
empty-queue path returns ``Wait(to="drain_queue")`` (self), so a REAL ``Engine`` drive's
declared-route guard (``graph.edges_from(current)`` — see ``cogworx.runtime.engine._drive``)
rejected the ``Wait`` with ``StageGraphError`` the moment the queue went empty (TK-5's own tests
only ever drove the stage via ``StageContextFake``, so the gap was never exercised until this e2e).
The architect sanctioned the one-line fix as a cross-ticket rider (Q-53):
``DrainQueueStage.transitions = ("gate", "drain_queue")`` declares BOTH real edges, so the idle
``Wait`` is now accepted and the run reaches ``WAITING`` cleanly — proven by the real (un-stubbed)
idle test below.
"""

from __future__ import annotations

import functools
import os
from datetime import UTC, datetime

import psycopg
import pytest
from cogworx.claims.provenance import Artifact, Provenance
from cogworx.loop.state import RunStatus
from cogworx.model.base import ModelResponse
from cogworx.model.registry import ModelRegistry
from cogworx.runtime.engine import Engine

from support.stage_context_fake import FakeModel
from wombat.compose.templates import TemplateComposer
from wombat.config import WombatConfig
from wombat.gate.gate import stub_evaluate
from wombat.gate.models import ItemKind
from wombat.pathways.drain_pathway import build_drain_pathway
from wombat.queue import QueueItem, WombatQueue, ensure_schema
from wombat.sources.presence import PresenceSnapshot, PresenceState
from wombat.stages.artifacts import (
    COMPOSED_OUTPUT,
    HOLD_REPORT,
    composed_output_from_artifact_data,
)
from wombat.stages.compose import ComposeStage
from wombat.stages.compose_dispatch_router import ComposeDispatchRouter
from wombat.stages.drain_queue import DrainQueueStage
from wombat.stages.gate_stage import GateStage
from wombat.stages.review_or_speak import ReviewOrSpeakStage
from wombat.substrate import cold_boot_bundle

_DSN = os.environ.get("WOMBAT_TEST_PG_DSN")

if not _DSN:
    pytest.skip(
        "WOMBAT_TEST_PG_DSN is not set — skipping the TK-7 drain-pathway e2e demo skeleton, "
        "which requires a real throwaway Postgres. Start one with:\n"
        "  docker run --rm -d -p 5511:5432 -e POSTGRES_PASSWORD=wombat postgres:16\n"
        "then export WOMBAT_TEST_PG_DSN=postgresql://postgres:wombat@localhost:5511/postgres",
        allow_module_level=True,
    )

_FIXED_NOW = datetime(2026, 7, 2, 12, 0, 0, tzinfo=UTC)
_PATHWAY_ID = "drain"
_URGENCY_THRESHOLD = 0.5
_STALENESS_CEILING_S = 300.0
_CONFIDENCE_FLOOR = 0.5

_ACTIVE_PRESENCE = PresenceSnapshot(
    state=PresenceState.ACTIVE, confidence=1.0, idle_ms=0, taken_at=0.0
)


def _config() -> WombatConfig:
    return WombatConfig(deepseek_api_key="dummy-not-real-key", deepseek_base_url="https://x.test")


def _initial_artifact() -> Artifact:
    return Artifact(
        kind="drain-tick",
        produced_by="test",
        provenance=Provenance(source="system", confidence=1.0, recorded_at=_FIXED_NOW),
        data={},
    )


def _build_stack(*, model_factory: object) -> tuple[Engine, WombatQueue]:
    """Assemble the REAL drain pathway over ONE real WombatQueue + a REAL cog-worx Engine.

    ``model_factory`` is a ``Callable[[BudgetGuard], Model]`` registered under the ``"deepseek"``
    profile via ``ModelRegistry.register_factory`` (the test seam — no ``ModelSpec``/network).
    """
    assert _DSN is not None
    queue = WombatQueue(_DSN, max_size=10)

    drain_queue_stage = DrainQueueStage(queue, batch_size=1, poll_interval_seconds=5.0)
    gate_stage = GateStage(
        evaluate=functools.partial(
            stub_evaluate,
            urgency_threshold=_URGENCY_THRESHOLD,
            staleness_ceiling_s=_STALENESS_CEILING_S,
            confidence_floor=_CONFIDENCE_FLOOR,
        ),
        presence_provider=lambda: _ACTIVE_PRESENCE,
    )
    review_or_speak_stage = ReviewOrSpeakStage(queue=queue)
    compose_dispatch_router = ComposeDispatchRouter(composer_by_kind={ItemKind.GENERIC: "compose"})
    compose_stage = ComposeStage(config=_config(), template_composer=TemplateComposer())

    graph = build_drain_pathway(
        drain_queue_stage,
        gate_stage,
        review_or_speak_stage,
        compose_dispatch_router,
        compose_stage,
    )

    bundle = cold_boot_bundle()
    bundle.pathways.register(_PATHWAY_ID, graph)

    models = ModelRegistry()
    models.register_factory("deepseek", model_factory)  # type: ignore[arg-type]

    engine = Engine(
        models=models,
        journal=bundle.journal,
        graph_store=bundle.graph_store,
        latent=bundle.latent,
        pathways=bundle.pathways,
        model_profile="deepseek",
        clock=lambda: _FIXED_NOW,
    )
    return engine, queue


@pytest.fixture
def clean_table() -> None:
    """Ensure the schema exists and the table is empty before each test (mirrors test_queue.py)."""
    assert _DSN is not None
    with psycopg.connect(_DSN) as conn:
        ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE wombat_queue")
        conn.commit()


# --- AC3: surface path — one generic surface-destined item drives all the way to composed_output --


async def test_ac3_surface_path_drains_and_journals_composed_output(clean_table: None) -> None:
    success_model = lambda guard: FakeModel(  # noqa: E731
        response=ModelResponse(text="You have a new alert.", model_id="fake", finish_reason="stop")
    )
    engine, queue = _build_stack(model_factory=success_model)
    try:
        queue.enqueue(
            QueueItem(
                idempotency_key="surface-1",
                payload={"item_kind": "generic", "stub_urgency": "high", "subject": "Server alert"},
            )
        )

        final = await engine.run(
            run_id="run-surface",
            session_id="sess-surface",
            pathway_id=_PATHWAY_ID,
            initial=_initial_artifact(),
        )

        assert final.status is RunStatus.COMPLETED
        assert queue.drain() == []  # AC3: the queue is empty (acked)

        compose_steps = [s for s in final.steps if s.stage_name == "compose"]
        assert len(compose_steps) == 1
        composed_artifact = compose_steps[0].result.output
        assert composed_artifact is not None
        assert composed_artifact.kind == COMPOSED_OUTPUT
        text, item_id, item_kind, degraded = composed_output_from_artifact_data(
            composed_artifact.data
        )
        assert text
        assert item_id == "surface-1"
        assert item_kind is ItemKind.GENERIC
        assert degraded is False

        # router Transitioned exactly once
        dispatch_steps = [s for s in final.steps if s.stage_name == "compose_dispatch"]
        assert len(dispatch_steps) == 1
    finally:
        queue.close()


# --- Hold variant: a low-urgency item never reaches compose_dispatch/compose ----------------------


async def test_hold_variant_journals_hold_report_no_composed_output(clean_table: None) -> None:
    never_called_model = lambda guard: FakeModel(  # noqa: E731
        raises=AssertionError("the mouth must never be called on the hold path")
    )
    engine, queue = _build_stack(model_factory=never_called_model)
    try:
        queue.enqueue(
            QueueItem(
                idempotency_key="hold-1",
                payload={"item_kind": "generic", "stub_urgency": "low", "subject": "Quiet update"},
            )
        )

        final = await engine.run(
            run_id="run-hold",
            session_id="sess-hold",
            pathway_id=_PATHWAY_ID,
            initial=_initial_artifact(),
        )

        assert final.status is RunStatus.COMPLETED
        assert queue.drain() == []  # acked on the hold branch too

        ros_steps = [s for s in final.steps if s.stage_name == "review_or_speak"]
        assert len(ros_steps) == 1
        hold_artifact = ros_steps[0].result.output
        assert hold_artifact is not None
        assert hold_artifact.kind == HOLD_REPORT
        assert hold_artifact.data["holds"][0]["item_id"] == "hold-1"

        assert not any(s.stage_name in ("compose_dispatch", "compose") for s in final.steps)
    finally:
        queue.close()


# --- Degrade variant: the mouth raises -> composed_output.degraded is True, no exception ----------


async def test_degrade_variant_composed_output_degraded_true(clean_table: None) -> None:
    raising_model = lambda guard: FakeModel(  # noqa: E731
        raises=ConnectionError("simulated DeepSeek outage")
    )
    engine, queue = _build_stack(model_factory=raising_model)
    try:
        queue.enqueue(
            QueueItem(
                idempotency_key="degrade-1",
                payload={"item_kind": "generic", "stub_urgency": "high", "subject": "Degrade me"},
            )
        )

        final = await engine.run(
            run_id="run-degrade",
            session_id="sess-degrade",
            pathway_id=_PATHWAY_ID,
            initial=_initial_artifact(),
        )

        assert final.status is RunStatus.COMPLETED
        assert queue.drain() == []

        compose_steps = [s for s in final.steps if s.stage_name == "compose"]
        assert len(compose_steps) == 1
        composed_artifact = compose_steps[0].result.output
        assert composed_artifact is not None
        _text, _item_id, _item_kind, degraded = composed_output_from_artifact_data(
            composed_artifact.data
        )
        assert degraded is True  # the keyless-demo proof: never raises, always degrades cleanly
    finally:
        queue.close()


# --- Idle: a drive on the empty queue parks WAITING (Q-53 route-guard rider fix — real pass) ------


async def test_idle_second_drive_on_empty_queue_parks_wait(clean_table: None) -> None:
    unused_model = lambda guard: FakeModel(  # noqa: E731
        raises=AssertionError("the mouth must never be called while the queue is empty")
    )
    engine, queue = _build_stack(model_factory=unused_model)
    try:
        final = await engine.run(
            run_id="run-idle",
            session_id="sess-idle",
            pathway_id=_PATHWAY_ID,
            initial=_initial_artifact(),
        )

        # No StageGraphError: the empty-queue Wait(to="drain_queue") is now an accepted edge and
        # the run parks WAITING on the drain_queue heartbeat (the committed fresh Wait step).
        assert final.status is RunStatus.WAITING
        assert tuple(s.stage_name for s in final.steps) == ("drain_queue",)
    finally:
        queue.close()
