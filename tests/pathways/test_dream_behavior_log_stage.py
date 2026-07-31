"""TK-111 — DreamBehaviorLogStage acceptance criteria (EP-21, Q-98).

In-memory substrate, ZERO network/model: ``entity_kg`` is cog-worx's ``InMemoryEntityKG``, seeded
directly via ``ObservationWriter`` (mirrors ``tests/user_model/test_outcome_loop_wiring.py``'s own
idiom). The Postgres-backed ``BehaviorEventLog`` side is stood in for by a REAL instance over an
unreachable DSN (lazy — never actually connects) with its ``upsert`` method monkeypatched to a
recording/raising double — the genuine pg round-trip lives in ``tests/behavior/test_event_log.py``
(pg-gated); this module is about ``DreamBehaviorLogStage``'s own read/map/write-seam logic.

  AC1 (row mapping): terminal OUTCOME_* claims across two event classes -> ``run()`` upserts one
      row per claim with the Q-98-ruled mapping (idempotency_key=item_ref, event_type=event_class,
      source_id parsed via ``split_idempotency_key``, timestamp_utc=resolved_at, outcome_label=the
      claim's own predicate, duration_seconds=None); a still-``OUTCOME_PENDING`` claim never
      reaches the writer (AC2 filtering).
  AC5 (never-block): a store whose ``upsert()`` raises -> logged LOUD, the row is skipped (counted
      as an error), and ``run()`` STILL ``Transition``s to ``dream_window`` (TK-112's stage, this
      stage's downstream neighbor since the window-detect pass was inserted between the behavior
      log and the terminal) — proven both as a direct unit call AND end-to-end through a real
      ``Engine`` drive (the run reaches COMPLETED).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from cogworx.claims.provenance import Artifact, Provenance
from cogworx.knowledge.scopes import ScopeRegistry
from cogworx.loop.result import StageResult, Transition
from cogworx.loop.stage import StageContext
from cogworx.loop.state import RunStatus
from cogworx.model.registry import ModelRegistry
from cogworx.runtime.engine import Engine
from cogworx.testing.doubles import InMemoryEntityKG

from tests.support.stage_context_fake import FakeModel, StageContextFake
from wombat.behavior.event_log import BehaviorEventLog
from wombat.domain.item_identity import idempotency_key
from wombat.pathways.dream_pathway import (
    DREAM_PATHWAY_ID,
    DreamBehaviorLogStage,
    build_dream_pathway,
    dream_trigger_artifact,
)
from wombat.rating.params import EventClass
from wombat.substrate import cold_boot_bundle
from wombat.user_model.claims import Claim, ClaimPredicate
from wombat.user_model.observation_writer import ObservationWriter

_USER_ID = "dream-behavior-log-test-user"
_NOW = datetime(2026, 7, 9, 9, 0, 0, tzinfo=UTC)
_UNREACHABLE_DSN = "postgresql://nonexistent-host-should-never-be-dialed:1/db"


@dataclass
class _RecordedUpsert:
    idempotency_key: str
    event_type: str
    source_id: str
    timestamp_utc: datetime
    outcome_label: str
    duration_seconds: float | None


def _fake_store(
    monkeypatch: pytest.MonkeyPatch, *, raises: BaseException | None = None
) -> tuple[BehaviorEventLog, list[_RecordedUpsert]]:
    """A REAL ``BehaviorEventLog`` over an unreachable DSN (lazy — never connects) with
    ``upsert`` monkeypatched to either record its call or raise (AC5's injection seam)."""
    calls: list[_RecordedUpsert] = []

    def _upsert(
        self: BehaviorEventLog,
        *,
        idempotency_key: str,
        event_type: str,
        source_id: str,
        timestamp_utc: datetime,
        outcome_label: str,
        duration_seconds: float | None = None,
    ) -> None:
        if raises is not None:
            raise raises
        calls.append(
            _RecordedUpsert(
                idempotency_key=idempotency_key,
                event_type=event_type,
                source_id=source_id,
                timestamp_utc=timestamp_utc,
                outcome_label=outcome_label,
                duration_seconds=duration_seconds,
            )
        )

    monkeypatch.setattr(BehaviorEventLog, "upsert", _upsert)
    return BehaviorEventLog(_UNREACHABLE_DSN), calls


async def _write_terminal_claim(
    writer: ObservationWriter,
    *,
    event_class: EventClass,
    predicate: ClaimPredicate,
    item_ref: str,
    resolved_at: datetime,
) -> None:
    """Mirrors ``OutcomeLabeler.label_terminal``'s EXACT payload shape (outcome_labeler.py:117-127)
    without going through the labeler — this module is about the READ side, not TK-45's write
    seam."""
    value = json.dumps(
        {
            "item_ref": item_ref,
            "outcome": predicate.value,
            "source": "rule",
            "rule_name": "test_rule",
            "resolved_at": resolved_at.isoformat(),
        }
    )
    await writer.record(
        Claim(
            predicate=predicate,
            subject=event_class.value,
            value=value,
            event_id=None,
            observed_at=resolved_at,
        )
    )


async def _write_pending_claim(
    writer: ObservationWriter,
    *,
    event_class: EventClass,
    item_ref: str,
    resolved_at: datetime,
) -> None:
    """Mirrors ``OutcomeLabeler.stamp_pending``'s payload shape — an UNRESOLVED item, which
    DreamBehaviorLogStage must never log (AC2: only TERMINAL OUTCOME_* claims)."""
    value = json.dumps(
        {"item_ref": item_ref, "disposition": "surfaced", "resolved_at": resolved_at.isoformat()}
    )
    await writer.record(
        Claim(
            predicate=ClaimPredicate.OUTCOME_PENDING,
            subject=event_class.value,
            value=value,
            event_id=None,
            observed_at=resolved_at,
        )
    )


# ================================================================================================
# AC1: terminal claims are mapped and upserted; a still-pending claim never reaches the writer
# ================================================================================================


async def test_ac1_terminal_claims_are_upserted_with_the_mapped_row_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entity_kg = InMemoryEntityKG()
    writer = ObservationWriter(
        entity_kg=entity_kg, scope_registry=ScopeRegistry(), user_id=_USER_ID
    )
    store, calls = _fake_store(monkeypatch)

    item_ref_a = idempotency_key("calendar", "evt_a")
    item_ref_b = idempotency_key("gmail", "msg_b")
    item_ref_pending = idempotency_key("calendar", "evt_c")

    await _write_terminal_claim(
        writer,
        event_class=EventClass.CALENDAR_CONFLICT,
        predicate=ClaimPredicate.OUTCOME_LOAD_BEARING,
        item_ref=item_ref_a,
        resolved_at=_NOW,
    )
    await _write_terminal_claim(
        writer,
        event_class=EventClass.DRAFT_REPLY,
        predicate=ClaimPredicate.OUTCOME_REGRETTED,
        item_ref=item_ref_b,
        resolved_at=_NOW,
    )
    # AC2: a still-PENDING item must never reach the writer.
    await _write_pending_claim(
        writer,
        event_class=EventClass.MORNING_BRIEF,
        item_ref=item_ref_pending,
        resolved_at=_NOW,
    )

    stage = DreamBehaviorLogStage(store=store, entity_kg=entity_kg, user_id=_USER_ID)
    ctx = StageContextFake(now_fn=lambda: _NOW)
    result = await stage.run(ctx)

    assert isinstance(result, Transition)
    assert result.to == "dream_window"
    assert result.output.data == {"rows_upserted": 2, "errors": 0}

    assert len(calls) == 2  # NOT the pending item
    by_key = {call.idempotency_key: call for call in calls}

    assert by_key[item_ref_a].event_type == EventClass.CALENDAR_CONFLICT.value
    assert by_key[item_ref_a].source_id == "calendar"
    assert by_key[item_ref_a].outcome_label == ClaimPredicate.OUTCOME_LOAD_BEARING.value
    assert by_key[item_ref_a].timestamp_utc == _NOW
    assert by_key[item_ref_a].duration_seconds is None  # v1: no duration signal exists yet

    assert by_key[item_ref_b].event_type == EventClass.DRAFT_REPLY.value
    assert by_key[item_ref_b].source_id == "gmail"
    assert by_key[item_ref_b].outcome_label == ClaimPredicate.OUTCOME_REGRETTED.value

    assert item_ref_pending not in by_key


async def test_ac1_empty_corpus_completes_cleanly_with_zero_upserts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entity_kg = InMemoryEntityKG()
    store, calls = _fake_store(monkeypatch)
    stage = DreamBehaviorLogStage(store=store, entity_kg=entity_kg, user_id=_USER_ID)

    result = await stage.run(StageContextFake(now_fn=lambda: _NOW))

    assert isinstance(result, Transition)
    assert result.to == "dream_window"
    assert result.output.data == {"rows_upserted": 0, "errors": 0}
    assert calls == []


# ================================================================================================
# AC5: never-block — a raising store is caught, logged, and the stage still transitions
# ================================================================================================


async def test_ac5_store_raise_is_caught_logged_and_still_transitions(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    entity_kg = InMemoryEntityKG()
    writer = ObservationWriter(
        entity_kg=entity_kg, scope_registry=ScopeRegistry(), user_id=_USER_ID
    )
    store, calls = _fake_store(monkeypatch, raises=RuntimeError("simulated store failure — AC5"))

    item_ref = idempotency_key("calendar", "evt_x")
    await _write_terminal_claim(
        writer,
        event_class=EventClass.CALENDAR_CONFLICT,
        predicate=ClaimPredicate.OUTCOME_IGNORED,
        item_ref=item_ref,
        resolved_at=_NOW,
    )

    stage = DreamBehaviorLogStage(store=store, entity_kg=entity_kg, user_id=_USER_ID)
    ctx = StageContextFake(now_fn=lambda: _NOW)
    with caplog.at_level(logging.ERROR, logger="wombat.pathways.dream_pathway"):
        result = await stage.run(ctx)

    assert isinstance(result, Transition)
    assert result.to == "dream_window"  # STILL transitions — one bad row never blocks the terminal
    assert result.output.data == {"rows_upserted": 0, "errors": 1}
    assert calls == []
    assert any(
        record.levelno == logging.ERROR and "upsert failed" in record.message
        for record in caplog.records
    )


@dataclass
class _PassthroughStage:
    """A trivial always-transitions-onward double standing in for whichever real dream stage this
    module doesn't exercise (mirrors ``test_dream_schedule_e2e.py``'s own passthrough-stage
    convention) — this module's ACs are about ``DreamBehaviorLogStage`` alone."""

    name: str
    to: str
    transitions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        self.transitions = (self.to,)

    async def run(self, ctx: StageContext) -> StageResult:
        return Transition(
            to=self.to,
            output=Artifact(
                kind="test.passthrough",
                produced_by=self.name,
                provenance=Provenance(source="system", confidence=1.0, recorded_at=ctx.clock()),
                data={},
            ),
        )


async def test_ac5_engine_drive_completes_even_when_the_store_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: a REAL ``Engine`` drives ``wombat.dream`` through ``dream_behavior_log`` with a
    raising store — the run still reaches COMPLETED (the ticket's own AC5 wording)."""
    entity_kg = InMemoryEntityKG()
    writer = ObservationWriter(
        entity_kg=entity_kg, scope_registry=ScopeRegistry(), user_id=_USER_ID
    )
    store, calls = _fake_store(monkeypatch, raises=RuntimeError("simulated store failure — AC5"))

    item_ref = idempotency_key("calendar", "evt_y")
    await _write_terminal_claim(
        writer,
        event_class=EventClass.CALENDAR_CONFLICT,
        predicate=ClaimPredicate.OUTCOME_LOAD_BEARING,
        item_ref=item_ref,
        resolved_at=_NOW,
    )

    behavior_log_stage = DreamBehaviorLogStage(store=store, entity_kg=entity_kg, user_id=_USER_ID)

    bundle = cold_boot_bundle()
    dream_graph = build_dream_pathway(
        _PassthroughStage(name="dream_consolidate", to="dream_outcome"),
        _PassthroughStage(name="dream_outcome", to="dream_tune"),
        _PassthroughStage(name="dream_tune", to="dream_persona"),
        _PassthroughStage(name="dream_persona", to="dream_facts"),
        _PassthroughStage(name="dream_facts", to="dream_derive"),
        _PassthroughStage(name="dream_derive", to="dream_observe"),
        _PassthroughStage(name="dream_observe", to="dream_screenpipe"),
        _PassthroughStage(name="dream_screenpipe", to="dream_behavior_log"),
        behavior_log_stage,
        _PassthroughStage(name="dream_window", to="dream_pattern"),
        _PassthroughStage(name="dream_pattern", to="dream_run"),
    )
    bundle.pathways.register(DREAM_PATHWAY_ID, dream_graph)

    models = ModelRegistry()
    models.register_factory(
        "deepseek",
        lambda guard: FakeModel(raises=AssertionError("dream stages never call the mouth")),
    )
    engine = Engine(
        models=models,
        journal=bundle.journal,
        graph_store=bundle.graph_store,
        latent=bundle.latent,
        pathways=bundle.pathways,
        model_profile="deepseek",
        clock=lambda: _NOW,
    )

    final = await engine.run(
        run_id="run-ac5-engine",
        session_id="run-ac5-engine",
        pathway_id=DREAM_PATHWAY_ID,
        initial=dream_trigger_artifact(_NOW),
    )

    assert final.status is RunStatus.COMPLETED
    stage_names = [step.stage_name for step in final.steps]
    assert stage_names[-4:] == ["dream_behavior_log", "dream_window", "dream_pattern", "dream_run"]
    assert calls == []  # the store raised — nothing was ever recorded
