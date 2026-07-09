"""TK-175 — outcome-loop dream-side wiring acceptance criteria (Q-90 split of TK-175, EP-12).

``DreamOutcomeStage`` is inserted as the ``wombat.dream`` entry, transitioning onward to the
TK-46 terminal scaffold (Q-90 end-state: ``dream_outcome`` -> ``dream_run``). Both ACs drive a
REAL cog-worx ``Engine`` (in-memory substrate — no Postgres needed) over ``build_dream_pathway``,
mirroring ``tests/integration/test_dream_pathway_e2e.py``'s hand-built-``Engine`` idiom.

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
from datetime import UTC, datetime

from cogworx.knowledge.scopes import ScopeRegistry
from cogworx.loop.state import RunStatus
from cogworx.model.registry import ModelRegistry
from cogworx.runtime.engine import Engine
from cogworx.testing.doubles import InMemoryEntityKG

from tests.support.stage_context_fake import FakeModel
from wombat.pathways.dream_pathway import (
    DREAM_PATHWAY_ID,
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


def _never_called_model(guard: object) -> FakeModel:
    """The engine assembles a model per-context eagerly (even off a dream stage that never
    dispatches it), so the factory itself must succeed — only ``complete()`` must never fire."""
    return FakeModel(raises=AssertionError("dream stages never call the mouth"))


def _build_engine(*, entity_kg: InMemoryEntityKG, labeler: OutcomeLabeler) -> Engine:
    """Mirrors ``test_dream_pathway_e2e.py``'s ``_build_stack_with_raising_dream`` idiom: a
    hand-built Engine over ``cold_boot_bundle()`` (in-memory substrate, zero infra) with
    ``wombat.dream`` registered via ``build_dream_pathway`` over a REAL ``DreamOutcomeStage``."""
    dream_outcome_stage = DreamOutcomeStage(entity_kg=entity_kg, labeler=labeler, user_id=_USER_ID)
    dream_graph = build_dream_pathway(dream_outcome_stage)

    bundle = cold_boot_bundle()
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
