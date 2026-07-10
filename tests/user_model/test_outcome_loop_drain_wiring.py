"""TK-176 — outcome-loop drain-side wiring acceptance criteria (Q-90 split of TK-175, EP-12).

Four wires over EXISTING pieces: (1) config, (2) the shared user-scope entity-KG hoist in
``assemble_runtime``, (3) ``GateStage``'s new ``absorb_feedback``/``stamp_resolution`` seams, and
(4) ``sources.bootstrap._maybe_register_feedback`` registration.

  AC1 (diversion): a drained batch with one feedback-marked + one normal item — the feedback item
      is diverted BEFORE scoring (a ``BEHAVIOR_OBSERVED`` claim is written, the item acked),
      appears in NO gate decision entry / NO pending-set add / NO brief payload; the normal
      item's ``gate_decisions`` artifact is byte-identical to a run without the feedback item.
      ``test_ac1_...diversion...``, plus a DSN-gated proof that the bootstrap-composed absorb
      closure really writes the claim and acks against a real Postgres.
  AC2 (stamp): a gate-decided normal item yields an ``OUTCOME_PENDING`` claim in the Q-90
      class-subject shape; no terminal predicate exists. ``test_ac2_...stamp...``.
  AC3 (registration): ``WOMBAT_FEEDBACK_FILE`` set -> ``registry.source_ids`` contains
      ``"feedback"``; unset -> loud skip, no crash, no ``"feedback"`` id.
      ``test_ac3_...registration...``.
  AC4 (shared instance): after ``assemble_runtime`` (``replay_pending=False``, connection-free),
      ``bundle.observation_writer``'s KG IS ``bundle.entity_kg`` IS ``UserModel``'s (identity
      assert); a claim written through the writer reads back via ``claims_about`` on the same
      instance. ``test_ac4_...shared...``.

``asyncio_mode = "auto"`` is configured in pyproject.toml (pytest-asyncio), so async test
functions run directly — no manual ``asyncio.run()`` driving needed.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg
import pytest
from cogworx.claims.provenance import Artifact, Provenance
from cogworx.knowledge.scopes import ScopeRegistry
from cogworx.loop.result import Transition
from cogworx.loop.state import RunStatus
from cogworx.substrate.entity_kg import EntityKG
from cogworx.testing.doubles import InMemoryEntityKG

from tests.support.stage_context_fake import StageContextFake
from wombat import bootstrap
from wombat.config import WombatConfig
from wombat.domain.daily_ledger import ensure_schema as ensure_daily_ledger_schema
from wombat.gate.gate import gate_item_from_queue_item
from wombat.gate.models import GateAction, GateDecision
from wombat.gate.pending_journal_pg import ensure_schema as ensure_pending_journal_schema
from wombat.params import load_operating_params
from wombat.queue import EnqueueResult, QueueItem
from wombat.queue import ensure_schema as ensure_queue_schema
from wombat.rating.params import EventClass
from wombat.sources import bootstrap as sources_bootstrap
from wombat.sources.presence import PresenceSnapshot, PresenceState
from wombat.stages.artifacts import (
    DRAINED_BATCH,
    gate_decisions_from_artifact_data,
    queue_items_to_artifact_data,
)
from wombat.stages.gate_stage import GateStage, make_stub_evaluator
from wombat.user_model.claims import Claim, ClaimPredicate
from wombat.user_model.feedback_source import FeedbackSignal
from wombat.user_model.observation_writer import ObservationWriter
from wombat.user_model.outcome_inference import ItemDisposition
from wombat.user_model.outcome_labeler import OutcomeLabeler
from wombat.user_model.user_model import UserModel

_FIXED_NOW = datetime(2026, 7, 9, 12, 0, 0, tzinfo=UTC)

_URGENCY_THRESHOLD = 0.75
_STALENESS_CEILING_S = 300.0
_CONFIDENCE_FLOOR = 0.5

_ACTIVE = PresenceSnapshot(
    state=PresenceState.ACTIVE, confidence=1.0, idle_ms=0, taken_at=_FIXED_NOW.timestamp()
)

# The same stub-evaluate composition test_gate.py uses (Q-55 async-batch seam).
_evaluate = make_stub_evaluator(
    urgency_threshold=_URGENCY_THRESHOLD,
    staleness_ceiling_s=_STALENESS_CEILING_S,
    confidence_floor=_CONFIDENCE_FLOOR,
)

_DSN = os.environ.get("WOMBAT_TEST_PG_DSN")

_requires_pg = pytest.mark.skipif(
    not _DSN,
    reason=(
        "WOMBAT_TEST_PG_DSN is not set — skipping the TK-176 DSN-gated absorb proof, which "
        "requires a real throwaway Postgres. Start one with:\n"
        "  docker run --rm -d -p 5511:5432 -e POSTGRES_PASSWORD=wombat postgres:16\n"
        "then export WOMBAT_TEST_PG_DSN=postgresql://postgres:wombat@localhost:5511/postgres"
    ),
)


def _drained_batch_artifact(items: list[QueueItem]) -> Artifact:
    return Artifact(
        kind=DRAINED_BATCH,
        produced_by="drain_queue",
        provenance=Provenance(source="system", confidence=1.0, recorded_at=_FIXED_NOW),
        data=queue_items_to_artifact_data(items),
    )


def _config() -> WombatConfig:
    return WombatConfig(deepseek_api_key="sk-test", deepseek_base_url="https://api.deepseek.com")


# ================================================================================================
# AC1: diversion — feedback item excluded before scoring, claim written, item acked
# ================================================================================================


async def test_ac1_diversion_feedback_item_excluded_claim_written_and_acked() -> None:
    feedback_item = QueueItem(
        idempotency_key="fb-1",
        payload=FeedbackSignal(item_ref="item-x", response="useful").to_payload(),
        item_id=1,
    )
    normal_item = QueueItem(
        idempotency_key="n-1", payload={"item_kind": "generic", "stub_urgency": "low"}, item_id=2
    )

    absorbed: list[QueueItem] = []

    async def fake_absorb(item: QueueItem) -> None:
        absorbed.append(item)

    stage = GateStage(
        evaluate=_evaluate, presence_provider=lambda: _ACTIVE, absorb_feedback=fake_absorb
    )
    ctx = StageContextFake(
        now_fn=lambda: _FIXED_NOW,
        last_output_map={"drain_queue": _drained_batch_artifact([feedback_item, normal_item])},
    )

    result = await stage.run(ctx)

    # The feedback item was diverted BEFORE scoring — nothing else observed it.
    assert absorbed == [feedback_item]

    assert isinstance(result, Transition)
    assert result.output is not None
    entries = gate_decisions_from_artifact_data(result.output.data)

    # No gate decision entry for the feedback item — exactly the normal item survives.
    assert len(entries) == 1
    assert entries[0][1] == normal_item

    # The normal item's gate_decisions artifact is byte-identical to a run without the
    # feedback item ever present in the batch at all.
    baseline_stage = GateStage(evaluate=_evaluate, presence_provider=lambda: _ACTIVE)
    baseline_ctx = StageContextFake(
        now_fn=lambda: _FIXED_NOW,
        last_output_map={"drain_queue": _drained_batch_artifact([normal_item])},
    )
    baseline_result = await baseline_stage.run(baseline_ctx)
    assert isinstance(baseline_result, Transition)
    assert baseline_result.output is not None
    assert result.output.data == baseline_result.output.data


async def test_ac1_diversion_absorb_failure_is_caught_loud_and_normal_item_still_gates() -> None:
    """The FAULT POSTURE (ruled): an absorb exception is caught + logged, the drain keeps
    draining — the feedback item is still excluded (never scored), just left un-acked."""
    feedback_item = QueueItem(
        idempotency_key="fb-2",
        payload=FeedbackSignal(item_ref="item-y", response="not_useful").to_payload(),
        item_id=3,
    )
    normal_item = QueueItem(
        idempotency_key="n-2", payload={"item_kind": "generic", "stub_urgency": "low"}, item_id=4
    )

    async def failing_absorb(item: QueueItem) -> None:
        raise RuntimeError("simulated absorb failure")

    stage = GateStage(
        evaluate=_evaluate, presence_provider=lambda: _ACTIVE, absorb_feedback=failing_absorb
    )
    ctx = StageContextFake(
        now_fn=lambda: _FIXED_NOW,
        last_output_map={"drain_queue": _drained_batch_artifact([feedback_item, normal_item])},
    )

    result = await stage.run(ctx)  # must not raise

    assert isinstance(result, Transition)
    assert result.output is not None
    entries = gate_decisions_from_artifact_data(result.output.data)
    assert len(entries) == 1
    assert entries[0][1] == normal_item


@pytest.fixture
def clean_tables() -> None:
    """Mirrors ``tests/integration/test_serve_boot.py``'s own ``clean_tables`` convention: every
    schema ``assemble_runtime`` touches must exist before the DSN-gated test runs."""
    assert _DSN is not None
    with psycopg.connect(_DSN) as conn:
        ensure_queue_schema(conn)
        ensure_daily_ledger_schema(conn)
        ensure_pending_journal_schema(conn)
        with conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE wombat_queue")
            cur.execute("TRUNCATE TABLE daily_ledger")
            cur.execute("TRUNCATE TABLE pending_journal")
        conn.commit()


def _initial_artifact() -> Artifact:
    return Artifact(
        kind="drain-tick",
        produced_by="test",
        provenance=Provenance(source="system", confidence=1.0, recorded_at=datetime.now(UTC)),
        data={},
    )


@_requires_pg
async def test_ac1_dsn_bootstrap_composed_absorb_writes_claim_and_acks(
    clean_tables: None,
) -> None:
    assert _DSN is not None
    bootstrap.reset_engine()
    op = load_operating_params()
    bundle = bootstrap.assemble_runtime(config=_config(), dsn=_DSN, params=op, tz=ZoneInfo("UTC"))
    try:
        signal = FeedbackSignal(item_ref="item-42", response="useful")
        bundle.queue.enqueue(
            QueueItem(idempotency_key="feedback-42", payload=signal.to_payload())
        )

        final = await bundle.engine.run(
            run_id="run-feedback-absorb",
            session_id="sess-feedback-absorb",
            pathway_id=bundle.drain_pathway_id,
            initial=_initial_artifact(),
        )
        assert final.status is RunStatus.COMPLETED
        assert bundle.queue.drain() == []  # acked

        scored = await bundle.entity_kg.claims_about(
            "item-42", scope=f"user:{bootstrap._RUNTIME_USER_ID}"
        )
        assert len(scored) == 1
        stored = scored[0].claim
        assert stored.predicate == ClaimPredicate.BEHAVIOR_OBSERVED.value
        envelope = json.loads(stored.payload)
        value = json.loads(envelope["value"])
        assert value == {"kind": "feedback", "response": "useful"}
    finally:
        bundle.queue.close()
        bundle.daily_ledger.close()
        bundle.pending_journal.close()
        bootstrap.reset_engine()


# ================================================================================================
# AC2: stamp — a gate-decided normal item yields an OUTCOME_PENDING claim, Q-90 shape
# ================================================================================================


async def test_ac2_stamp_gate_decided_item_yields_outcome_pending_q90_shape() -> None:
    kg = InMemoryEntityKG()
    writer = ObservationWriter(entity_kg=kg, scope_registry=ScopeRegistry(), user_id="alice")
    labeler = OutcomeLabeler(writer=writer)
    user_model = UserModel(entity_kg=kg, user_id="alice")

    def _disposition_for(action: GateAction) -> ItemDisposition:
        return "held" if action is GateAction.HOLD else "surfaced"

    async def stamp_resolution(decision: GateDecision, queue_item: QueueItem) -> None:
        gate_item = gate_item_from_queue_item(queue_item)
        event_class = user_model.resolve_event_class(gate_item)
        await labeler.stamp_pending(
            item_ref=queue_item.idempotency_key,
            event_class=event_class,
            disposition=_disposition_for(decision.action),
            resolved_at=_FIXED_NOW,
        )

    normal_item = QueueItem(
        idempotency_key="n-42",
        payload={"item_kind": "generic", "stub_urgency": "high"},
        item_id=9,
    )
    stage = GateStage(
        evaluate=_evaluate, presence_provider=lambda: _ACTIVE, stamp_resolution=stamp_resolution
    )
    ctx = StageContextFake(
        now_fn=lambda: _FIXED_NOW,
        last_output_map={"drain_queue": _drained_batch_artifact([normal_item])},
    )

    await stage.run(ctx)

    scored = await kg.claims_about(EventClass.GENERIC.value, scope="user:alice")
    assert len(scored) == 1
    stored = scored[0].claim
    assert stored.subject == EventClass.GENERIC.value
    assert stored.predicate == ClaimPredicate.OUTCOME_PENDING.value

    envelope = json.loads(stored.payload)
    value = json.loads(envelope["value"])
    assert value["item_ref"] == "n-42"
    assert value["disposition"] == "surfaced"  # high urgency clears the stub's threshold

    terminal_predicates = {
        ClaimPredicate.OUTCOME_LOAD_BEARING.value,
        ClaimPredicate.OUTCOME_REGRETTED.value,
        ClaimPredicate.OUTCOME_IGNORED.value,
    }
    assert stored.predicate not in terminal_predicates


# ================================================================================================
# AC3: registration — WOMBAT_FEEDBACK_FILE set/unset
# ================================================================================================


class _StubEnqueuer:
    def enqueue(self, item: QueueItem) -> EnqueueResult:
        return EnqueueResult.QUEUED


def _feedback_config(feedback_file: str | None) -> WombatConfig:
    return WombatConfig(
        deepseek_api_key="sk-test",
        deepseek_base_url="https://api.deepseek.com",
        wombat_feedback_file=feedback_file,
    )


def test_ac3_registration_feedback_file_set_registers_feedback_source(tmp_path: Path) -> None:
    feedback_path = tmp_path / "feedback.txt"
    config = _feedback_config(str(feedback_path))

    registry = sources_bootstrap.build_source_registry(
        config, _StubEnqueuer(), tz=ZoneInfo("UTC")
    )

    assert "feedback" in registry.source_ids


def test_ac3_registration_unset_loud_skip_no_crash_no_feedback_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    config = _feedback_config(None)

    with caplog.at_level(logging.WARNING):
        registry = sources_bootstrap.build_source_registry(
            config, _StubEnqueuer(), tz=ZoneInfo("UTC")
        )

    assert "feedback" not in registry.source_ids
    assert "WOMBAT_FEEDBACK_FILE" in caplog.text


# ================================================================================================
# AC4: shared instance — bundle.observation_writer's KG IS bundle.entity_kg IS UserModel's
# ================================================================================================


async def test_ac4_shared_entity_kg_across_bundle_observation_writer_and_user_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, EntityKG] = {}

    class _SpyUserModel(UserModel):
        def __init__(self, *, entity_kg: EntityKG, user_id: str) -> None:
            captured["entity_kg"] = entity_kg
            super().__init__(entity_kg=entity_kg, user_id=user_id)

    monkeypatch.setattr(bootstrap, "UserModel", _SpyUserModel)

    op = load_operating_params()
    bundle = bootstrap.assemble_runtime(
        config=_config(),
        dsn="postgresql://fake-host/fake-db",
        params=op,
        replay_pending=False,
        tz=ZoneInfo("UTC"),
    )

    # UserModel was constructed over the EXACT SAME entity_kg instance the bundle exposes.
    assert captured["entity_kg"] is bundle.entity_kg

    # observation_writer's write seam wraps that SAME instance — proven behaviorally, since an
    # InMemoryEntityKG's storage is per-instance: a claim written through the writer is only
    # readable back via bundle.entity_kg if they share the identical instance.
    claim_id = await bundle.observation_writer.record(
        Claim(
            predicate=ClaimPredicate.BEHAVIOR_OBSERVED,
            subject="shared-check",
            value=json.dumps({"kind": "feedback", "response": "useful"}),
            event_id=None,
            observed_at=_FIXED_NOW,
        )
    )

    scored = await bundle.entity_kg.claims_about(
        "shared-check", scope=f"user:{bootstrap._RUNTIME_USER_ID}"
    )
    assert any(s.claim.id == claim_id for s in scored)
