"""TK-175 — outcome-loop dream-side wiring acceptance criteria (Q-90 split of TK-175, EP-12).

``DreamOutcomeStage`` is the ``wombat.dream`` graph's SECOND stage, transitioning onward to
``dream_tune`` then the TK-46 terminal scaffold (Q-91 end-state: ``dream_consolidate`` ->
``dream_outcome`` -> ``dream_tune`` -> ``dream_run``). Both ACs drive a REAL cog-worx ``Engine``
(in-memory substrate — no Postgres needed) over ``build_dream_pathway``, mirroring ``tests/
integration/test_dream_pathway_e2e.py``'s hand-built-``Engine`` idiom. TK-47 (mechanical update,
flagged per the ticket's own sanction): ``build_dream_pathway`` now also requires a
``consolidate`` (entry) stage — ``_build_engine`` below wires a zero-work
``DreamConsolidationStage`` (empty KG, empty journal, so its own sweepers never do anything)
purely to satisfy the graph shape; this module's ACs are about the outcome pass, not
consolidation.

TK-49 (mechanical update, flagged per the ticket's own sanction): ``build_dream_pathway`` now
also requires a ``tune`` stage. A REAL ``RatingTuner`` would react to the OUTCOME_* corpus AC1
seeds (it would write its OWN ``rating_params`` claim onto the SAME event-class subjects AC1's
own ``claims_about`` assertions enumerate), so ``_build_engine`` wires a trivial always-
transitions-onward double instead — this module's ACs are about the outcome pass, not tuning
(TK-49 owns its own acceptance criteria in ``tests/unit/test_rating_tuner.py``).

TK-214 (mechanical update, flagged per the ticket's own sanction, EP-35): ``build_dream_pathway``
now also requires a ``persona`` stage, inserted between ``tune`` and ``behavior_log``. A REAL
``DreamPersonaStage`` needs a Postgres-backed ``BehaviorEventLog`` this module has no DSN for, so
``_build_engine`` wires a trivial always-transitions-onward double instead (TK-214 owns its own
acceptance criteria in ``tests/pathways/test_dream_persona_stage.py``).

TK-111 (mechanical update, flagged per the ticket's own sanction, Q-98): ``build_dream_pathway``
now also requires a ``behavior_log`` stage. A REAL ``DreamBehaviorLogStage`` needs a Postgres-
backed ``BehaviorEventLog`` this module has no DSN for, so ``_build_engine`` wires a second
trivial always-transitions-onward double instead (TK-111 owns its own acceptance criteria in
``tests/behavior/test_event_log.py``).

TK-112 (mechanical update, flagged per the ticket's own sanction, Q-99e): ``build_dream_pathway``
now also requires a ``window`` stage. A REAL ``WriteWindowSummariesStage`` needs the SAME
Postgres-backed ``BehaviorEventLog``, so ``_build_engine`` wires a third trivial
always-transitions-onward double instead (TK-112 owns its own acceptance criteria in ``tests/
behavior/stages/test_write_window_summaries.py``).

TK-113 (mechanical update, flagged per the ticket's own sanction, Q-99f): ``build_dream_pathway``
now also requires a ``pattern`` stage. A REAL ``PatternDetectorStage`` needs an injected
``enqueue`` callable and the loaded psychology KB, so ``_build_engine`` wires a fourth trivial
always-transitions-onward double instead (TK-113 owns its own acceptance criteria in ``tests/
behavior/stages/test_pattern_detector.py``).

  AC1 (e2e): seed the shared KG (via ``OutcomeLabeler``) with PENDING claims for two items across
      two event classes, plus a ``BEHAVIOR_OBSERVED`` 'useful' feedback claim for one of them.
      Drive one ``wombat.dream`` run: the run walks ``dream_outcome`` AND ``dream_run``
      (``final.steps``); the feedback item's PENDING claim is invalidated with an active
      ``OUTCOME_LOAD_BEARING`` claim; the no-feedback item got ``OUTCOME_IGNORED``; both are
      enumerable via ``claims_about(event_class.value)`` (the corpus TK-49 reads).
      ``test_ac1_...e2e...``.
  AC2 (empty corpus): no PENDING claims seeded -> the run COMPLETES cleanly, zero claim writes,
      ``dream_run`` is still reached. ``test_ac2_...empty...``.

``asyncio_mode = "auto"`` is configured in pyproject.toml (pytest-asyncio), so async test
functions run directly — no manual ``asyncio.run()`` driving needed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime

from cogworx.claims.provenance import Artifact, Provenance
from cogworx.coherence.reconciler import CoherenceReconciler
from cogworx.knowledge.scopes import ScopeRegistry
from cogworx.knowledge.source_registry import SourceRegistry
from cogworx.loop.result import StageResult, Transition
from cogworx.loop.stage import StageContext
from cogworx.loop.state import RunStatus
from cogworx.model.registry import ModelRegistry
from cogworx.runtime.claim_extractor import ClaimExtractor
from cogworx.runtime.engine import Engine
from cogworx.testing.doubles import InMemoryEntityKG
from cogworx.testing.fake_model import ReplayModel
from cogworx.testing.fake_oracle import TableOracle

from tests.support.stage_context_fake import FakeModel
from wombat.pathways.dream_pathway import (
    DREAM_PATHWAY_ID,
    DreamConsolidationStage,
    DreamOutcomeStage,
    build_dream_pathway,
    dream_trigger_artifact,
)
from wombat.rating.params import EventClass
from wombat.substrate import cold_boot_bundle
from wombat.user_model.claims import Claim, ClaimPredicate
from wombat.user_model.observation_writer import ObservationWriter
from wombat.user_model.outcome_labeler import OutcomeLabeler

_FIXED_NOW = datetime(2026, 7, 9, 12, 0, 0, tzinfo=UTC)
_USER_ID = "alice"
_SCOPE = f"user:{_USER_ID}"


@dataclass
class _PassthroughTuneStage:
    """TK-49 mechanical reshape (flagged per the ticket's own sanction): a trivial
    always-transitions-onward double standing in for ``DreamTuneStage`` — this module's ACs are
    about the outcome pass, never touching the KG here (a real ``RatingTuner`` would react to the
    OUTCOME_* corpus AC1 seeds; see the module docstring)."""

    name: str = "dream_tune"
    transitions: tuple[str, ...] = ("dream_persona",)

    async def run(self, ctx: StageContext) -> StageResult:
        return Transition(
            to="dream_persona",
            output=Artifact(
                kind="wombat.dream_tune_report",
                produced_by=self.name,
                provenance=Provenance(source="system", confidence=1.0, recorded_at=ctx.clock()),
                data={},
            ),
        )


@dataclass
class _PassthroughPersonaStage:
    """TK-214 mechanical reshape (flagged per the ticket's own sanction, EP-35): a trivial
    always-transitions-onward double standing in for ``DreamPersonaStage`` — this module's ACs
    are about the outcome pass, never touching the persona matrix or a Postgres store here (a
    real ``DreamPersonaStage`` needs a ``BehaviorEventLog`` this module has no DSN for; see the
    module docstring)."""

    name: str = "dream_persona"
    transitions: tuple[str, ...] = ("dream_facts",)

    async def run(self, ctx: StageContext) -> StageResult:
        return Transition(
            to="dream_facts",
            output=Artifact(
                kind="wombat.dream_persona_report",
                produced_by=self.name,
                provenance=Provenance(source="system", confidence=1.0, recorded_at=ctx.clock()),
                data={"stepped": []},
            ),
        )


class _PassthroughFactsStage:
    """TK-297 mechanical reshape (flagged per the ticket's own sanction, EP-13): a trivial
    always-transitions-onward double standing in for ``DreamFactsStage`` — this module's ACs are
    about the outcome pass, never touching a chat/user-facts store here (a real ``DreamFactsStage``
    needs a ``ChatTurnStore``/``UserFactsStore`` this module has no DSN for; see the module
    docstring)."""

    name: str = "dream_facts"
    transitions: tuple[str, ...] = ("dream_derive",)

    async def run(self, ctx: StageContext) -> StageResult:
        return Transition(
            to="dream_derive",
            output=Artifact(
                kind="wombat.dream_facts_report",
                produced_by=self.name,
                provenance=Provenance(source="system", confidence=1.0, recorded_at=ctx.clock()),
                data={"new_facts": 0},
            ),
        )


@dataclass
class _PassthroughDeriveStage:
    """TK-299 mechanical reshape (flagged per the ticket's own sanction): a trivial
    always-transitions-onward double standing in for ``DreamDeriveStage`` — this module's ACs are
    about the outcome pass, never touching an ``ExternalItemStore``/``UserFactsStore`` here (TK-299
    owns its own acceptance criteria in ``tests/behavior/test_dream_derive.py``)."""

    name: str = "dream_derive"
    transitions: tuple[str, ...] = ("dream_observe",)

    async def run(self, ctx: StageContext) -> StageResult:
        return Transition(
            to="dream_observe",
            output=Artifact(
                kind="wombat.dream_derive_report",
                produced_by=self.name,
                provenance=Provenance(source="system", confidence=1.0, recorded_at=ctx.clock()),
                data={"new_facts": 0},
            ),
        )


@dataclass
class _PassthroughObserveStage:
    """TK-314 mechanical reshape (flagged per the ticket's own sanction): a trivial
    always-transitions-onward double standing in for ``DreamObserveStage`` — this module's ACs are
    about the outcome pass, never touching an ``ObservationStore``/``UserFactsStore`` here (TK-314
    owns its own acceptance criteria in ``tests/behavior/test_dream_observe.py``)."""

    name: str = "dream_observe"
    transitions: tuple[str, ...] = ("dream_behavior_log",)

    async def run(self, ctx: StageContext) -> StageResult:
        return Transition(
            to="dream_behavior_log",
            output=Artifact(
                kind="wombat.dream_observe_report",
                produced_by=self.name,
                provenance=Provenance(source="system", confidence=1.0, recorded_at=ctx.clock()),
                data={"new_facts": 0},
            ),
        )


@dataclass
class _PassthroughBehaviorLogStage:
    """TK-111 mechanical reshape (flagged per the ticket's own sanction, Q-98): a trivial
    always-transitions-onward double standing in for ``DreamBehaviorLogStage`` — this module's
    ACs are about the outcome pass, never touching the KG or a Postgres store here (a real
    ``DreamBehaviorLogStage`` needs a ``BehaviorEventLog`` this module has no DSN for; see the
    module docstring)."""

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


@dataclass
class _PassthroughWindowStage:
    """TK-112 mechanical reshape (flagged per the ticket's own sanction, Q-99e): a trivial
    always-transitions-onward double standing in for ``WriteWindowSummariesStage`` — this
    module's ACs are about the outcome pass, never touching the KG or a Postgres store here (a
    real ``WriteWindowSummariesStage`` needs a ``BehaviorEventLog`` this module has no DSN for;
    see the module docstring)."""

    name: str = "dream_window"
    transitions: tuple[str, ...] = ("dream_pattern",)

    async def run(self, ctx: StageContext) -> StageResult:
        return Transition(
            to="dream_pattern",
            output=Artifact(
                kind="wombat.dream_window_report",
                produced_by=self.name,
                provenance=Provenance(source="system", confidence=1.0, recorded_at=ctx.clock()),
                data={},
            ),
        )


@dataclass
class _PassthroughPatternStage:
    """TK-113 mechanical reshape (flagged per the ticket's own sanction, Q-99f): a trivial
    always-transitions-onward double standing in for ``PatternDetectorStage`` — this module's ACs
    are about the outcome pass, never touching the queue here (a real ``PatternDetectorStage``
    needs an injected ``enqueue`` callable and the loaded psychology KB; see the module
    docstring)."""

    name: str = "dream_pattern"
    transitions: tuple[str, ...] = ("dream_run",)

    async def run(self, ctx: StageContext) -> StageResult:
        return Transition(
            to="dream_run",
            output=Artifact(
                kind="wombat.dream_pattern_report",
                produced_by=self.name,
                provenance=Provenance(source="system", confidence=1.0, recorded_at=ctx.clock()),
                data={},
            ),
        )


def _never_called_model(guard: object) -> FakeModel:
    """The engine assembles a model per-context eagerly (even off a dream stage that never
    dispatches it), so the factory itself must succeed — only ``complete()`` must never fire."""
    return FakeModel(raises=AssertionError("dream stages never call the mouth"))


def _build_engine(*, entity_kg: InMemoryEntityKG, labeler: OutcomeLabeler) -> Engine:
    """Mirrors ``test_dream_pathway_e2e.py``'s ``_build_stack_with_raising_dream`` idiom: a
    hand-built Engine over ``cold_boot_bundle()`` (in-memory substrate, zero infra) with
    ``wombat.dream`` registered via ``build_dream_pathway`` over a REAL ``DreamOutcomeStage``.

    TK-47: ``build_dream_pathway`` now also requires a ``consolidate`` (entry) stage. A zero-work
    ``DreamConsolidationStage`` is wired here purely to satisfy the graph shape — this KG has no
    dirty subjects and the journal has no committed steps, so both sweepers report zero work on
    tick 1 and the stage transitions straight through, unaffected by this module's own ACs.
    """
    dream_reconciler = CoherenceReconciler(
        entity_kg=entity_kg, store=entity_kg, oracle=TableOracle([])
    )
    bundle = cold_boot_bundle()
    dream_extractor = ClaimExtractor(
        journal=bundle.journal,
        entity_kg=entity_kg,
        model=ReplayModel([]),
        source_registry=SourceRegistry(),
    )
    dream_consolidation_stage = DreamConsolidationStage(
        reconciler=dream_reconciler, extractor=dream_extractor
    )
    dream_outcome_stage = DreamOutcomeStage(entity_kg=entity_kg, labeler=labeler, user_id=_USER_ID)
    dream_graph = build_dream_pathway(
        dream_consolidation_stage,
        dream_outcome_stage,
        _PassthroughTuneStage(),
        _PassthroughPersonaStage(),
        _PassthroughFactsStage(),
        _PassthroughDeriveStage(),
        _PassthroughObserveStage(),
        _PassthroughBehaviorLogStage(),
        _PassthroughWindowStage(),
        _PassthroughPatternStage(),
    )

    bundle.pathways.register(DREAM_PATHWAY_ID, dream_graph)

    models = ModelRegistry()
    models.register_factory("deepseek", _never_called_model)

    return Engine(
        models=models,
        journal=bundle.journal,
        graph_store=bundle.graph_store,
        latent=bundle.latent,
        pathways=bundle.pathways,
        model_profile="deepseek",
        clock=lambda: _FIXED_NOW,
    )


# ================================================================================================
# AC1: e2e — feedback item -> OUTCOME_LOAD_BEARING, no-feedback item -> OUTCOME_IGNORED
# ================================================================================================


async def test_ac1_e2e_dream_run_labels_feedback_and_no_feedback_items() -> None:
    entity_kg = InMemoryEntityKG()
    writer = ObservationWriter(
        entity_kg=entity_kg, scope_registry=ScopeRegistry(), user_id=_USER_ID
    )
    labeler = OutcomeLabeler(writer=writer)

    # Two PENDING items across two event classes.
    pending_fb_id = await labeler.stamp_pending(
        item_ref="item-fb",
        event_class=EventClass.CALENDAR_CONFLICT,
        disposition="surfaced",
        resolved_at=_FIXED_NOW,
    )
    pending_nofb_id = await labeler.stamp_pending(
        item_ref="item-nofb",
        event_class=EventClass.MORNING_BRIEF,
        disposition="surfaced",
        resolved_at=_FIXED_NOW,
    )

    # One BEHAVIOR_OBSERVED 'useful' feedback claim for item-fb only (mirrors bootstrap.py's
    # absorb_feedback write shape exactly).
    await writer.record(
        Claim(
            predicate=ClaimPredicate.BEHAVIOR_OBSERVED,
            subject="item-fb",
            value=json.dumps({"kind": "feedback", "response": "useful"}),
            event_id=None,
            observed_at=_FIXED_NOW,
        )
    )

    engine = _build_engine(entity_kg=entity_kg, labeler=labeler)

    final = await engine.run(
        run_id="run-outcome-ac1",
        session_id="run-outcome-ac1",
        pathway_id=DREAM_PATHWAY_ID,
        initial=dream_trigger_artifact(_FIXED_NOW),
    )

    assert final.status is RunStatus.COMPLETED
    stage_names = {step.stage_name for step in final.steps}
    assert {"dream_outcome", "dream_run"} <= stage_names

    # The feedback item's PENDING claim is invalidated.
    pending_fb_claim = await entity_kg.get_claim(pending_fb_id)
    assert pending_fb_claim is not None
    assert pending_fb_claim.valid_to is not None

    # ...and an active OUTCOME_LOAD_BEARING claim now exists, enumerable via claims_about the
    # event class (the corpus TK-49 reads).
    scored_cc = await entity_kg.claims_about(EventClass.CALENDAR_CONFLICT.value, scope=_SCOPE)
    active_cc = [s.claim for s in scored_cc if s.claim.valid_to is None]
    assert len(active_cc) == 1
    assert active_cc[0].predicate == ClaimPredicate.OUTCOME_LOAD_BEARING.value
    fb_envelope = json.loads(active_cc[0].payload)
    fb_value = json.loads(fb_envelope["value"])
    assert fb_value["item_ref"] == "item-fb"

    # The no-feedback item's PENDING claim is also invalidated...
    pending_nofb_claim = await entity_kg.get_claim(pending_nofb_id)
    assert pending_nofb_claim is not None
    assert pending_nofb_claim.valid_to is not None

    # ...and got OUTCOME_IGNORED (no feedback, no calendar/draft signal -> the default rule),
    # enumerable the same way.
    scored_mb = await entity_kg.claims_about(EventClass.MORNING_BRIEF.value, scope=_SCOPE)
    active_mb = [s.claim for s in scored_mb if s.claim.valid_to is None]
    assert len(active_mb) == 1
    assert active_mb[0].predicate == ClaimPredicate.OUTCOME_IGNORED.value
    nofb_envelope = json.loads(active_mb[0].payload)
    nofb_value = json.loads(nofb_envelope["value"])
    assert nofb_value["item_ref"] == "item-nofb"


# ================================================================================================
# AC2: empty corpus — clean COMPLETED, zero claim writes, dream_run still reached
# ================================================================================================


async def test_ac2_empty_corpus_completes_cleanly_with_zero_claim_writes() -> None:
    entity_kg = InMemoryEntityKG()
    writer = ObservationWriter(
        entity_kg=entity_kg, scope_registry=ScopeRegistry(), user_id=_USER_ID
    )
    labeler = OutcomeLabeler(writer=writer)

    engine = _build_engine(entity_kg=entity_kg, labeler=labeler)

    final = await engine.run(
        run_id="run-outcome-ac2",
        session_id="run-outcome-ac2",
        pathway_id=DREAM_PATHWAY_ID,
        initial=dream_trigger_artifact(_FIXED_NOW),
    )

    assert final.status is RunStatus.COMPLETED
    stage_names = {step.stage_name for step in final.steps}
    assert {"dream_outcome", "dream_run"} <= stage_names

    # Zero claim writes: no claims exist for ANY event class in the closed set.
    for event_class in EventClass:
        scored = await entity_kg.claims_about(event_class.value, scope=_SCOPE)
        assert scored == ()

    # The dream_outcome step's own report artifact confirms zero items/labels/errors.
    outcome_step = next(step for step in final.steps if step.stage_name == "dream_outcome")
    assert outcome_step.result.output is not None
    assert outcome_step.result.output.data == {"items_collected": 0, "labeled": 0, "errors": 0}
