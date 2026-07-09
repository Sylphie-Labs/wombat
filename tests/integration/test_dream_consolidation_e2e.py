"""TK-47 — DreamConsolidationStage end-to-end acceptance criteria (EP-13, DEC-12/DEC-23, Q-90).

In-memory substrate, ZERO network: the oracle is cog-worx's ``TableOracle`` (deterministic,
table-driven) and the model is ``ReplayModel``/``FakeModel`` (scripted, spy). Both are injected
directly through ``DreamConsolidationStage``'s ``reconciler``/``extractor`` ctor seams — no
Postgres, no live DeepSeek endpoint, no ``bootstrap.assemble_runtime``.

  AC1 (drain, ``-k drain``): a dirty/conflicting user-scope claim pair (the reconciler merges
      them) PLUS a populated journal step (the extractor mints a claim from it) -> one real
      ``wombat.dream`` run TERMINATES the consolidation loop (``ticks < MAX_TICKS``), the summary
      artifact reflects the reconciler's own merge semantics, and ``dream_outcome``/``dream_run``
      still execute downstream.
  AC2 (clean night, ``-k clean``): zero dirty subjects + an empty journal -> the loop terminates
      in ONE drained pass (``ticks == 1``), zero claims extracted, zero model calls, and the
      stage logs a zero-change result.
  AC3 (stall, ``-k stall``): a raising fake model stalls ``ClaimExtractor.tick()`` -> the stage
      logs loud and STILL transitions onward; ``dream_outcome``/``dream_run`` run; the run
      COMPLETES.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta

import pytest
from cogworx.claims.provenance import Artifact, Claim, Provenance
from cogworx.coherence.reconciler import CoherenceReconciler
from cogworx.cost.budget import BudgetGuard
from cogworx.knowledge.episodes import Turn
from cogworx.knowledge.evidence import EvidenceEvent, make_evidence
from cogworx.knowledge.identity import claim_id_for
from cogworx.knowledge.scopes import ScopeRegistry
from cogworx.knowledge.source_registry import SourceRegistry
from cogworx.loop.result import Done, StageResult, Transition
from cogworx.loop.stage import StageContext
from cogworx.loop.state import RunStatus
from cogworx.model.base import Model, ModelResponse
from cogworx.model.registry import ModelRegistry
from cogworx.runtime.claim_extractor import ClaimExtractor
from cogworx.runtime.engine import Engine
from cogworx.substrate.journal import Journal, StepRecord
from cogworx.testing.doubles import InMemoryEntityKG
from cogworx.testing.fake_model import ReplayModel
from cogworx.testing.fake_oracle import TableOracle

from tests.support.stage_context_fake import FakeModel
from wombat.params import load_operating_params
from wombat.pathways.dream_pathway import (
    DREAM_PATHWAY_ID,
    MAX_TICKS,
    DreamConsolidationStage,
    DreamOutcomeStage,
    DreamTuneStage,
    build_dream_pathway,
    dream_trigger_artifact,
)
from wombat.rating.rating_tuner import RatingTuner
from wombat.substrate import SubstrateBundle, cold_boot_bundle
from wombat.user_model.observation_writer import ObservationWriter
from wombat.user_model.outcome_labeler import OutcomeLabeler

_NOW = datetime(2026, 7, 9, 3, 0, 0, tzinfo=UTC)
_T0 = _NOW - timedelta(hours=2)
_T1 = _NOW - timedelta(hours=1)
_USER_ID = "dream-test-user"
_SCOPE = f"user:{_USER_ID}"


def _claim(
    subject: str, predicate: str, payload: str, *, scope: str, valid_from: datetime
) -> Claim:
    """Mirrors cog-worx's own ``test_coherence_reconciler.py::_make_claim`` helper."""
    return Claim(
        id=claim_id_for(subject, predicate, payload, scope=scope),
        subject=subject,
        predicate=predicate,
        payload=payload,
        epistemic_type="observation",
        provenance=Provenance(source="system", confidence=0.9, recorded_at=valid_from),
        valid_from=valid_from,
        ingest_time=valid_from,
        created_by="test",
        scope=scope,
    )


def _ev(event_id: str) -> EvidenceEvent:
    return make_evidence(
        type="corroboration",
        polarity="+",
        source_id="test-src",
        source_authority=0.8,
        recorded_at=_T0,
        event_id=event_id,
    )


async def _seed_conversation_step(journal: Journal, *, run_id: str, content: str) -> None:
    """Commit ONE journal step carrying a user turn — the extractor's own input shape
    (``turns_of``/``render_transcript``, cogworx.knowledge.episodes)."""
    await journal.start_run(
        run_id, run_id, pathway_id="seed-pathway", pathway_version=1, pathway_fingerprint="seed-fp"
    )
    turn = Turn(role="user", content=content, kind="conversation")
    step = StepRecord(
        run_id=run_id,
        step_index=0,
        stage_name="seed_stage",
        result=Done(
            output=Artifact(
                kind="seed.turn",
                produced_by="seed_stage",
                provenance=Provenance(source="system", confidence=1.0, recorded_at=_T0),
                data={"turns": [turn.model_dump()]},
            )
        ),
        committed_at=_T0,
    )
    await journal.commit_step(step)


def _outcome_stage(entity_kg: InMemoryEntityKG) -> DreamOutcomeStage:
    return DreamOutcomeStage(
        entity_kg=entity_kg,
        labeler=OutcomeLabeler(
            writer=ObservationWriter(
                entity_kg=entity_kg, scope_registry=ScopeRegistry(), user_id=_USER_ID
            )
        ),
        user_id=_USER_ID,
    )


def _tune_stage(entity_kg: InMemoryEntityKG) -> DreamTuneStage:
    """TK-49 mechanical reshape (flagged per the ticket's own sanction): ``build_dream_pathway``
    now requires a ``tune`` stage too — this suite's own AC1-AC3 witnesses are all about
    ``DreamConsolidationStage``, so a trivially-constructed real ``DreamTuneStage`` (over a
    throwaway ``RatingTuner`` on the SAME ``entity_kg``) merely satisfies the shape without
    asserting anything about it."""
    writer = ObservationWriter(
        entity_kg=entity_kg, scope_registry=ScopeRegistry(), user_id=_USER_ID
    )
    tuner = RatingTuner(
        entity_kg=entity_kg,
        writer=writer,
        params=load_operating_params(),
        user_id=_USER_ID,
        clock=lambda: _NOW,
    )
    return DreamTuneStage(tuner=tuner)


class _PassthroughBehaviorLogStage:
    """TK-111 mechanical reshape (flagged per the ticket's own sanction, Q-98):
    ``build_dream_pathway`` now also requires a ``behavior_log`` stage too — this suite's own
    AC1-AC3 witnesses are all about ``DreamConsolidationStage``, so a trivial always-
    transitions-onward double merely satisfies the shape without asserting anything about it (a
    real ``DreamBehaviorLogStage`` needs a Postgres-backed ``BehaviorEventLog`` this module has
    no DSN for)."""

    name: str = "dream_behavior_log"
    transitions: tuple[str, ...] = ("dream_window",)

    async def run(self, ctx: StageContext) -> StageResult:
        return Transition(
            to="dream_window",
            output=Artifact(
                kind="wombat.dream_behavior_log_report",
                produced_by=self.name,
                provenance=Provenance(source="system", confidence=1.0, recorded_at=ctx.clock()),
                data={},
            ),
        )


class _PassthroughWindowStage:
    """TK-112 mechanical reshape (flagged per the ticket's own sanction, Q-99e):
    ``build_dream_pathway`` now also requires a ``window`` stage too — this suite's own AC1-AC3
    witnesses are all about ``DreamConsolidationStage``, so a trivial always-transitions-onward
    double merely satisfies the shape without asserting anything about it (a real
    ``WriteWindowSummariesStage`` needs a Postgres-backed ``BehaviorEventLog`` this module has no
    DSN for)."""

    name: str = "dream_window"
    transitions: tuple[str, ...] = ("dream_run",)

    async def run(self, ctx: StageContext) -> StageResult:
        return Transition(
            to="dream_run",
            output=Artifact(
                kind="wombat.dream_window_report",
                produced_by=self.name,
                provenance=Provenance(source="system", confidence=1.0, recorded_at=ctx.clock()),
                data={},
            ),
        )


def _never_called_model(guard: BudgetGuard) -> Model:
    """The Engine's own ``ctx.model`` factory — NEVER actually invoked, because neither
    ``DreamConsolidationStage`` nor ``DreamOutcomeStage``/``DreamScaffoldStage`` calls
    ``ctx.model`` (the reconciler/extractor's model is injected directly through their own ctor
    seams, TK-54). A raising ``FakeModel`` proves that structurally."""
    return FakeModel(raises=AssertionError("ctx.model must never be called by dream stages"))


def _engine(bundle: SubstrateBundle) -> Engine:
    """A real ``Engine`` over a ``cold_boot_bundle()`` — mirrors ``test_dream_pathway_e2e.py``'s
    own hand-built-Engine harness (AC2's ``_build_stack_with_raising_dream``)."""
    models = ModelRegistry()
    models.register_factory("unused", _never_called_model)
    return Engine(
        models=models,
        journal=bundle.journal,
        graph_store=bundle.graph_store,
        latent=bundle.latent,
        pathways=bundle.pathways,
        model_profile="unused",
        clock=lambda: _NOW,
    )


async def _run_dream(bundle: SubstrateBundle, *, run_id: str) -> RunStatus:
    engine = _engine(bundle)
    final = await engine.run(
        run_id=run_id,
        session_id=run_id,
        pathway_id=DREAM_PATHWAY_ID,
        initial=dream_trigger_artifact(_NOW),
    )
    return final.status


# --- AC1: drain with work -----------------------------------------------------------------


async def test_ac1_drain_with_work_reflects_reconciler_merges_and_terminates() -> None:
    entity_kg = InMemoryEntityKG()

    # A dirty/conflicting user-scope claim pair (TC-2's own planted-conflict recipe): same
    # subject+predicate, overlapping validity, different payloads -> the reconciler defeats one.
    claim_a = _claim("Bob", "salary", "100k", scope=_SCOPE, valid_from=_T0)
    claim_b = _claim("Bob", "salary", "80k", scope=_SCOPE, valid_from=_T1)
    await entity_kg.write_claim(claim_a, evidence=_ev("ev-a"))
    await entity_kg.write_claim(claim_b, evidence=_ev("ev-b"))

    oracle = TableOracle([frozenset({claim_a.id, claim_b.id})])
    reconciler = CoherenceReconciler(entity_kg=entity_kg, store=entity_kg, oracle=oracle)

    bundle = cold_boot_bundle()
    await _seed_conversation_step(bundle.journal, run_id="seed-run-ac1", content="I love pizza")

    extraction_response = ModelResponse(
        text=json.dumps(
            {
                "claims": [
                    {
                        "subject": "user",
                        "predicate": "likes",
                        "object": "pizza",
                        "supporting_turn_index": 0,
                    }
                ]
            }
        ),
        model_id="replay",
        finish_reason="stop",
    )
    extraction_model = ReplayModel([extraction_response])
    extractor = ClaimExtractor(
        journal=bundle.journal,
        entity_kg=entity_kg,
        model=extraction_model,
        source_registry=SourceRegistry(),
    )

    consolidate_stage = DreamConsolidationStage(reconciler=reconciler, extractor=extractor)
    dream_graph = build_dream_pathway(
        consolidate_stage,
        _outcome_stage(entity_kg),
        _tune_stage(entity_kg),
        _PassthroughBehaviorLogStage(),
        _PassthroughWindowStage(),
    )
    bundle.pathways.register(DREAM_PATHWAY_ID, dream_graph)

    run_id = "run-dream-ac1"
    status = await _run_dream(bundle, run_id=run_id)
    assert status is RunStatus.COMPLETED

    run_state = await bundle.journal.load_run(run_id)
    assert run_state is not None
    stage_names = [s.stage_name for s in run_state.steps]
    assert "dream_consolidate" in stage_names
    assert "dream_outcome" in stage_names
    assert "dream_tune" in stage_names
    assert "dream_run" in stage_names

    consolidate_step = next(s for s in run_state.steps if s.stage_name == "dream_consolidate")
    assert isinstance(consolidate_step.result, Transition)
    data = consolidate_step.result.output.data

    assert data["ticks"] < MAX_TICKS
    assert data["stalled"] is False
    assert data["claims_extracted"] == 1
    # The reconciler's own merge semantics (TC-2): the older claim is defeated.
    assert data["subjects_defeated"] == 1
    assert data["subjects_processed"] >= 1

    loser = await entity_kg.get_claim(claim_a.id)
    assert loser is not None
    assert loser.status == "defeasibly-defeated"

    # The extracted claim was actually written to the shared KG.
    extracted_id = claim_id_for("user", "likes", "pizza")
    extracted = await entity_kg.get_claim(extracted_id)
    assert extracted is not None


# --- AC2: clean night -----------------------------------------------------------------------


async def test_ac2_clean_night_terminates_in_one_pass_with_zero_model_calls(
    caplog: pytest.LogCaptureFixture,
) -> None:
    entity_kg = InMemoryEntityKG()
    reconciler = CoherenceReconciler(entity_kg=entity_kg, store=entity_kg, oracle=TableOracle([]))

    bundle = cold_boot_bundle()
    # No committed steps — a genuinely empty journal.

    extraction_model = ReplayModel([])  # never scripted a response -> proves zero calls
    extractor = ClaimExtractor(
        journal=bundle.journal,
        entity_kg=entity_kg,
        model=extraction_model,
        source_registry=SourceRegistry(),
    )

    consolidate_stage = DreamConsolidationStage(reconciler=reconciler, extractor=extractor)
    dream_graph = build_dream_pathway(
        consolidate_stage,
        _outcome_stage(entity_kg),
        _tune_stage(entity_kg),
        _PassthroughBehaviorLogStage(),
        _PassthroughWindowStage(),
    )
    bundle.pathways.register(DREAM_PATHWAY_ID, dream_graph)

    run_id = "run-dream-ac2"
    with caplog.at_level(logging.INFO, logger="wombat.pathways.dream_pathway"):
        status = await _run_dream(bundle, run_id=run_id)
    assert status is RunStatus.COMPLETED

    run_state = await bundle.journal.load_run(run_id)
    assert run_state is not None
    consolidate_step = next(s for s in run_state.steps if s.stage_name == "dream_consolidate")
    assert isinstance(consolidate_step.result, Transition)
    data = consolidate_step.result.output.data

    assert data["ticks"] == 1
    assert data["stalled"] is False
    assert data["claims_extracted"] == 0
    assert data["subjects_processed"] == 0
    assert data["subjects_defeated"] == 0
    assert extraction_model.call_count == 0

    # The stage logs a zero-change result.
    assert any(
        "drained in 1 tick" in record.message and "claims_extracted=0" in record.message
        for record in caplog.records
    )


# --- AC3: stall posture -------------------------------------------------------------------


async def test_ac3_extractor_stall_still_transitions_and_run_completes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    entity_kg = InMemoryEntityKG()
    reconciler = CoherenceReconciler(entity_kg=entity_kg, store=entity_kg, oracle=TableOracle([]))

    bundle = cold_boot_bundle()
    await _seed_conversation_step(bundle.journal, run_id="seed-run-ac3", content="hello there")

    raising_model = FakeModel(raises=RuntimeError("simulated extraction model failure — AC3"))
    extractor = ClaimExtractor(
        journal=bundle.journal,
        entity_kg=entity_kg,
        model=raising_model,
        source_registry=SourceRegistry(),
    )

    consolidate_stage = DreamConsolidationStage(reconciler=reconciler, extractor=extractor)
    dream_graph = build_dream_pathway(
        consolidate_stage,
        _outcome_stage(entity_kg),
        _tune_stage(entity_kg),
        _PassthroughBehaviorLogStage(),
        _PassthroughWindowStage(),
    )
    bundle.pathways.register(DREAM_PATHWAY_ID, dream_graph)

    run_id = "run-dream-ac3"
    with caplog.at_level(logging.ERROR, logger="wombat.pathways.dream_pathway"):
        status = await _run_dream(bundle, run_id=run_id)
    assert status is RunStatus.COMPLETED

    run_state = await bundle.journal.load_run(run_id)
    assert run_state is not None
    stage_names = [s.stage_name for s in run_state.steps]
    assert "dream_consolidate" in stage_names
    assert "dream_outcome" in stage_names
    assert "dream_tune" in stage_names
    assert "dream_run" in stage_names

    consolidate_step = next(s for s in run_state.steps if s.stage_name == "dream_consolidate")
    assert isinstance(consolidate_step.result, Transition)
    data = consolidate_step.result.output.data

    assert data["stalled"] is True
    assert data["ticks"] == 1
    assert data["claims_extracted"] == 0

    assert any(
        record.levelno == logging.ERROR and "STALLED" in record.message
        for record in caplog.records
    )
