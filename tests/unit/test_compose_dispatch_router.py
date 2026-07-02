"""TK-10 — ComposeDispatchRouter acceptance criteria (Q-51).

All PURE: no Postgres, no model. ``support.stage_context_fake`` is importable via the
``pythonpath = ["tests"]`` pytest setting.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from cogworx.claims.provenance import Artifact, Provenance
from cogworx.loop.result import Transition

from support.stage_context_fake import StageContextFake
from wombat.gate.models import GateAction, ItemKind, ScoredItem
from wombat.queue import QueueItem
from wombat.stages.artifacts import (
    COMPOSE_REQUEST,
    SURFACED_ITEM,
    compose_request_from_artifact_data,
    surfaced_item_from_artifact_data,
    surfaced_item_to_artifact_data,
)
from wombat.stages.compose_dispatch_router import ComposeDispatchRouter

_FIXED_NOW = datetime(2026, 7, 2, 12, 0, 0, tzinfo=UTC)

_DEFAULT_MAP: dict[ItemKind, str] = {
    ItemKind.GENERIC: "compose",
    ItemKind.REFLECTION: "reflection_compose",
    ItemKind.DRAFT: "draft_compose",
    ItemKind.BRIEF: "brief_compose",
}


def _surfaced_item_artifact(
    action: GateAction, scored_item: ScoredItem, queue_item: QueueItem
) -> Artifact:
    return Artifact(
        kind=SURFACED_ITEM,
        produced_by="review_or_speak",
        provenance=Provenance(source="system", confidence=1.0, recorded_at=_FIXED_NOW),
        data=surfaced_item_to_artifact_data(action, scored_item, queue_item),
    )


def _ctx(action: GateAction, scored_item: ScoredItem, queue_item: QueueItem) -> StageContextFake:
    return StageContextFake(
        now_fn=lambda: _FIXED_NOW,
        last_output_map={
            "review_or_speak": _surfaced_item_artifact(action, scored_item, queue_item)
        },
    )


# --- AC1: item_kind=generic -> the default ComposeStage (TK-8) -----------------------------------


async def test_ac1_generic_kind_routes_to_compose_exactly() -> None:
    scored_item = ScoredItem(item_id="i-1", item_kind=ItemKind.GENERIC, urgency=0.5, load=0.1)
    queue_item = QueueItem(idempotency_key="i-1", payload={"subject": "hi"}, item_id=1)
    ctx = _ctx(GateAction.SURFACE_IMMEDIATE, scored_item, queue_item)
    router = ComposeDispatchRouter(composer_by_kind=_DEFAULT_MAP)

    result = await router.run(ctx)

    assert isinstance(result, Transition)
    assert result.to == "compose"
    assert result.to not in {"reflection_compose", "draft_compose", "brief_compose"}


# --- AC2: item_kind=reflection -> ReflectionComposeStage (TK-114) ---------------------------------


async def test_ac2_reflection_kind_routes_to_reflection_compose() -> None:
    scored_item = ScoredItem(item_id="i-2", item_kind=ItemKind.REFLECTION, urgency=0.5, load=0.1)
    queue_item = QueueItem(idempotency_key="i-2", payload={"text": "a thought"}, item_id=2)
    ctx = _ctx(GateAction.SURFACE_FLUSH, scored_item, queue_item)
    router = ComposeDispatchRouter(composer_by_kind=_DEFAULT_MAP)

    result = await router.run(ctx)

    assert isinstance(result, Transition)
    assert result.to == "reflection_compose"


# --- AC3: item_kind=draft -> the Gmail DraftComposer path (TK-79) ---------------------------------


async def test_ac3_draft_kind_routes_to_draft_compose() -> None:
    scored_item = ScoredItem(item_id="i-3", item_kind=ItemKind.DRAFT, urgency=0.5, load=0.1)
    queue_item = QueueItem(idempotency_key="i-3", payload={"body": "reply text"}, item_id=3)
    ctx = _ctx(GateAction.SURFACE_IMMEDIATE, scored_item, queue_item)
    router = ComposeDispatchRouter(composer_by_kind=_DEFAULT_MAP)

    result = await router.run(ctx)

    assert isinstance(result, Transition)
    assert result.to == "draft_compose"


# --- AC4: unregistered kind -> fallback to compose + warning; never raises, never drops -----------


async def test_ac4_unregistered_kind_falls_back_to_compose_and_logs_warning(
    caplog: Any,
) -> None:
    incomplete_map: dict[ItemKind, str] = {ItemKind.GENERIC: "compose"}  # DRAFT missing
    scored_item = ScoredItem(item_id="i-4", item_kind=ItemKind.DRAFT, urgency=0.5, load=0.1)
    queue_item = QueueItem(idempotency_key="i-4", payload={"body": "reply text"}, item_id=4)
    ctx = _ctx(GateAction.SURFACE_IMMEDIATE, scored_item, queue_item)
    router = ComposeDispatchRouter(composer_by_kind=incomplete_map)

    with caplog.at_level(logging.WARNING):
        result = await router.run(ctx)

    assert isinstance(result, Transition)
    assert result.to == "compose"
    assert any("DRAFT" in record.message or "compose_dispatch" in record.message.lower()
               or "not in composer_by_kind" in record.message for record in caplog.records)
    assert any(record.levelno == logging.WARNING for record in caplog.records)


# --- PAYLOAD BOUNDARY: compose_request.payload excludes internals even when the entry carries them


async def test_payload_boundary_excludes_scores_action_and_queue_internals() -> None:
    scored_item = ScoredItem(item_id="i-5", item_kind=ItemKind.GENERIC, urgency=0.9, load=0.5)
    queue_item = QueueItem(
        idempotency_key="i-5", payload={"subject": "Renewal notice", "sender": "a@b.com"}, item_id=5
    )
    ctx = _ctx(GateAction.SURFACE_IMMEDIATE, scored_item, queue_item)
    router = ComposeDispatchRouter(composer_by_kind=_DEFAULT_MAP)

    result = await router.run(ctx)

    assert isinstance(result, Transition)
    assert result.output.kind == COMPOSE_REQUEST
    item_id, item_kind, payload = compose_request_from_artifact_data(result.output.data)

    assert payload == {"subject": "Renewal notice", "sender": "a@b.com"}
    forbidden_keys = {"urgency", "load", "action", "scored_item", "idempotency_key", "leased_by"}
    assert forbidden_keys.isdisjoint(payload.keys())
    assert item_id == "i-5"
    assert item_kind is ItemKind.GENERIC


# --- surfaced_item wire round-trip: json.dumps must not raise; inverse is lossless (Q-49) --------


def test_surfaced_item_artifact_data_is_json_native_and_round_trips() -> None:
    scored_item = ScoredItem(item_id="i-6", item_kind=ItemKind.BRIEF, urgency=0.8, load=0.3)
    queue_item = QueueItem(idempotency_key="i-6", payload={"n": 1, "text": "hi"}, item_id=6)

    data = surfaced_item_to_artifact_data(GateAction.SURFACE_FLUSH, scored_item, queue_item)

    assert data == {
        "action": "surface_flush",
        "scored_item": {
            "item_id": "i-6",
            "item_kind": "brief",
            "urgency": 0.8,
            "load": 0.3,
        },
        "queue_item": {
            "idempotency_key": "i-6",
            "payload": {"n": 1, "text": "hi"},
            "item_id": 6,
        },
    }

    serialized = json.dumps(data)
    action, round_scored, round_queue = surfaced_item_from_artifact_data(json.loads(serialized))
    assert action is GateAction.SURFACE_FLUSH
    assert round_scored == scored_item
    assert round_queue == queue_item


# --- ComposeDispatchRouter.transitions reflects the injected map ----------------------------------


def test_transitions_are_the_sorted_set_of_composer_map_values() -> None:
    router = ComposeDispatchRouter(composer_by_kind=_DEFAULT_MAP)

    assert router.transitions == (
        "brief_compose",
        "compose",
        "draft_compose",
        "reflection_compose",
    )
