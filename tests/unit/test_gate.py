"""TK-6 — deterministic gate stub acceptance criteria (Q-48; async-batch seam, Q-55).

All PURE: no Postgres, no model. ``support.stage_context_fake`` is importable via the
``pythonpath = ["tests"]`` pytest setting (no sys.path hack, TK-6 test-tooling cleanup).
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from cogworx.claims.provenance import Artifact, Provenance
from cogworx.loop.result import Transition

from tests.support.stage_context_fake import StageContextFake
from wombat.gate.decay import LedgerReset
from wombat.gate.gate import gate_item_from_queue_item, stub_evaluate
from wombat.gate.models import GateAction, GateDecision, GateItem, ItemKind, ScoredItem
from wombat.gate.pending_set import InMemoryPendingJournal, PendingSet
from wombat.gate.pipeline import Gate
from wombat.queue import QueueItem
from wombat.rating.params import EventClass, RatingParams
from wombat.sources.presence import PresenceSnapshot, PresenceState
from wombat.stages.artifacts import (
    DRAINED_BATCH,
    GATE_DECISIONS,
    gate_decisions_from_artifact_data,
    gate_decisions_to_artifact_data,
    queue_items_to_artifact_data,
)
from wombat.stages.gate_stage import GateStage, make_gate_evaluator, make_stub_evaluator

_FIXED_NOW = datetime(2026, 7, 2, 12, 0, 0, tzinfo=UTC)
_TAKEN_AT = _FIXED_NOW.timestamp()

_ACTIVE = PresenceSnapshot(
    state=PresenceState.ACTIVE, confidence=1.0, idle_ms=1_000, taken_at=_TAKEN_AT
)
_UNKNOWN = PresenceSnapshot(
    state=PresenceState.UNKNOWN, confidence=0.0, idle_ms=None, taken_at=_TAKEN_AT
)

_URGENCY_THRESHOLD = 0.75
_STALENESS_CEILING_S = 300.0
_CONFIDENCE_FLOOR = 0.5

# The async-batch evaluate callable GateStage is composed with (Q-55) — built via the same
# ``make_stub_evaluator`` factory production composition uses, binding urgency_threshold/
# staleness_ceiling_s/confidence_floor exactly as ``functools.partial`` composition would.
_evaluate = make_stub_evaluator(
    urgency_threshold=_URGENCY_THRESHOLD,
    staleness_ceiling_s=_STALENESS_CEILING_S,
    confidence_floor=_CONFIDENCE_FLOOR,
)


def _drained_batch_artifact(items: list[QueueItem]) -> Artifact:
    return Artifact(
        kind=DRAINED_BATCH,
        produced_by="drain_queue",
        provenance=Provenance(source="system", confidence=1.0, recorded_at=_FIXED_NOW),
        data=queue_items_to_artifact_data(items),
    )


# --- gate_item_from_queue_item mapping -----------------------------------------------------------


def test_gate_item_from_queue_item_maps_id_kind_created_at_payload() -> None:
    queue_item = QueueItem(
        idempotency_key="q-1",
        payload={"item_kind": "reflection", "stub_urgency": "high"},
        item_id=7,
    )

    gate_item = gate_item_from_queue_item(queue_item)

    assert gate_item.item_id == "q-1"
    assert gate_item.item_kind == ItemKind.REFLECTION
    assert gate_item.created_at == 0.0
    assert gate_item.payload == {"item_kind": "reflection", "stub_urgency": "high"}


def test_gate_item_from_queue_item_defaults_missing_or_invalid_kind_to_generic() -> None:
    missing = QueueItem(idempotency_key="q-2", payload={}, item_id=1)
    invalid = QueueItem(idempotency_key="q-3", payload={"item_kind": "not-a-kind"}, item_id=2)

    assert gate_item_from_queue_item(missing).item_kind == ItemKind.GENERIC
    assert gate_item_from_queue_item(invalid).item_kind == ItemKind.GENERIC


# --- stub_evaluate: AC1 / AC2 / AC3 --------------------------------------------------------------


def test_ac1_high_urgency_and_active_presence_surfaces_immediately() -> None:
    gate_item = GateItem(
        item_id="i-1",
        item_kind=ItemKind.GENERIC,
        created_at=0.0,
        payload={"stub_urgency": "high"},
    )

    decision = stub_evaluate(
        gate_item,
        _ACTIVE,
        urgency_threshold=_URGENCY_THRESHOLD,
        staleness_ceiling_s=_STALENESS_CEILING_S,
        confidence_floor=_CONFIDENCE_FLOOR,
    )

    assert decision.action is GateAction.SURFACE_IMMEDIATE
    assert decision.items == (
        ScoredItem(item_id="i-1", item_kind=ItemKind.GENERIC, urgency=0.9, load=0.0),
    )


def test_ac2_low_urgency_holds() -> None:
    gate_item = GateItem(
        item_id="i-2",
        item_kind=ItemKind.GENERIC,
        created_at=0.0,
        payload={"stub_urgency": "low"},
    )

    decision = stub_evaluate(
        gate_item,
        _ACTIVE,
        urgency_threshold=_URGENCY_THRESHOLD,
        staleness_ceiling_s=_STALENESS_CEILING_S,
        confidence_floor=_CONFIDENCE_FLOOR,
    )

    assert decision.action is GateAction.HOLD


def test_ac3_unknown_presence_holds_regardless_of_high_urgency() -> None:
    gate_item = GateItem(
        item_id="i-3",
        item_kind=ItemKind.GENERIC,
        created_at=0.0,
        payload={"stub_urgency": "high"},
    )

    decision = stub_evaluate(
        gate_item,
        _UNKNOWN,
        urgency_threshold=_URGENCY_THRESHOLD,
        staleness_ceiling_s=_STALENESS_CEILING_S,
        confidence_floor=_CONFIDENCE_FLOOR,
    )

    assert decision.action is GateAction.HOLD


def test_none_presence_holds_without_scoring() -> None:
    gate_item = GateItem(
        item_id="i-4",
        item_kind=ItemKind.GENERIC,
        created_at=0.0,
        payload={"stub_urgency": "high"},
    )

    decision = stub_evaluate(
        gate_item,
        None,
        urgency_threshold=_URGENCY_THRESHOLD,
        staleness_ceiling_s=_STALENESS_CEILING_S,
        confidence_floor=_CONFIDENCE_FLOOR,
    )

    assert decision.action is GateAction.HOLD


def test_missing_stub_urgency_defaults_to_low_quiet_default() -> None:
    gate_item = GateItem(item_id="i-5", item_kind=ItemKind.GENERIC, created_at=0.0, payload={})

    decision = stub_evaluate(
        gate_item,
        _ACTIVE,
        urgency_threshold=_URGENCY_THRESHOLD,
        staleness_ceiling_s=_STALENESS_CEILING_S,
        confidence_floor=_CONFIDENCE_FLOOR,
    )

    assert decision.action is GateAction.HOLD
    assert decision.items[0].urgency == 0.1


# --- AC4: 10 items in sequence, no exceptions, 0 model calls -------------------------------------


async def test_ac4_ten_items_in_sequence_all_decide_with_no_exceptions() -> None:
    items = [
        QueueItem(idempotency_key=f"item-{i}", payload={"stub_urgency": "high"}, item_id=i)
        for i in range(10)
    ]
    ctx = StageContextFake(
        now_fn=lambda: _FIXED_NOW,
        last_output_map={"drain_queue": _drained_batch_artifact(items)},
    )
    # `evaluate`/`presence_provider` never construct or touch a Model; GateStage's only imports
    # are gate.gate + sources.presence + stages.artifacts — none reach cogworx.model at all, so
    # there is structurally no seam through which a model call could happen (NG-4).
    stage = GateStage(evaluate=_evaluate, presence_provider=lambda: _ACTIVE)

    result = await stage.run(ctx)

    assert isinstance(result, Transition)
    entries = gate_decisions_from_artifact_data(result.output.data)
    assert len(entries) == 10
    for decision, _queue_item in entries:
        assert decision.action is GateAction.SURFACE_IMMEDIATE


# --- GateStage integration (pure, via StageContextFake / real Artifact) --------------------------


async def test_gate_stage_surfaces_a_high_urgency_item_round_trip_through_the_wire_helpers() -> (
    None
):
    """At the Q-51 mvp batch_size=1 composition rule, one drained item -> one evaluate() call ->
    one GateDecision, pairing exactly (decision, its own queue_item) as the sole entry."""
    items = [QueueItem(idempotency_key="a", payload={"stub_urgency": "high"}, item_id=1)]
    ctx = StageContextFake(
        now_fn=lambda: _FIXED_NOW,
        last_output_map={"drain_queue": _drained_batch_artifact(items)},
    )
    stage = GateStage(evaluate=_evaluate, presence_provider=lambda: _ACTIVE)

    result = await stage.run(ctx)

    assert isinstance(result, Transition)
    assert result.to == "review_or_speak"
    assert result.output.kind == GATE_DECISIONS
    assert result.output.produced_by == "gate"
    assert result.output.provenance.source == "system"
    assert result.output.provenance.confidence == 1.0

    entries = gate_decisions_from_artifact_data(result.output.data)
    assert len(entries) == 1
    decision, queue_item = entries[0]
    assert decision.action is GateAction.SURFACE_IMMEDIATE
    assert queue_item == items[0]


async def test_gate_stage_holds_a_low_urgency_item() -> None:
    items = [QueueItem(idempotency_key="b", payload={"stub_urgency": "low"}, item_id=2)]
    ctx = StageContextFake(
        now_fn=lambda: _FIXED_NOW,
        last_output_map={"drain_queue": _drained_batch_artifact(items)},
    )
    stage = GateStage(evaluate=_evaluate, presence_provider=lambda: _ACTIVE)

    result = await stage.run(ctx)

    assert isinstance(result, Transition)
    entries = gate_decisions_from_artifact_data(result.output.data)
    assert len(entries) == 1
    decision, queue_item = entries[0]
    assert decision.action is GateAction.HOLD
    assert queue_item == items[0]


async def test_gate_stage_holds_all_items_when_presence_is_unknown() -> None:
    items = [
        QueueItem(idempotency_key="a", payload={"stub_urgency": "high"}, item_id=1),
        QueueItem(idempotency_key="b", payload={"stub_urgency": "high"}, item_id=2),
    ]
    ctx = StageContextFake(
        now_fn=lambda: _FIXED_NOW,
        last_output_map={"drain_queue": _drained_batch_artifact(items)},
    )
    stage = GateStage(evaluate=_evaluate, presence_provider=lambda: _UNKNOWN)

    result = await stage.run(ctx)

    assert isinstance(result, Transition)
    entries = gate_decisions_from_artifact_data(result.output.data)
    assert len(entries) == 2
    assert all(decision.action is GateAction.HOLD for decision, _ in entries)


async def test_gate_stage_touches_no_ctx_member_beyond_last_output_and_clock() -> None:
    """A ctx that raises on everything but clock()/last_output() must not blow up the stage —
    proving GateStage's ctx surface really is last_output (+ clock for provenance only, Q-48)."""
    items = [QueueItem(idempotency_key="only", payload={"stub_urgency": "high"}, item_id=1)]
    ctx = StageContextFake(
        now_fn=lambda: _FIXED_NOW,
        last_output_map={"drain_queue": _drained_batch_artifact(items)},
    )
    stage = GateStage(evaluate=_evaluate, presence_provider=lambda: _ACTIVE)

    result = await stage.run(ctx)

    assert isinstance(result, Transition)


# --- TK-273 (ISS-23): ONE gate-decision INFO line per decided entry, ids/enums only --------------


async def test_gate_stage_logs_one_info_line_per_decided_item_with_no_payload_text(
    caplog: Any,
) -> None:
    # At the Q-51 mvp batch_size=1 composition rule GateStage is always called with exactly one
    # drained item; two separate single-item runs (rather than one two-item batch, whose
    # make_stub_evaluator degenerate case pairs the SAME last-item decision with every queue_item)
    # exercise the surface_immediate and hold cases independently.
    high_items = [
        QueueItem(
            idempotency_key="a",
            payload={"stub_urgency": "high", "secret_body": "do not log this"},
            item_id=1,
        )
    ]
    low_items = [
        QueueItem(
            idempotency_key="b",
            payload={"stub_urgency": "low", "secret_body": "or this either"},
            item_id=2,
        )
    ]
    stage = GateStage(evaluate=_evaluate, presence_provider=lambda: _ACTIVE)

    with caplog.at_level(logging.INFO, logger="wombat.stages.gate_stage"):
        result_a = await stage.run(
            StageContextFake(
                now_fn=lambda: _FIXED_NOW,
                last_output_map={"drain_queue": _drained_batch_artifact(high_items)},
            )
        )
        result_b = await stage.run(
            StageContextFake(
                now_fn=lambda: _FIXED_NOW,
                last_output_map={"drain_queue": _drained_batch_artifact(low_items)},
            )
        )

    assert isinstance(result_a, Transition)
    assert isinstance(result_b, Transition)
    gate_records = [r for r in caplog.records if r.name == "wombat.stages.gate_stage"]
    assert len(gate_records) == 2
    for record in gate_records:
        assert "secret_body" not in record.message
        assert "do not log this" not in record.message
        assert "or this either" not in record.message
    a_record = next(r for r in gate_records if "item_id='a'" in r.message)
    assert "item_kind=" in a_record.message
    assert "event_class=" in a_record.message
    assert "action='surface_immediate'" in a_record.message
    assert "urgency=0.9" in a_record.message
    b_record = next(r for r in gate_records if "item_id='b'" in r.message)
    assert "action='hold'" in b_record.message


# --- round-trip: gate_decisions_to/from_artifact_data is lossless (incl. carried queue_item) -----


def test_gate_decisions_artifact_data_round_trip_is_lossless() -> None:
    decision_surface = GateDecision(
        action=GateAction.SURFACE_IMMEDIATE,
        items=(ScoredItem(item_id="a", item_kind=ItemKind.DRAFT, urgency=0.9, load=0.2),),
    )
    # A HOLD with EMPTY items (Q-55: the production Gate's HOLD carries no scored item at all).
    decision_hold = GateDecision(action=GateAction.HOLD, items=())
    queue_item_a = QueueItem(idempotency_key="a", payload={"stub_urgency": "high"}, item_id=1)
    queue_item_b = QueueItem(idempotency_key="b", payload={"stub_urgency": "low"}, item_id=2)
    entries = [(decision_surface, queue_item_a), (decision_hold, queue_item_b)]

    data = gate_decisions_to_artifact_data(entries)

    assert data == {
        "decisions": [
            {
                "action": "surface_immediate",
                "scored_items": [
                    {
                        "item_id": "a",
                        "item_kind": "draft",  # enum on the wire as its .value string (Q-49)
                        "urgency": 0.9,
                        "load": 0.2,
                    }
                ],
                "queue_item": {
                    "idempotency_key": "a",
                    "payload": {"stub_urgency": "high"},
                    "item_id": 1,
                },
            },
            {
                "action": "hold",
                "scored_items": [],  # Q-55: zero-or-many, never a bare "scored_item"
                "queue_item": {
                    "idempotency_key": "b",
                    "payload": {"stub_urgency": "low"},
                    "item_id": 2,
                },
            },
        ]
    }
    assert gate_decisions_from_artifact_data(data) == entries


def test_gate_decisions_artifact_data_round_trips_a_multi_item_flush_decision() -> None:
    """Q-55: SURFACE_FLUSH can carry MANY scored items (the whole flushed pending set)."""
    decision_flush = GateDecision(
        action=GateAction.SURFACE_FLUSH,
        items=(
            ScoredItem(item_id="x", item_kind=ItemKind.GENERIC, urgency=0.8, load=0.3),
            ScoredItem(item_id="y", item_kind=ItemKind.DRAFT, urgency=0.6, load=0.4),
        ),
    )
    queue_item = QueueItem(idempotency_key="x", payload={"stub_urgency": "high"}, item_id=5)
    entries = [(decision_flush, queue_item)]

    data = gate_decisions_to_artifact_data(entries)

    assert len(data["decisions"][0]["scored_items"]) == 2
    assert gate_decisions_from_artifact_data(data) == entries


# --- Q-49: Artifact.data must be plain-JSON-native (spine-wide guarantee) ------------------------


def test_gate_decisions_artifact_data_is_json_native_and_round_trips_through_json() -> None:
    """A full JSON round-trip THROUGH A STRING (not just the dict): json.dumps must not raise on
    the enum members, and json.loads back through the inverse must reproduce the exact entries.
    Spans multiple ItemKind + GateAction values so every enum lands on the wire as its .value."""
    entries = [
        (
            GateDecision(
                action=GateAction.SURFACE_IMMEDIATE,
                items=(ScoredItem(item_id="a", item_kind=ItemKind.BRIEF, urgency=0.9, load=0.2),),
            ),
            QueueItem(idempotency_key="a", payload={"stub_urgency": "high"}, item_id=1),
        ),
        (
            GateDecision(
                action=GateAction.SURFACE_FLUSH,
                items=(
                    ScoredItem(item_id="b", item_kind=ItemKind.REFLECTION, urgency=0.8, load=0.3),
                ),
            ),
            QueueItem(idempotency_key="b", payload={"stub_urgency": "low"}, item_id=2),
        ),
        (
            GateDecision(
                action=GateAction.HOLD,
                items=(ScoredItem(item_id="c", item_kind=ItemKind.DRAFT, urgency=0.1, load=0.0),),
            ),
            QueueItem(idempotency_key="c", payload={"n": 3}, item_id=3),
        ),
        (
            GateDecision(
                action=GateAction.HOLD,
                items=(ScoredItem(item_id="d", item_kind=ItemKind.GENERIC, urgency=0.1, load=0.0),),
            ),
            QueueItem(idempotency_key="d", payload={}, item_id=4),
        ),
    ]

    data = gate_decisions_to_artifact_data(entries)

    # 1. json.dumps SUCCEEDS (would raise TypeError if an enum MEMBER were embedded).
    serialized = json.dumps(data)
    # 2. full round-trip through the JSON string yields the exact same entries (lossless).
    assert gate_decisions_from_artifact_data(json.loads(serialized)) == entries


# --- make_gate_evaluator: the production Gate adapter (Q-55) ------------------------------------


class _FixedUserModel:
    """Returns a fixed RatingParams/EventClass regardless of the item (mirrors test_pipeline.py's
    ``_FakeUserModel`` — a real ``Gate`` needs the ``UserModelProtocol`` shape, not a mock)."""

    def __init__(self, rating_params: RatingParams) -> None:
        self._rating_params = rating_params

    def resolve_event_class(self, item: GateItem) -> EventClass:
        return EventClass.GENERIC

    async def ratings_for(self, item: GateItem) -> RatingParams:
        return self._rating_params


class _AlwaysAllowCeiling:
    def allow(self, event_class: EventClass) -> bool:
        return True

    def record(self, event_class: EventClass) -> None:
        pass


class _NoOpRollover:
    """A ``DayRolloverProtocol`` double that never fires (TK-28, Q-73) — this module covers the
    ``make_gate_evaluator`` adapter, not decay/rollover."""

    def check(self) -> LedgerReset | None:
        return None


def _real_gate(*, rating_params: RatingParams, urgency_threshold: float = 0.5) -> Gate:
    return Gate(
        user_model=_FixedUserModel(rating_params),
        pending_set=PendingSet(journal=InMemoryPendingJournal(), max_pending=50),
        ceiling=_AlwaysAllowCeiling(),
        urgency_threshold=urgency_threshold,
        load_flush_threshold=10.0,
        flush_min_age_seconds=100.0,
        decay_ttl_seconds=float("inf"),
        day_rollover=_NoOpRollover(),
        clock=lambda: _TAKEN_AT,
    )


async def test_make_gate_evaluator_surfaces_a_worthy_item_when_presence_is_active() -> None:
    rating_params = RatingParams(urgency_base=0.9, urgency_gain=0.0, load_base=0.0, load_gain=0.0)
    gate = _real_gate(rating_params=rating_params, urgency_threshold=0.1)
    evaluate = make_gate_evaluator(
        gate=gate,
        staleness_ceiling_s=_STALENESS_CEILING_S,
        confidence_floor=_CONFIDENCE_FLOOR,
        clock=lambda: _TAKEN_AT,
    )
    item = GateItem(item_id="a", item_kind=ItemKind.GENERIC, created_at=0.0, payload={})

    decision = await evaluate([item], _ACTIVE)

    assert decision.action is GateAction.SURFACE_IMMEDIATE
    assert [scored.item_id for scored in decision.items] == ["a"]


async def test_make_gate_evaluator_holds_when_presence_is_none() -> None:
    """``presence=None`` -> ``presence_hold`` returns True -> ``surfacing_permitted=False`` —
    the SAME degrade-to-held rule every presence-first call site in wombat honors (Q-12)."""
    rating_params = RatingParams(urgency_base=0.9, urgency_gain=0.0, load_base=0.0, load_gain=0.0)
    gate = _real_gate(rating_params=rating_params, urgency_threshold=0.1)
    evaluate = make_gate_evaluator(
        gate=gate,
        staleness_ceiling_s=_STALENESS_CEILING_S,
        confidence_floor=_CONFIDENCE_FLOOR,
        clock=lambda: _TAKEN_AT,
    )
    item = GateItem(item_id="a", item_kind=ItemKind.GENERIC, created_at=0.0, payload={})

    decision = await evaluate([item], None)

    assert decision.action is GateAction.HOLD
    assert decision.items == ()  # the production Gate's HOLD carries no scored item (Q-55)


async def test_make_gate_evaluator_holds_when_presence_is_unknown() -> None:
    rating_params = RatingParams(urgency_base=0.9, urgency_gain=0.0, load_base=0.0, load_gain=0.0)
    gate = _real_gate(rating_params=rating_params, urgency_threshold=0.1)
    evaluate = make_gate_evaluator(
        gate=gate,
        staleness_ceiling_s=_STALENESS_CEILING_S,
        confidence_floor=_CONFIDENCE_FLOOR,
        clock=lambda: _TAKEN_AT,
    )
    item = GateItem(item_id="a", item_kind=ItemKind.GENERIC, created_at=0.0, payload={})

    decision = await evaluate([item], _UNKNOWN)

    assert decision.action is GateAction.HOLD


def test_drained_batch_artifact_data_is_json_native() -> None:
    """Lock the spine-wide 'Artifact.data is JSON-native' rule for the DRAINED_BATCH wire too so
    TK-7/8/10 inherit a tested guarantee (QueueItem is already JSON-native — this is a lock)."""
    items = [
        QueueItem(idempotency_key="a", payload={"stub_urgency": "high", "n": 1}, item_id=1),
        QueueItem(idempotency_key="b", payload={}, item_id=None),
    ]

    data = queue_items_to_artifact_data(items)

    # json.dumps must not raise — every field is plain-JSON-native.
    assert json.loads(json.dumps(data)) == data
