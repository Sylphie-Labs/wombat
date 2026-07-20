"""TK-7 — ReviewOrSpeakStage acceptance criteria (Q-52).

All PURE: no Postgres, no cog-worx Engine. A FAKE queue records ``ack(item_id)`` calls;
``support.stage_context_fake.StageContextFake`` is importable via the ``pythonpath = ["tests"]``
pytest setting (TK-6 convention).
"""

from __future__ import annotations

import inspect
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from cogworx.claims.provenance import Artifact, Provenance
from cogworx.loop.result import Done, Transition

from tests.support.stage_context_fake import StageContextFake
from wombat.gate.decay import LedgerReset
from wombat.gate.gate import gate_item_from_queue_item
from wombat.gate.models import GateAction, GateDecision, ItemKind, ScoredItem
from wombat.gate.pending_set import InMemoryPendingJournal, PendingSet
from wombat.gate.pipeline import Gate
from wombat.queue import QueueItem
from wombat.rating.params import EventClass, RatingParams
from wombat.stages import review_or_speak as review_or_speak_module
from wombat.stages.artifacts import (
    GATE_DECISIONS,
    GateDecisionEntry,
    gate_decisions_to_artifact_data,
    hold_report_from_artifact_data,
    hold_report_to_artifact_data,
    surfaced_item_from_artifact_data,
    surfaced_item_held_chat_from_artifact_data,
)
from wombat.stages.review_or_speak import ReviewOrSpeakStage

_FIXED_NOW = datetime(2026, 7, 2, 12, 0, 0, tzinfo=UTC)


@dataclass
class _FakeQueue:
    """Records every ``ack(item_id)`` call — no Postgres, no real WombatQueue."""

    acked: list[int] = field(default_factory=list)

    def ack(self, item_id: int) -> None:
        self.acked.append(item_id)


def _gate_artifact(entries: list[GateDecisionEntry]) -> Artifact:
    return Artifact(
        kind=GATE_DECISIONS,
        produced_by="gate",
        provenance=Provenance(source="system", confidence=1.0, recorded_at=_FIXED_NOW),
        data=gate_decisions_to_artifact_data(entries),
    )


def _ctx(entries: list[GateDecisionEntry]) -> StageContextFake:
    return StageContextFake(
        now_fn=lambda: _FIXED_NOW,
        last_output_map={"gate": _gate_artifact(entries)},
    )


def _surface_entry(
    item_id: str = "s-1", urgency: float = 0.9, queue_item_id: int = 1
) -> GateDecisionEntry:
    scored = ScoredItem(item_id=item_id, item_kind=ItemKind.GENERIC, urgency=urgency, load=0.1)
    decision = GateDecision(action=GateAction.SURFACE_IMMEDIATE, items=(scored,))
    queue_item = QueueItem(
        idempotency_key=item_id, payload={"subject": "hi"}, item_id=queue_item_id
    )
    return decision, queue_item


def _hold_entry(
    item_id: str = "h-1", urgency: float = 0.1, load: float = 0.2, queue_item_id: int = 2
) -> GateDecisionEntry:
    scored = ScoredItem(item_id=item_id, item_kind=ItemKind.GENERIC, urgency=urgency, load=load)
    decision = GateDecision(action=GateAction.HOLD, items=(scored,))
    queue_item = QueueItem(
        idempotency_key=item_id, payload={"subject": "quiet"}, item_id=queue_item_id
    )
    return decision, queue_item


# --- AC1: surface_immediate / surface_flush -> Transition(to="compose_dispatch") ------------------


async def test_ac1_surface_transitions_to_compose_dispatch_and_acks_once() -> None:
    entry = _surface_entry(item_id="s-1", queue_item_id=7)
    queue = _FakeQueue()
    stage = ReviewOrSpeakStage(queue=queue)
    ctx = _ctx([entry])

    result = await stage.run(ctx)

    assert isinstance(result, Transition)
    assert result.to == "compose_dispatch"
    assert queue.acked == [7]

    action, scored_item, queue_item = surfaced_item_from_artifact_data(result.output.data)
    orig_decision, orig_queue_item = entry
    assert action is orig_decision.action
    assert scored_item == orig_decision.items[0]
    assert queue_item == orig_queue_item


async def test_ac1_surface_flush_also_transitions() -> None:
    scored = ScoredItem(item_id="s-2", item_kind=ItemKind.GENERIC, urgency=0.95, load=0.0)
    decision = GateDecision(action=GateAction.SURFACE_FLUSH, items=(scored,))
    queue_item = QueueItem(idempotency_key="s-2", payload={}, item_id=9)
    queue = _FakeQueue()
    stage = ReviewOrSpeakStage(queue=queue)
    ctx = _ctx([(decision, queue_item)])

    result = await stage.run(ctx)

    assert isinstance(result, Transition)
    assert result.to == "compose_dispatch"
    assert queue.acked == [9]


def test_no_item_kind_branch_or_composer_reference_in_source() -> None:
    """AC1: ReviewOrSpeak contains NO branch on item_kind and NO reference to any composer
    (ISS-5 — routing is TK-10's job). Verified structurally against the module's own imports."""
    source = inspect.getsource(review_or_speak_module)
    assert "ComposeStage" not in source
    assert "ComposeDispatchRouter" not in source
    assert "compose_dispatch_router" not in source
    assert "wombat.stages.compose" not in source


# --- AC2: hold -> Done(wombat.hold_report), acked, reason present ---------------------------------


async def test_ac2_hold_returns_done_hold_report_and_acks_once() -> None:
    entry = _hold_entry(item_id="h-1", urgency=0.1, load=0.2, queue_item_id=3)
    queue = _FakeQueue()
    stage = ReviewOrSpeakStage(queue=queue)
    ctx = _ctx([entry])

    result = await stage.run(ctx)

    assert isinstance(result, Done)
    assert result.output.kind == "wombat.hold_report"
    assert queue.acked == [3]

    holds = hold_report_from_artifact_data(result.output.data)
    assert len(holds) == 1
    hold = holds[0]
    assert hold["item_id"] == "h-1"
    assert hold["item_kind"] == "generic"
    assert "urgency=0.10" in hold["reason"]
    assert "load=0.20" in hold["reason"]


# --- DEC-57/TK-272: a HOLD entry whose item is CHAT forwards as surfaced, held_chat=True ----------


async def test_held_chat_forwards_as_surfaced_not_a_hold_report() -> None:
    scored = ScoredItem(item_id="c-1", item_kind=ItemKind.CHAT, urgency=0.1, load=0.1)
    decision = GateDecision(action=GateAction.HOLD, items=(scored,))
    queue_item = QueueItem(
        idempotency_key="c-1", payload={"item_kind": "chat", "text": "hi"}, item_id=5
    )
    queue = _FakeQueue()
    stage = ReviewOrSpeakStage(queue=queue)
    ctx = _ctx([(decision, queue_item)])

    result = await stage.run(ctx)

    assert isinstance(result, Transition)
    assert result.to == "compose_dispatch"
    assert queue.acked == [5]

    action, scored_item, round_queue_item = surfaced_item_from_artifact_data(result.output.data)
    assert action is GateAction.HOLD
    assert scored_item == scored
    assert round_queue_item == queue_item
    assert surfaced_item_held_chat_from_artifact_data(result.output.data) is True


async def test_held_non_chat_stays_a_hold_report_held_chat_never_set() -> None:
    """DEC-57e: every other kind's HOLD stays byte-identical — no held_chat forwarding."""
    entry = _hold_entry(item_id="h-2", queue_item_id=4)
    queue = _FakeQueue()
    stage = ReviewOrSpeakStage(queue=queue)
    ctx = _ctx([entry])

    result = await stage.run(ctx)

    assert isinstance(result, Done)
    assert result.output.kind == "wombat.hold_report"
    holds = hold_report_from_artifact_data(result.output.data)
    assert len(holds) == 1
    assert holds[0]["item_id"] == "h-2"


# --- TK-278 AC2: an asr-shaped chat item, scored by the REAL production gate ----------------------


class _NoOpRollover:
    def check(self) -> LedgerReset | None:
        return None


@dataclass
class _FakeUserModel:
    rating_params: RatingParams

    def resolve_event_class(self, item: object) -> EventClass:
        return EventClass.GENERIC

    async def ratings_for(self, item: object) -> RatingParams:
        return self.rating_params


@dataclass
class _FakeCeiling:
    def allow(self, event_class: EventClass) -> bool:
        return True

    def record(self, event_class: EventClass) -> None:
        pass


async def test_asr_chat_item_through_real_gate_routes_to_compose_and_never_pends() -> None:
    """TK-278 (DEC-60a): an asr-shaped QueueItem (item_kind 'chat', voice_turn True,
    transcript) drained through the REAL production ``Gate`` (not the TK-6 stub) with a
    below-threshold rating still produces a HOLD decision carrying item_kind=CHAT, routes to
    compose_dispatch with held_chat=True via ReviewOrSpeakStage's DEC-57/TK-272 branch, and the
    durable pending set gains NO entry (DEC-57/TK-272 R1: chat never absorbs)."""
    queue_item = QueueItem(
        idempotency_key="asr-1",
        payload={"item_kind": "chat", "voice_turn": True, "transcript": "buy milk"},
        item_id=11,
    )
    gate_item = gate_item_from_queue_item(queue_item)
    assert gate_item.item_kind is ItemKind.CHAT  # stamped by construction, no Q-41 fallback

    pending_set = PendingSet(journal=InMemoryPendingJournal(), max_pending=50)
    below_threshold_params = RatingParams(
        urgency_base=0.0, urgency_gain=0.0, load_base=0.0, load_gain=0.0
    )
    gate = Gate(
        user_model=_FakeUserModel(rating_params=below_threshold_params),
        pending_set=pending_set,
        ceiling=_FakeCeiling(),
        urgency_threshold=0.5,
        load_flush_threshold=10.0,
        flush_min_age_seconds=100.0,
        decay_ttl_seconds=float("inf"),
        day_rollover=_NoOpRollover(),
        clock=lambda: 1000.0,
    )

    decision = await gate.pipeline([gate_item])

    assert decision.action is GateAction.HOLD
    assert decision.items[0].item_kind is ItemKind.CHAT
    assert pending_set.list() == []  # never absorbed into the durable pending set

    queue = _FakeQueue()
    stage = ReviewOrSpeakStage(queue=queue)
    ctx = _ctx([(decision, queue_item)])

    result = await stage.run(ctx)

    assert isinstance(result, Transition)
    assert result.to == "compose_dispatch"
    assert queue.acked == [11]
    assert surfaced_item_held_chat_from_artifact_data(result.output.data) is True


# --- hold_report wire: json.dumps round-trip regression (Q-49) ------------------------------------


def test_hold_report_artifact_data_is_json_native_and_round_trips() -> None:
    holds = [
        {
            "item_id": "h-1",
            "item_kind": "generic",
            "reason": "urgency=0.10 load=0.20 below surface bar",
            "urgency": 0.1,
            "load": 0.2,
        },
        {
            "item_id": "h-2",
            "item_kind": "draft",
            "reason": "urgency=0.05 load=0.00 below surface bar",
            "urgency": 0.05,
            "load": 0.0,
        },
    ]

    data = hold_report_to_artifact_data(holds)
    serialized = json.dumps(data)
    assert hold_report_from_artifact_data(json.loads(serialized)) == holds


# --- Defensive: a 2-entry batch (1 hold + 1 surface) ----------------------------------------------


async def test_defensive_mixed_batch_forwards_surface_acks_both_and_logs_hold(
    caplog: Any,
) -> None:
    hold = _hold_entry(item_id="h-1", queue_item_id=1)
    surface = _surface_entry(item_id="s-1", queue_item_id=2)
    queue = _FakeQueue()
    stage = ReviewOrSpeakStage(queue=queue)
    ctx = _ctx([hold, surface])

    with caplog.at_level(logging.WARNING):
        result = await stage.run(ctx)

    assert isinstance(result, Transition)
    assert result.to == "compose_dispatch"
    assert sorted(queue.acked) == [1, 2]

    _action, scored_item, _queue_item = surfaced_item_from_artifact_data(result.output.data)
    assert scored_item.item_id == "s-1"

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("h-1" in record.message for record in warnings)
