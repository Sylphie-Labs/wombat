"""TK-8 — ComposeStage mouth-wiring acceptance criteria (Q-50).

All PURE: no Postgres, no real network. ``support.stage_context_fake`` is importable via the
``pythonpath = ["tests"]`` pytest setting.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from cogworx.claims.provenance import Artifact, Provenance
from cogworx.cost.budget import BudgetExceededError
from cogworx.loop.result import Transition
from cogworx.model.base import ModelResponse

from tests.support.stage_context_fake import FakeModel, StageContextFake
from wombat.compose.templates import TemplateComposer
from wombat.config import ConfigurationError, WombatConfig
from wombat.gate.models import ItemKind
from wombat.persona.live import LivePersona
from wombat.persona.matrix import DEFAULT_MATRIX, Humor, PersonaMatrix
from wombat.stages.artifacts import (
    COMPOSE_REQUEST,
    COMPOSED_OUTPUT,
    compose_request_from_artifact_data,
    compose_request_to_artifact_data,
    composed_output_from_artifact_data,
    composed_output_to_artifact_data,
)
from wombat.stages.compose import ComposeStage

_FIXED_NOW = datetime(2026, 7, 2, 12, 0, 0, tzinfo=UTC)

_INTERNAL_KEYS = {"urgency", "load", "action", "surface", "scored_item", "idempotency_key"}

_ITEM_ID = "i-1"
_ITEM_KIND = ItemKind.GENERIC
_PAYLOAD: dict[str, Any] = {"subject": "Renewal notice", "sender": "billing@acme.com"}


def _config(api_key: str = "sk-test") -> WombatConfig:
    return WombatConfig(deepseek_api_key=api_key, deepseek_base_url="https://api.deepseek.com")


def _compose_request_artifact() -> Artifact:
    return Artifact(
        kind=COMPOSE_REQUEST,
        produced_by="compose_dispatch",
        provenance=Provenance(source="system", confidence=1.0, recorded_at=_FIXED_NOW),
        data=compose_request_to_artifact_data(_ITEM_ID, _ITEM_KIND, _PAYLOAD),
    )


def _ctx(model: FakeModel) -> StageContextFake:
    return StageContextFake(
        now_fn=lambda: _FIXED_NOW,
        last_output_map={"compose_dispatch": _compose_request_artifact()},
        model_fake=model,
    )


# --- AC1: success path — model called once, prompt carries payload, no internals -----------------


async def test_ac1_success_path_phrases_via_model_and_prompt_excludes_internals() -> None:
    model = FakeModel(
        response=ModelResponse(text="phrased!", model_id="deepseek-chat", finish_reason="stop")
    )
    ctx = _ctx(model)
    stage = ComposeStage(config=_config(), template_composer=TemplateComposer())

    result = await stage.run(ctx)

    assert isinstance(result, Transition)
    assert result.to == "chat_reply"  # TK-222, Q-110(d): the mouth now hops through chat_reply
    assert result.output.kind == COMPOSED_OUTPUT
    assert result.output.produced_by == "compose"
    text, item_id, item_kind, degraded = composed_output_from_artifact_data(result.output.data)
    assert text == "phrased!"
    assert item_id == _ITEM_ID
    assert item_kind is _ITEM_KIND
    assert degraded is False

    # the fake model was called exactly once
    assert len(model.calls) == 1
    captured = model.calls[0]
    system_msg, user_msg = captured
    assert system_msg.role == "system"
    assert user_msg.role == "user"

    # the user message carries the payload's user-facing content
    assert "Renewal notice" in user_msg.content
    assert "billing@acme.com" in user_msg.content

    # NONE of the gate/queue-internal keys appear anywhere in the captured prompt
    full_prompt_text = system_msg.content + "\n" + user_msg.content
    for internal_key in _INTERNAL_KEYS:
        assert internal_key not in full_prompt_text


# --- AC2(a): provider/connection/5xx error degrades, never raises --------------------------------


async def test_ac2a_provider_error_degrades_to_template_without_raising() -> None:
    model = FakeModel(raises=ConnectionError("503 Service Unavailable"))
    ctx = _ctx(model)
    stage = ComposeStage(config=_config(), template_composer=TemplateComposer())

    result = await stage.run(ctx)

    assert isinstance(result, Transition)
    assert result.to == "chat_reply"  # TK-222, Q-110(d): the mouth now hops through chat_reply
    text, item_id, item_kind, degraded = composed_output_from_artifact_data(result.output.data)
    assert degraded is True
    assert text == TemplateComposer().render(_ITEM_KIND, _PAYLOAD)
    assert item_id == _ITEM_ID
    assert item_kind is _ITEM_KIND


# --- AC2(b): timeout degrades, bounded elapsed time -----------------------------------------------


async def test_ac2b_timeout_degrades_to_template_within_bound() -> None:
    model = FakeModel(sleep_seconds=5.0)  # far longer than the tiny timeout below
    ctx = _ctx(model)
    stage = ComposeStage(
        config=_config(), template_composer=TemplateComposer(), timeout_seconds=0.05
    )

    start = time.monotonic()
    result = await stage.run(ctx)
    elapsed = time.monotonic() - start

    assert elapsed < 1.0  # bounded by wait_for's 0.05s timeout, not the model's 5s sleep
    assert isinstance(result, Transition)
    assert result.to == "chat_reply"  # TK-222, Q-110(d): the mouth now hops through chat_reply
    _text, _item_id, _item_kind, degraded = composed_output_from_artifact_data(result.output.data)
    assert degraded is True


# --- empty/whitespace-only response text also degrades --------------------------------------------


@pytest.mark.parametrize("blank_text", [None, "", "   "])
async def test_empty_or_whitespace_response_text_degrades(blank_text: str | None) -> None:
    model = FakeModel(
        response=ModelResponse(text=blank_text, model_id="deepseek-chat", finish_reason="stop")
    )
    ctx = _ctx(model)
    stage = ComposeStage(config=_config(), template_composer=TemplateComposer())

    result = await stage.run(ctx)

    assert isinstance(result, Transition)
    assert result.to == "chat_reply"  # TK-222, Q-110(d): the mouth now hops through chat_reply
    text, _item_id, _item_kind, degraded = composed_output_from_artifact_data(result.output.data)
    assert degraded is True
    assert text == TemplateComposer().render(_ITEM_KIND, _PAYLOAD)


# --- BudgetExceeded-type error degrades (budget POLICY is TK-9's; the mouth never raises) ---------


async def test_budget_exceeded_error_degrades_not_raises() -> None:
    model = FakeModel(raises=BudgetExceededError("call ceiling reached"))
    ctx = _ctx(model)
    stage = ComposeStage(config=_config(), template_composer=TemplateComposer())

    result = await stage.run(ctx)

    assert isinstance(result, Transition)
    assert result.to == "chat_reply"  # TK-222, Q-110(d): the mouth now hops through chat_reply
    text, _item_id, _item_kind, degraded = composed_output_from_artifact_data(result.output.data)
    assert degraded is True
    assert text == TemplateComposer().render(_ITEM_KIND, _PAYLOAD)


# --- AC3: ConfigurationError at CONSTRUCTION, not first call -------------------------------------


def test_ac3_blank_deepseek_api_key_raises_configuration_error_at_construction() -> None:
    blank_config = _config(api_key="")

    with pytest.raises(ConfigurationError):
        ComposeStage(config=blank_config, template_composer=TemplateComposer())


# --- TK-10 whitespace RIDER (Q-51/v0.47): a whitespace-only key must ALSO raise at construction ---


def test_whitespace_only_deepseek_api_key_raises_configuration_error_at_construction() -> None:
    whitespace_config = _config(api_key="   ")

    with pytest.raises(ConfigurationError):
        ComposeStage(config=whitespace_config, template_composer=TemplateComposer())


# --- ComposeStage touches no ctx member beyond model/last_output/clock ---------------------------


async def test_compose_stage_touches_no_ctx_member_beyond_model_last_output_and_clock() -> None:
    model = FakeModel(
        response=ModelResponse(text="phrased!", model_id="deepseek-chat", finish_reason="stop")
    )
    ctx = _ctx(model)
    stage = ComposeStage(config=_config(), template_composer=TemplateComposer())

    result = await stage.run(ctx)

    assert isinstance(result, Transition)
    assert result.to == "chat_reply"  # TK-222, Q-110(d): the mouth now hops through chat_reply


# --- TK-222, Q-110(d): ComposeStage declares "chat_reply" as its one edge -----------------------


def test_compose_stage_transitions_declares_chat_reply_as_its_only_edge() -> None:
    stage = ComposeStage(config=_config(), template_composer=TemplateComposer())
    assert stage.transitions == ("chat_reply",)


# --- TK-194: assistant name threads into the system instruction only -----------------------------


async def test_tk194_default_assistant_name_renders_in_system_instruction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)  # TK-202 hermeticity: no real .env can leak an override in
    model = FakeModel(
        response=ModelResponse(text="phrased!", model_id="deepseek-chat", finish_reason="stop")
    )
    ctx = _ctx(model)
    stage = ComposeStage(config=_config(), template_composer=TemplateComposer())

    await stage.run(ctx)

    system_msg, _user_msg = model.calls[0]
    assert system_msg.content.startswith("You are Steward, a quiet steward")


async def test_tk194_configured_assistant_name_renders_in_system_instruction_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)  # TK-202 hermeticity: no real .env can leak an override in
    config = WombatConfig(
        deepseek_api_key="sk-test",
        deepseek_base_url="https://api.deepseek.com",
        wombat_assistant_name="Marvin",
    )
    model = FakeModel(
        response=ModelResponse(text="phrased!", model_id="deepseek-chat", finish_reason="stop")
    )
    ctx = _ctx(model)
    stage = ComposeStage(config=config, template_composer=TemplateComposer())

    await stage.run(ctx)

    system_msg, user_msg = model.calls[0]
    assert "Marvin" in system_msg.content
    # Structural non-goal: the name is display/persona only -- name-free everywhere else.
    assert "Marvin" not in user_msg.content


# --- TK-209: OPTIONAL live_persona renders at RENDER time and hot-applies between turns ----------


async def test_tk209_no_live_persona_preserves_the_frozen_default_instruction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)  # TK-202 hermeticity: no real .env can leak an override in
    model = FakeModel(
        response=ModelResponse(text="phrased!", model_id="deepseek-chat", finish_reason="stop")
    )
    ctx = _ctx(model)
    # live_persona defaults to None.
    stage = ComposeStage(config=_config(), template_composer=TemplateComposer())

    await stage.run(ctx)

    system_msg, _user_msg = model.calls[0]
    assert system_msg.content == (
        "You are Steward, a quiet steward. Phrase this one item for the user in one terse, "
        "calm line. No preamble."
    )


async def test_tk209_live_persona_renders_at_run_time_and_hot_applies_between_turns(
    tmp_path: Path,
) -> None:
    live_persona = LivePersona(
        DEFAULT_MATRIX, "Steward", settings_path=str(tmp_path / "wombat.settings.json")
    )
    stage = ComposeStage(
        config=_config(), template_composer=TemplateComposer(), live_persona=live_persona
    )

    model_one = FakeModel(
        response=ModelResponse(text="phrased!", model_id="deepseek-chat", finish_reason="stop")
    )
    await stage.run(_ctx(model_one))
    first_system_msg, _ = model_one.calls[0]

    dry_matrix = PersonaMatrix(
        brevity=DEFAULT_MATRIX.brevity,
        warmth=DEFAULT_MATRIX.warmth,
        directness=DEFAULT_MATRIX.directness,
        humor=Humor.DRY,
        proactivity=DEFAULT_MATRIX.proactivity,
    )
    live_persona.set(dry_matrix)  # between two turns — no restart, no new stage instance

    model_two = FakeModel(
        response=ModelResponse(text="phrased!", model_id="deepseek-chat", finish_reason="stop")
    )
    await stage.run(_ctx(model_two))
    second_system_msg, _ = model_two.calls[0]

    # The FIRST turn still rendered under DEFAULT_MATRIX (no restart needed to prove that).
    assert first_system_msg.content == (
        "You are Steward, a quiet steward. Phrase this one item for the user in one terse, "
        "calm line. No preamble."
    )
    # The SECOND turn, rendered AFTER set(), picks up the new matrix with zero restart.
    assert second_system_msg.content != first_system_msg.content


# --- wire round-trips: json.dumps + inverse must be lossless (Q-49 regressions) -------------------


def test_compose_request_artifact_data_is_json_native_and_round_trips() -> None:
    data = compose_request_to_artifact_data(_ITEM_ID, ItemKind.DRAFT, {"subject": "hi", "n": 1})

    serialized = json.dumps(data)
    assert compose_request_from_artifact_data(json.loads(serialized)) == (
        _ITEM_ID,
        ItemKind.DRAFT,
        {"subject": "hi", "n": 1},
    )


def test_composed_output_artifact_data_is_json_native_and_round_trips() -> None:
    data = composed_output_to_artifact_data("hello there", _ITEM_ID, ItemKind.REFLECTION, True)

    serialized = json.dumps(data)
    assert composed_output_from_artifact_data(json.loads(serialized)) == (
        "hello there",
        _ITEM_ID,
        ItemKind.REFLECTION,
        True,
    )


# --- TemplateComposer.render is pure/deterministic ------------------------------------------------


def test_template_composer_render_is_pure_and_deterministic() -> None:
    composer = TemplateComposer()
    payload = {"subject": "Renewal notice", "sender": "billing@acme.com"}

    first = composer.render(ItemKind.GENERIC, payload)
    second = composer.render(ItemKind.GENERIC, dict(payload))

    assert first == second
    assert isinstance(first, str)
    assert first != ""
