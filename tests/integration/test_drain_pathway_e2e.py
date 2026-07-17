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

IDLE SCENARIO (TK-230, DEC-41, superseding the old Q-53 self-park rider): a second drive on an
empty queue no longer parks the run on a ``Wait`` at all. ``DrainQueueStage`` (TK-5) declares only
``transitions = ("gate",)``; its empty-queue path returns ``Done`` carrying a ``DRAIN_HEARTBEAT``
artifact, so the run COMPLETES — proven by the real (un-stubbed) idle test below.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import psycopg
import pytest
from cogworx.claims.provenance import Artifact, Provenance
from cogworx.loop.state import RunStatus
from cogworx.model.base import ModelResponse
from cogworx.model.registry import ModelRegistry
from cogworx.runtime.engine import Engine
from cogworx.testing.doubles import InMemoryEntityKG

from tests.support.stage_context_fake import FakeModel
from wombat.compose.templates import TemplateComposer
from wombat.config import WombatConfig
from wombat.domain.daily_ledger import DailyLedger
from wombat.domain.daily_ledger import ensure_schema as ensure_daily_ledger_schema
from wombat.gate.ceiling import CeilingLedger
from wombat.gate.decay import LedgerReset
from wombat.gate.models import ItemKind
from wombat.gate.pending_set import InMemoryPendingJournal, PendingSet
from wombat.gate.pipeline import Gate
from wombat.pathways.drain_pathway import build_drain_pathway
from wombat.queue import QueueItem, WombatQueue, ensure_schema
from wombat.sinks.speak import SpeakSink
from wombat.sources.presence import PresenceSnapshot, PresenceState
from wombat.stages.artifacts import (
    COMPOSED_OUTPUT,
    DRAIN_HEARTBEAT,
    HOLD_REPORT,
    composed_output_from_artifact_data,
)
from wombat.stages.chat_reply import ChatReplyStage
from wombat.stages.compose import ComposeStage
from wombat.stages.compose_dispatch_router import ComposeDispatchRouter
from wombat.stages.drain_queue import DrainQueueStage
from wombat.stages.gate_stage import GateStage, make_gate_evaluator, make_stub_evaluator
from wombat.stages.review_or_speak import ReviewOrSpeakStage
from wombat.substrate import cold_boot_bundle
from wombat.user_model.user_model import UserModel

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

# The REAL Gate variant (Q-55) uses the audited wombat_params.yaml urgency_threshold (0.75) —
# under the default GENERIC RatingParams (urgency_base=0.5) an untimed/automated item never
# clears this bar (raw_urgency tops out well under 1.0) while a timed VIP item does.
_REAL_URGENCY_THRESHOLD = 0.75

_ACTIVE_PRESENCE = PresenceSnapshot(
    state=PresenceState.ACTIVE, confidence=1.0, idle_ms=0, taken_at=0.0
)

# The real-gate variant's presence must NOT be stale relative to the REAL clock the production
# Gate's presence_hold check compares against (make_gate_evaluator calls presence_hold(presence,
# clock(), ...) with the genuine `now` — unlike the stub, which passes the snapshot's OWN
# taken_at as `now`, making staleness inert by construction). taken_at must sit at _FIXED_NOW.
_REAL_ACTIVE_PRESENCE = PresenceSnapshot(
    state=PresenceState.ACTIVE, confidence=1.0, idle_ms=0, taken_at=_FIXED_NOW.timestamp()
)


class _NoOpRollover:
    """A ``DayRolloverProtocol`` double that never fires (TK-28, Q-73) — this e2e module proves
    the surface/hold/degrade/idle drain spine, not decay/rollover."""

    def check(self) -> LedgerReset | None:
        return None


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
        evaluate=make_stub_evaluator(
            urgency_threshold=_URGENCY_THRESHOLD,
            staleness_ceiling_s=_STALENESS_CEILING_S,
            confidence_floor=_CONFIDENCE_FLOOR,
        ),
        presence_provider=lambda: _ACTIVE_PRESENCE,
    )
    review_or_speak_stage = ReviewOrSpeakStage(queue=queue)
    compose_dispatch_router = ComposeDispatchRouter(composer_by_kind={ItemKind.GENERIC: "compose"})
    compose_stage = ComposeStage(config=_config(), template_composer=TemplateComposer())
    # TK-164 (Q-96): compose transitions onward to "chat_reply" (TK-222) — voice-off here, this
    # module isn't testing voice, only that the real Engine drives the drain graph to its new
    # terminal. chat_reply is wired with broker=None (chat-disabled shape, pure pass-through) —
    # this module isn't testing chat either.
    chat_reply_stage = ChatReplyStage(broker=None)
    speak_stage = SpeakSink(voice_enabled=False, adapter=None)

    graph = build_drain_pathway(
        drain_queue_stage,
        gate_stage,
        review_or_speak_stage,
        compose_dispatch_router,
        compose_stage,
        chat_reply_stage,
        speak_stage,
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


def _build_real_gate_stack(*, model_factory: object) -> tuple[Engine, WombatQueue, DailyLedger]:
    """Assemble the drain pathway with the REAL production ``Gate`` (TK-27) wired in via
    ``make_gate_evaluator`` (Q-55) — mirrors ``_build_stack`` exactly except for the gate itself:

    * ``UserModel`` over a FRESH ``InMemoryEntityKG`` (no seeded claims -> every event class reads
      its documented defaults, ``rating/params.py``).
    * ``PendingSet`` over a fresh ``InMemoryPendingJournal`` (in-memory custody — TK-29 is real
      pg durability, out of scope here).
    * ``CeilingLedger`` over a REAL ``DailyLedger`` on the SAME docker Postgres as the queue (the
      one piece of the real Gate that genuinely needs Postgres).

    Returns the ``DailyLedger`` too so the caller can ``close()`` its own lazily-opened connection.
    """
    assert _DSN is not None
    queue = WombatQueue(_DSN, max_size=10)

    user_model = UserModel(entity_kg=InMemoryEntityKG(), user_id="demo-user")
    pending_set = PendingSet(journal=InMemoryPendingJournal(), max_pending=100)
    daily_ledger = DailyLedger(_DSN, tz=ZoneInfo("UTC"), clock=lambda: _FIXED_NOW)
    ceiling = CeilingLedger(daily_ledger=daily_ledger, per_class_daily_ceiling=3)
    gate = Gate(
        user_model=user_model,
        pending_set=pending_set,
        ceiling=ceiling,
        urgency_threshold=_REAL_URGENCY_THRESHOLD,
        load_flush_threshold=10.0,  # high enough that one held item never trips the flush arm
        flush_min_age_seconds=300.0,
        decay_ttl_seconds=float("inf"),
        day_rollover=_NoOpRollover(),
        clock=lambda: _FIXED_NOW.timestamp(),
    )

    drain_queue_stage = DrainQueueStage(queue, batch_size=1, poll_interval_seconds=5.0)
    gate_stage = GateStage(
        evaluate=make_gate_evaluator(
            gate=gate,
            staleness_ceiling_s=_STALENESS_CEILING_S,
            confidence_floor=_CONFIDENCE_FLOOR,
            clock=lambda: _FIXED_NOW.timestamp(),
        ),
        presence_provider=lambda: _REAL_ACTIVE_PRESENCE,
    )
    review_or_speak_stage = ReviewOrSpeakStage(queue=queue)
    compose_dispatch_router = ComposeDispatchRouter(composer_by_kind={ItemKind.GENERIC: "compose"})
    compose_stage = ComposeStage(config=_config(), template_composer=TemplateComposer())
    # TK-164 (Q-96): compose transitions onward to "chat_reply" (TK-222) — voice-off here (see
    # _build_stack). chat_reply is wired with broker=None (chat-disabled shape).
    chat_reply_stage = ChatReplyStage(broker=None)
    speak_stage = SpeakSink(voice_enabled=False, adapter=None)

    graph = build_drain_pathway(
        drain_queue_stage,
        gate_stage,
        review_or_speak_stage,
        compose_dispatch_router,
        compose_stage,
        chat_reply_stage,
        speak_stage,
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
    return engine, queue, daily_ledger


@pytest.fixture
def clean_table_and_ledger() -> None:
    """Like ``clean_table`` but also resets ``daily_ledger`` (the real ``CeilingLedger``'s table)
    so a prior test's per-class daily count can never leak into this one's ceiling check."""
    assert _DSN is not None
    with psycopg.connect(_DSN) as conn:
        ensure_schema(conn)
        ensure_daily_ledger_schema(conn)
        with conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE wombat_queue")
            cur.execute("TRUNCATE TABLE daily_ledger")
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


# --- Idle: a drive on the empty queue completes Done (DEC-41 bounded episode) --------------------


async def test_idle_second_drive_on_empty_queue_completes_done_pg(clean_table: None) -> None:
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

        # TK-230/DEC-41: the empty-queue drive is NOT a self-park any more — DrainQueueStage
        # returns Done carrying a DRAIN_HEARTBEAT artifact, and the run COMPLETES.
        assert final.status is RunStatus.COMPLETED
        assert tuple(s.stage_name for s in final.steps) == ("drain_queue",)
        assert final.steps[0].result.output is not None
        assert final.steps[0].result.output.kind == DRAIN_HEARTBEAT
    finally:
        queue.close()


# --- REAL-gate variant (Q-55): the production Gate wired via make_gate_evaluator, not the stub ----


async def test_real_gate_surfaces_a_high_urgency_item_and_composes(
    clean_table_and_ledger: None,
) -> None:
    """A timed, VIP-sender item clears the real Gate's urgency bar -> SURFACE_IMMEDIATE, under
    ceiling, all the way to a composed_output — no stub anywhere on this path."""
    success_model = lambda guard: FakeModel(  # noqa: E731
        response=ModelResponse(
            text="VIP meeting starting now.", model_id="fake", finish_reason="stop"
        )
    )
    engine, queue, daily_ledger = _build_real_gate_stack(model_factory=success_model)
    try:
        queue.enqueue(
            QueueItem(
                idempotency_key="real-surface-1",
                payload={
                    "item_kind": "generic",
                    "subject": "VIP meeting starting now",
                    "is_timed": True,
                    "seconds_to_event": 0.0,
                    "sender_class": "vip",
                },
            )
        )

        final = await engine.run(
            run_id="run-real-surface",
            session_id="sess-real-surface",
            pathway_id=_PATHWAY_ID,
            initial=_initial_artifact(),
        )

        assert final.status is RunStatus.COMPLETED
        assert queue.drain() == []  # acked

        compose_steps = [s for s in final.steps if s.stage_name == "compose"]
        assert len(compose_steps) == 1
        composed_artifact = compose_steps[0].result.output
        assert composed_artifact is not None
        text, _item_id, item_kind, degraded = composed_output_from_artifact_data(
            composed_artifact.data
        )
        assert text
        assert item_kind is ItemKind.GENERIC
        assert degraded is False
    finally:
        queue.close()
        daily_ledger.close()


async def test_real_gate_holds_a_low_urgency_item_and_accumulates_in_pending(
    clean_table_and_ledger: None,
) -> None:
    """An untimed, automated-sender item never clears the real Gate's urgency bar -> HOLD, and
    accumulates into the durable pending set; the mouth is never called."""
    never_called_model = lambda guard: FakeModel(  # noqa: E731
        raises=AssertionError("the mouth must never be called on the real-gate hold path")
    )
    engine, queue, daily_ledger = _build_real_gate_stack(model_factory=never_called_model)
    try:
        queue.enqueue(
            QueueItem(
                idempotency_key="real-hold-1",
                payload={
                    "item_kind": "generic",
                    "subject": "Automated newsletter",
                    "is_timed": False,
                    "sender_class": "automated",
                },
            )
        )

        final = await engine.run(
            run_id="run-real-hold",
            session_id="sess-real-hold",
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
        holds = hold_artifact.data["holds"]
        assert holds[0]["item_id"] == "real-hold-1"
        # Q-55: the production Gate's HOLD carries no ScoredItem at all (the score lives only in
        # the durable pending set) — review_or_speak's fallback hold record is honest about that
        # rather than fabricating a score.
        assert holds[0]["urgency"] is None
        assert holds[0]["load"] is None

        assert not any(s.stage_name in ("compose_dispatch", "compose") for s in final.steps)
    finally:
        queue.close()
        daily_ledger.close()
