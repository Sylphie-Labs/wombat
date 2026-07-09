"""TK-46 — the wombat.dream off-path scaffold, end-to-end off-path isolation proof (EP-13, Q-85).

ALL tests in this module require a real Postgres and are gated on ``WOMBAT_TEST_PG_DSN`` (the SAME
convention as ``tests/integration/test_drain_pathway_e2e.py`` / ``tests/integration/test_serve_boot.
py``): absent it, the whole module is skipped LOUDLY at collection time.

    docker run --rm -d -p 5511:5432 -e POSTGRES_PASSWORD=wombat postgres:16
    WOMBAT_TEST_PG_DSN=postgresql://postgres:wombat@localhost:5511/postgres

  AC1(b) a REAL cog-worx Engine drives ``wombat.dream`` with a fresh run_id -> the run reaches
      COMPLETED, journaled as its OWN run (Q-85: the DSN-gated variant reuses the
      ``bootstrap.assemble_runtime`` harness, mirroring ``test_serve_boot.py``); a drain drive
      fired AFTER it completes cleanly (the drain heartbeat continues — off-path isolation, the
      positive half).
  AC2 ``build_dream_pathway`` with an INJECTED raising stage, registered directly (mirrors
      ``test_brief_pathway_e2e.py``'s own hand-built-Engine harness, since ``assemble_runtime``
      has no seam to inject a raising dream stage) -> the dream run fails/errors in its own
      journaled run (S9 fail-loud: an un-classified exception propagates uncaught, never a
      silent FAILED); a SUBSEQUENT drain drive on the SAME engine is clean and unaffected — no
      shared-state corruption between dream and drain.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import psycopg
import pytest
from cogworx.claims.provenance import Artifact, Provenance
from cogworx.knowledge.scopes import ScopeRegistry
from cogworx.loop.result import StageResult
from cogworx.loop.stage import StageContext
from cogworx.loop.state import RunStatus
from cogworx.model.registry import ModelRegistry
from cogworx.runtime.engine import Engine
from cogworx.substrate.journal import Journal
from cogworx.testing.doubles import InMemoryEntityKG

from tests.support.stage_context_fake import FakeModel
from wombat import bootstrap
from wombat.behavior.event_log import BehaviorEventLog
from wombat.behavior.event_log import ensure_schema as ensure_behavior_event_log_schema
from wombat.behavior.stages.write_window_summaries import WriteWindowSummariesStage
from wombat.compose.templates import TemplateComposer
from wombat.config import WombatConfig
from wombat.domain.daily_ledger import ensure_schema as ensure_daily_ledger_schema
from wombat.gate.models import ItemKind
from wombat.gate.pending_journal_pg import ensure_schema as ensure_pending_journal_schema
from wombat.params import load_operating_params
from wombat.pathways.drain_pathway import build_drain_pathway
from wombat.pathways.dream_pathway import (
    DREAM_PATHWAY_ID,
    DreamBehaviorLogStage,
    DreamOutcomeStage,
    DreamTuneStage,
    build_dream_pathway,
    dream_trigger_artifact,
)
from wombat.queue import WombatQueue
from wombat.queue import ensure_schema as ensure_queue_schema
from wombat.rating.rating_tuner import RatingTuner
from wombat.sinks.speak import SpeakSink
from wombat.stages.compose import ComposeStage
from wombat.stages.compose_dispatch_router import ComposeDispatchRouter
from wombat.stages.drain_queue import DrainQueueStage
from wombat.stages.gate_stage import GateStage, make_stub_evaluator
from wombat.stages.review_or_speak import ReviewOrSpeakStage
from wombat.substrate import cold_boot_bundle
from wombat.user_model.observation_writer import ObservationWriter
from wombat.user_model.outcome_labeler import OutcomeLabeler

_DSN = os.environ.get("WOMBAT_TEST_PG_DSN")

if not _DSN:
    pytest.skip(
        "WOMBAT_TEST_PG_DSN is not set — skipping the TK-46 dream-pathway e2e off-path isolation "
        "proof, which requires a real throwaway Postgres. Start one with:\n"
        "  docker run --rm -d -p 5511:5432 -e POSTGRES_PASSWORD=wombat postgres:16\n"
        "then export WOMBAT_TEST_PG_DSN=postgresql://postgres:wombat@localhost:5511/postgres",
        allow_module_level=True,
    )

_FIXED_NOW = datetime(2026, 7, 9, 12, 0, 0, tzinfo=UTC)
_LOCAL_DRAIN_PATHWAY_ID = "drain"
_URGENCY_THRESHOLD = 0.5
_STALENESS_CEILING_S = 300.0
_CONFIDENCE_FLOOR = 0.5


def _config() -> WombatConfig:
    return WombatConfig(deepseek_api_key="dummy-not-real-key", deepseek_base_url="https://x.test")


def _initial_drain_artifact() -> Artifact:
    return Artifact(
        kind="drain-tick",
        produced_by="test",
        provenance=Provenance(source="system", confidence=1.0, recorded_at=_FIXED_NOW),
        data={},
    )


@pytest.fixture(autouse=True)
def _reset_singleton() -> None:
    bootstrap.reset_engine()


@pytest.fixture
def clean_tables() -> None:
    """Ensure every schema this composition touches exists, then truncate (mirrors
    ``test_serve_boot.py``'s own ``clean_tables`` convention)."""
    assert _DSN is not None
    with psycopg.connect(_DSN) as conn:
        ensure_queue_schema(conn)
        ensure_daily_ledger_schema(conn)
        ensure_pending_journal_schema(conn)
        ensure_behavior_event_log_schema(conn)
        with conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE wombat_queue")
            cur.execute("TRUNCATE TABLE daily_ledger")
            cur.execute("TRUNCATE TABLE pending_journal")
            cur.execute("TRUNCATE TABLE wombat_behavior_events")
        conn.commit()


# --- AC1(b): a real assemble_runtime-composed Engine, dream COMPLETED, drain drive still clean ----


async def test_ac1_dream_run_completes_and_a_subsequent_drain_drive_stays_clean(
    clean_tables: None,
) -> None:
    assert _DSN is not None
    op = load_operating_params()
    bundle = bootstrap.assemble_runtime(config=_config(), dsn=_DSN, params=op)
    try:
        dream_run_id = "run-dream-ac1"
        dream_final = await bundle.engine.run(
            run_id=dream_run_id,
            session_id=dream_run_id,
            pathway_id=bundle.dream_pathway_id,
            initial=dream_trigger_artifact(_FIXED_NOW),
        )
        assert dream_final.status is RunStatus.COMPLETED

        # Journaled as its OWN run, distinct from any drain run.
        dream_state = await bundle.journal.load_run(dream_run_id)
        assert dream_state is not None
        assert dream_state.pathway_id == bundle.dream_pathway_id

        # TK-112 (Q-99e): the graph AC — the run walked all six stages, in order, ending COMPLETED.
        assert [step.stage_name for step in dream_state.steps] == [
            "dream_consolidate",
            "dream_outcome",
            "dream_tune",
            "dream_behavior_log",
            "dream_window",
            "dream_run",
        ]

        # A drain drive fired AFTER the dream run completes is clean and unaffected — the
        # heartbeat continues (an empty queue self-parks WAITING, mirrors the drain e2e's own
        # idle scenario).
        drain_run_id = "run-drain-after-dream-ac1"
        drain_final = await bundle.engine.run(
            run_id=drain_run_id,
            session_id=drain_run_id,
            pathway_id=bundle.drain_pathway_id,
            initial=_initial_drain_artifact(),
        )
        assert drain_final.status is RunStatus.WAITING
    finally:
        bundle.queue.close()
        bundle.daily_ledger.close()
        bundle.pending_journal.close()
        bundle.behavior_event_log.close()


# --- AC2: an injected raising dream stage errors in its OWN run; drain stays clean afterward ------


class _RaisingDreamStage:
    """A ``Stage`` double whose ``run()`` ALWAYS raises — the AC2 injection seam.

    Substituted as ``build_dream_pathway``'s ``consolidate`` (entry) arg (TK-47 reshape,
    mechanical update — flagged per the ticket's own sanction): declaring
    ``transitions=("dream_outcome",)`` keeps the three-stage graph's static shape fully connected
    (``StageGraph`` now validates every stage is reachable from the entry) even though ``run()``
    always raises before ever returning a ``Transition`` that would take it. The real ``outcome``
    stage passed alongside it is never ACTUALLY reached at runtime (the entry always raises
    first), so a trivially-constructed ``DreamOutcomeStage`` over a throwaway in-memory KG
    satisfies the now-required ``outcome`` arg without asserting anything about it.

    An un-classified exception (not in ``DEFAULT_RETRY_POLICY.retryable``, not a ``TimeoutError``)
    propagates loud out of ``Engine.run`` (cog-worx S9: uncaught is a bug, never a silent FAILED),
    so the dream run's journaled status stays ``RUNNING`` — it is proof the error stayed inside the
    dream run's own journal record rather than corrupting shared state the drain pathway reads.
    """

    name: str = "dream_run_raising"
    transitions: tuple[str, ...] = ("dream_outcome",)
    # TK-49 reshape note: this stage stands in as the graph's ENTRY (``consolidate``) and always
    # raises before ever reaching ``dream_outcome``/``dream_tune`` — the two real stages built
    # alongside it in ``_build_stack_with_raising_dream`` merely satisfy ``build_dream_pathway``'s
    # now-three-required-stages shape.

    async def run(self, ctx: StageContext) -> StageResult:
        raise RuntimeError("simulated dream failure — injected for AC2 isolation proof")


def _build_stack_with_raising_dream(
    *, model_factory: object
) -> tuple[Engine, WombatQueue, Journal]:
    """Assemble a REAL drain pathway (id ``_LOCAL_DRAIN_PATHWAY_ID``) ALONGSIDE ``wombat.dream``
    wired via ``build_dream_pathway`` over the AC2 injected raising stage — on ONE
    ``PathwayRegistry``/``Engine``, exactly like ``test_drain_pathway_e2e.py``'s own ``_build_
    stack``. ``assemble_runtime`` has no seam to inject a raising dream stage, so this hand-rolls
    the registration directly (mirrors ``test_brief_pathway_e2e.py``'s own hand-built-Engine
    posture) rather than forking a second composition path.
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
        presence_provider=lambda: None,  # never read — this module drives the idle path only
    )
    review_or_speak_stage = ReviewOrSpeakStage(queue=queue)
    compose_dispatch_router = ComposeDispatchRouter(composer_by_kind={ItemKind.GENERIC: "compose"})
    compose_stage = ComposeStage(config=_config(), template_composer=TemplateComposer())
    # TK-164 (Q-96): compose now transitions onward to "speak" — voice-off (this module isn't
    # testing voice, only dream/drain off-path isolation).
    speak_stage = SpeakSink(voice_enabled=False, adapter=None)

    drain_graph = build_drain_pathway(
        drain_queue_stage,
        gate_stage,
        review_or_speak_stage,
        compose_dispatch_router,
        compose_stage,
        speak_stage,
    )

    # Never reached (the entry always raises first) — throwaway stub outcome/tune/behavior_log/
    # window stages merely satisfy build_dream_pathway's now-required args (TK-47/TK-49/TK-111/
    # TK-112 reshape).
    stub_entity_kg = InMemoryEntityKG()
    stub_writer = ObservationWriter(
        entity_kg=stub_entity_kg, scope_registry=ScopeRegistry(), user_id="test-user"
    )
    stub_outcome_stage = DreamOutcomeStage(
        entity_kg=stub_entity_kg,
        labeler=OutcomeLabeler(writer=stub_writer),
        user_id="test-user",
    )
    stub_tune_stage = DreamTuneStage(
        tuner=RatingTuner(
            entity_kg=stub_entity_kg,
            writer=stub_writer,
            params=load_operating_params(),
            user_id="test-user",
            clock=lambda: _FIXED_NOW,
        )
    )
    stub_behavior_log_stage = DreamBehaviorLogStage(
        store=BehaviorEventLog(_DSN), entity_kg=stub_entity_kg, user_id="test-user"
    )
    stub_window_stage = WriteWindowSummariesStage(
        store=BehaviorEventLog(_DSN), writer=stub_writer, tz=ZoneInfo("UTC")
    )
    dream_graph = build_dream_pathway(
        _RaisingDreamStage(),
        stub_outcome_stage,
        stub_tune_stage,
        stub_behavior_log_stage,
        stub_window_stage,
    )

    bundle = cold_boot_bundle()
    bundle.pathways.register(_LOCAL_DRAIN_PATHWAY_ID, drain_graph)
    bundle.pathways.register(DREAM_PATHWAY_ID, dream_graph)

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
    return engine, queue, bundle.journal


@pytest.fixture
def clean_table(clean_tables: None) -> None:
    """AC2 only exercises the idle (empty-queue) drain path, but still truncates via the shared
    ``clean_tables`` fixture so a prior test's rows can never leak into the queue drive."""


async def test_ac2_injected_raising_dream_stage_isolated_from_subsequent_drain_drive(
    clean_table: None,
) -> None:
    never_called_model = lambda guard: FakeModel(  # noqa: E731
        raises=AssertionError("the mouth must never be called on the idle drain path")
    )
    engine, queue, journal = _build_stack_with_raising_dream(model_factory=never_called_model)
    try:
        dream_run_id = "run-dream-ac2-fail"
        with pytest.raises(RuntimeError, match="simulated dream failure"):
            await engine.run(
                run_id=dream_run_id,
                session_id=dream_run_id,
                pathway_id=DREAM_PATHWAY_ID,
                initial=dream_trigger_artifact(_FIXED_NOW),
            )

        # Journaled as its OWN run — never advanced past RUNNING (fail-loud propagation, S9: no
        # FAILED is set for an un-classified exception).
        dream_state = await journal.load_run(dream_run_id)
        assert dream_state is not None
        assert dream_state.status is RunStatus.RUNNING
        assert dream_state.pathway_id == DREAM_PATHWAY_ID

        # A SUBSEQUENT drain drive on the SAME engine is clean and unaffected.
        drain_run_id = "run-drain-after-dream-fail"
        drain_final = await engine.run(
            run_id=drain_run_id,
            session_id=drain_run_id,
            pathway_id=_LOCAL_DRAIN_PATHWAY_ID,
            initial=_initial_drain_artifact(),
        )
        assert drain_final.status is RunStatus.WAITING
    finally:
        queue.close()
