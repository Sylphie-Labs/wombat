"""TK-222 — ChatReplyStage acceptance criteria (EP-32, Q-110(d) ruling 3).

All PURE: no Postgres, no real network, no real asyncio server. ``support.stage_context_fake``
is importable via the ``tests`` package (mirrors ``tests/unit/test_compose_stage.py``).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import pytest
from cogworx.claims.provenance import Artifact, Provenance
from cogworx.loop.result import Transition

from tests.support.stage_context_fake import StageContextFake
from wombat.chat.surface import ChatReplyBroker
from wombat.gate.models import ItemKind
from wombat.stages.artifacts import COMPOSED_OUTPUT, composed_output_to_artifact_data
from wombat.stages.chat_reply import (
    ChatReplyStage,
    chat_delivery_from_artifact_data,
    chat_delivery_to_artifact_data,
)

_FIXED_NOW = datetime(2026, 7, 9, 12, 0, 0, tzinfo=UTC)
_ITEM_ID = "chat-item-1"
_TEXT = "the composed reply"


class _RaisingBroker(ChatReplyBroker):
    def resolve(self, item_id: str, text: str) -> None:
        raise RuntimeError("boom")


def _compose_output_artifact(
    *, item_id: str = _ITEM_ID, text: str = _TEXT, item_kind: ItemKind = ItemKind.CHAT
) -> Artifact:
    return Artifact(
        kind=COMPOSED_OUTPUT,
        produced_by="compose",
        provenance=Provenance(source="system", confidence=1.0, recorded_at=_FIXED_NOW),
        data=composed_output_to_artifact_data(text, item_id, item_kind, False),
    )


def _ctx(*, compose_output: Artifact | None) -> StageContextFake:
    return StageContextFake(
        now_fn=lambda: _FIXED_NOW,
        last_output_map={"compose": compose_output},
    )


# --- name/transitions ------------------------------------------------------------------------


def test_stage_declares_name_and_speak_as_its_only_edge() -> None:
    stage = ChatReplyStage(broker=None)
    assert stage.name == "chat_reply"
    assert stage.transitions == ("speak",)


# --- success: a wired broker resolves and delivered=True ----------------------------------------


async def test_wired_broker_end_to_end_unblocks_the_registered_future() -> None:
    """A REAL ChatReplyBroker: register() before run(), resolve happens inside run(), the
    future's result equals the composed text — the exact correlation chat_reply performs."""
    broker = ChatReplyBroker()
    future = broker.register(_ITEM_ID)
    stage = ChatReplyStage(broker=broker)
    ctx = _ctx(compose_output=_compose_output_artifact())

    result = await stage.run(ctx)

    assert isinstance(result, Transition)
    assert result.to == "speak"
    item_id, delivered = chat_delivery_from_artifact_data(result.output.data)
    assert item_id == _ITEM_ID
    assert delivered is True
    assert future.done()
    assert future.result() == _TEXT


# --- broker=None is a pure pass-through (chat-disabled boot shape) ------------------------------


async def test_broker_none_is_a_pure_pass_through_delivered_false() -> None:
    stage = ChatReplyStage(broker=None)
    ctx = _ctx(compose_output=_compose_output_artifact())

    result = await stage.run(ctx)

    assert isinstance(result, Transition)
    assert result.to == "speak"
    item_id, delivered = chat_delivery_from_artifact_data(result.output.data)
    assert item_id == _ITEM_ID
    assert delivered is False


# --- unknown item_id resolves to a documented no-op (a non-chat item riding the same mouth) -----


async def test_non_chat_item_resolves_as_a_no_op_but_still_reports_delivered_true() -> None:
    """ChatReplyBroker.resolve() is a documented no-op for an id it never registered — the stage
    itself has no way to distinguish "resolved a real waiter" from "resolved nothing" (that
    distinction lives inside the broker); it only degrades on a RAISE (see below)."""
    broker = ChatReplyBroker()  # nothing registered
    stage = ChatReplyStage(broker=broker)
    ctx = _ctx(
        compose_output=_compose_output_artifact(item_id="generic-1", item_kind=ItemKind.GENERIC)
    )

    result = await stage.run(ctx)

    assert isinstance(result, Transition)
    assert result.to == "speak"
    item_id, delivered = chat_delivery_from_artifact_data(result.output.data)
    assert item_id == "generic-1"
    assert delivered is True  # resolve() didn't raise — it just had nothing to deliver to


# --- degrade: a raising broker never raises out of run() ----------------------------------------


async def test_raising_broker_degrades_to_delivered_false_and_logs_one_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    stage = ChatReplyStage(broker=_RaisingBroker())
    ctx = _ctx(compose_output=_compose_output_artifact())

    with caplog.at_level(logging.WARNING, logger="wombat.stages.chat_reply"):
        result = await stage.run(ctx)

    assert isinstance(result, Transition)
    assert result.to == "speak"
    _item_id, delivered = chat_delivery_from_artifact_data(result.output.data)
    assert delivered is False
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1


# --- no compose output yet raises (mirrors ComposeStage/SpeakSink's own posture) ----------------


async def test_no_compose_output_yet_raises_runtime_error() -> None:
    stage = ChatReplyStage(broker=None)
    ctx = _ctx(compose_output=None)

    with pytest.raises(RuntimeError):
        await stage.run(ctx)


# --- wire round-trip (Q-49) -----------------------------------------------------------------------


def test_chat_delivery_artifact_data_round_trips() -> None:
    data = chat_delivery_to_artifact_data(item_id=_ITEM_ID, delivered=True)
    assert chat_delivery_from_artifact_data(data) == (_ITEM_ID, True)
