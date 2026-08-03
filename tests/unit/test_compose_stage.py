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
from wombat.persona.builder import Mouth, instruction_for
from wombat.persona.capabilities import CAPABILITY_CHARTER
from wombat.persona.live import LivePersona
from wombat.persona.matrix import DEFAULT_MATRIX, Humor, PersonaMatrix
from wombat.stages.artifacts import (
    COMPOSE_REQUEST,
    COMPOSED_OUTPUT,
    compose_request_from_artifact_data,
    compose_request_to_artifact_data,
    compose_request_voice_turn_from_artifact_data,
    composed_output_from_artifact_data,
    composed_output_held_chat_from_artifact_data,
    composed_output_to_artifact_data,
    composed_output_voice_turn_from_artifact_data,
)
from wombat.stages.compose import _GROUNDING_ONLY_KEYS, ComposeStage

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


# --- TK-293 (DEC-65b) helpers: a chat-kind compose_dispatch artifact, typed or voice --------------


def _chat_compose_request_artifact(*, voice_turn: bool = False) -> Artifact:
    return Artifact(
        kind=COMPOSE_REQUEST,
        produced_by="compose_dispatch",
        provenance=Provenance(source="system", confidence=1.0, recorded_at=_FIXED_NOW),
        data=compose_request_to_artifact_data(
            _ITEM_ID, ItemKind.CHAT, _PAYLOAD, voice_turn=voice_turn
        ),
    )


def _ctx_with(model: FakeModel, artifact: Artifact) -> StageContextFake:
    return StageContextFake(
        now_fn=lambda: _FIXED_NOW,
        last_output_map={"compose_dispatch": artifact},
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

    # NONE of the gate/queue-internal keys appear in the payload-derived user message. (TK-284,
    # v2.143: the system message is excluded here — the capability charter legitimately contains
    # "action" twice, and the system message's exact content is covered by the byte-equality pins
    # elsewhere in this file.)
    for internal_key in _INTERNAL_KEYS:
        assert internal_key not in user_msg.content


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


# --- TK-283 (DEC-61): the boot-contention repro -- a call slower than the OLD 2.0s default but
# under the injected mouth_model_timeout_seconds tunable returns real phrased text, NOT a
# template (runtime-20260720-183045.log: boot-time faster-whisper CPU decode saturated cores
# and a HEALTHY DeepSeek call got cut at the old hard-coded 2.0s default). -------------------------


async def test_ac_boot_contention_slow_healthy_call_returns_phrased_text_not_degraded() -> None:
    model = FakeModel(
        response=ModelResponse(text="phrased!", model_id="deepseek-chat", finish_reason="stop"),
        sleep_seconds=2.5,  # longer than the OLD 2.0s default, well under the injected tunable
    )
    ctx = _ctx(model)
    stage = ComposeStage(
        config=_config(), template_composer=TemplateComposer(), timeout_seconds=10.0
    )

    result = await stage.run(ctx)

    assert isinstance(result, Transition)
    text, _item_id, _item_kind, degraded = composed_output_from_artifact_data(result.output.data)
    assert degraded is False
    assert text == "phrased!"


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
        "calm line. No preamble. " + CAPABILITY_CHARTER
    )


async def test_tk209_live_persona_renders_at_run_time_and_hot_applies_between_turns() -> None:
    live_persona = LivePersona(DEFAULT_MATRIX, "Steward")  # store-less (TK-243), fully in-memory
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
        "calm line. No preamble. " + CAPABILITY_CHARTER
    )
    # The SECOND turn, rendered AFTER set(), picks up the new matrix with zero restart.
    assert second_system_msg.content != first_system_msg.content


# --- TK-293 (DEC-65b): ComposeStage selects Mouth.CHAT by item_kind -------------------------------


async def test_ac1_chat_turn_typed_and_voice_with_live_persona_uses_mouth_chat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)  # TK-202 hermeticity: no real .env can leak an override in
    live_persona = LivePersona(DEFAULT_MATRIX, "Steward", user_name="Jim")
    stage = ComposeStage(
        config=_config(), template_composer=TemplateComposer(), live_persona=live_persona
    )
    expected = instruction_for(Mouth.CHAT, DEFAULT_MATRIX, "Steward", user_name="Jim")
    assert expected.endswith(CAPABILITY_CHARTER)

    typed_model = FakeModel(
        response=ModelResponse(text="hey!", model_id="deepseek-chat", finish_reason="stop")
    )
    await stage.run(_ctx_with(typed_model, _chat_compose_request_artifact()))
    typed_system_msg, _ = typed_model.calls[0]
    assert typed_system_msg.content == expected

    voice_model = FakeModel(
        response=ModelResponse(text="hey!", model_id="deepseek-chat", finish_reason="stop")
    )
    await stage.run(_ctx_with(voice_model, _chat_compose_request_artifact(voice_turn=True)))
    voice_system_msg, _ = voice_model.calls[0]
    assert voice_system_msg.content == expected


async def test_ac1_non_chat_turn_with_live_persona_still_uses_mouth_compose(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)  # TK-202 hermeticity: no real .env can leak an override in
    live_persona = LivePersona(DEFAULT_MATRIX, "Steward", user_name="Jim")
    stage = ComposeStage(
        config=_config(), template_composer=TemplateComposer(), live_persona=live_persona
    )
    expected = instruction_for(Mouth.COMPOSE, DEFAULT_MATRIX, "Steward")

    model = FakeModel(
        response=ModelResponse(text="phrased!", model_id="deepseek-chat", finish_reason="stop")
    )
    await stage.run(_ctx(model))  # a generic (non-chat) compose_dispatch artifact

    system_msg, _ = model.calls[0]
    assert system_msg.content == expected


async def test_ac2_chat_turn_without_live_persona_uses_frozen_chat_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)  # TK-202 hermeticity: no real .env can leak an override in
    config = WombatConfig(
        deepseek_api_key="sk-test",
        deepseek_base_url="https://api.deepseek.com",
        wombat_assistant_name="Marvin",
        wombat_user_name="Jim",
    )
    stage = ComposeStage(config=config, template_composer=TemplateComposer())
    expected = instruction_for(Mouth.CHAT, DEFAULT_MATRIX, "Marvin", user_name="Jim")

    model = FakeModel(
        response=ModelResponse(text="hey!", model_id="deepseek-chat", finish_reason="stop")
    )
    await stage.run(_ctx_with(model, _chat_compose_request_artifact()))

    system_msg, _ = model.calls[0]
    assert system_msg.content == expected


async def test_ac2_chat_turn_without_live_persona_or_configured_user_name_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)  # TK-202 hermeticity: no real .env can leak an override in
    stage = ComposeStage(config=_config(), template_composer=TemplateComposer())
    expected = instruction_for(Mouth.CHAT, DEFAULT_MATRIX, "Steward")

    model = FakeModel(
        response=ModelResponse(text="hey!", model_id="deepseek-chat", finish_reason="stop")
    )
    await stage.run(_ctx_with(model, _chat_compose_request_artifact()))

    system_msg, _ = model.calls[0]
    assert system_msg.content == expected
    assert "the user" in system_msg.content


async def test_ac4_chat_turn_degrade_renders_template_line_exactly_as_today() -> None:
    model = FakeModel(raises=ConnectionError("503 Service Unavailable"))
    stage = ComposeStage(config=_config(), template_composer=TemplateComposer())

    result = await stage.run(_ctx_with(model, _chat_compose_request_artifact()))

    assert isinstance(result, Transition)
    text, _item_id, item_kind, degraded = composed_output_from_artifact_data(result.output.data)
    assert degraded is True
    assert item_kind is ItemKind.CHAT
    assert text == TemplateComposer().render(ItemKind.CHAT, _PAYLOAD)


# --- REPAIR (batch review, TK-293 x TK-296): a chat degrade must not leak the grounding-only
# fields context_hook stamps (known_user_context/context_calendar_today/context_recent_email/
# replying_to/current_activity/current_body_state) verbatim to the user, even though the
# model-facing prompt still needs to see them (voice is shielded downstream by SpeechShapeStage's
# DEC-55c never-verbatim; typed chat is not — ChatReplyStage resolves this stage's degrade text
# straight to the chat pane, unshaped). current_activity (TK-311, DEC-68(d)(1)) and
# current_body_state (TK-347, R7) are folded into the SAME fixture/test rather than new ones — both
# are stamped by the SAME closure and must pass through the SAME filter. --------------------------

_GROUNDED_CHAT_PAYLOAD: dict[str, Any] = {
    "text": "hey",
    "known_user_context": "Jim is allergic to shellfish",
    "context_calendar_today": "09:00 Therapy appointment",
    "context_recent_email": "Re: divorce paperwork - lawyer@example.com",
    "replying_to": "sure, want me to book it?",
    "current_activity": "notepad.exe - Untitled - Notepad",
    "current_body_state": "resting_hr_daily: bpm=62",
}


def _grounded_chat_compose_request_artifact() -> Artifact:
    return Artifact(
        kind=COMPOSE_REQUEST,
        produced_by="compose_dispatch",
        provenance=Provenance(source="system", confidence=1.0, recorded_at=_FIXED_NOW),
        data=compose_request_to_artifact_data(_ITEM_ID, ItemKind.CHAT, _GROUNDED_CHAT_PAYLOAD),
    )


async def test_repair_chat_degrade_strips_grounding_only_keys_but_prompt_keeps_them() -> None:
    model = FakeModel(raises=ConnectionError("503 Service Unavailable"))
    stage = ComposeStage(config=_config(), template_composer=TemplateComposer())

    result = await stage.run(_ctx_with(model, _grounded_chat_compose_request_artifact()))
    assert isinstance(result, Transition)

    # the model still SAW every grounding field in its prompt — the call was attempted (and
    # failed) before degrading, so the prompt-building path above is completely unaffected.
    _system_msg, user_msg = model.calls[0]
    assert "known_user_context: Jim is allergic to shellfish" in user_msg.content
    assert "context_calendar_today: 09:00 Therapy appointment" in user_msg.content
    assert "context_recent_email: Re: divorce paperwork - lawyer@example.com" in user_msg.content
    assert "replying_to: sure, want me to book it?" in user_msg.content
    # AC1 (TK-311, DEC-68(d)(1)): the one-line current_activity reaches the prompt too, within cap.
    assert "current_activity: notepad.exe - Untitled - Notepad" in user_msg.content
    # AC1 (TK-347, R7): the one-line current_body_state reaches the prompt too.
    assert "current_body_state: resting_hr_daily: bpm=62" in user_msg.content

    # but the DEGRADED reply text — what ChatReplyStage resolves verbatim to the typed chat pane
    # — never echoes any grounding field back.
    text, _item_id, item_kind, degraded = composed_output_from_artifact_data(result.output.data)
    assert degraded is True
    assert item_kind is ItemKind.CHAT
    assert "Jim is allergic to shellfish" not in text
    assert "Therapy appointment" not in text
    assert "divorce paperwork" not in text
    assert "sure, want me to book it?" not in text
    assert "notepad.exe" not in text
    # AC5 (TK-347, R7): the degrade template never echoes current_body_state verbatim either.
    assert "resting_hr_daily" not in text
    assert "bpm=62" not in text
    # the item's own genuine content still renders — the fix isn't a black hole, just a filter.
    assert text == TemplateComposer().render(ItemKind.CHAT, {"text": "hey"})


# --- TK-347 (R7): current_body_state merged into the SAME asr_context_hook closure -----------


async def test_ac1_current_body_state_renders_into_prompt_with_no_other_payload_change() -> None:
    """AC1: a chat payload carrying ``current_body_state`` (as the SAME shared asr_context_hook
    closure would stamp it) renders into the model prompt via format_payload_fields exactly like
    every other grounding field — and the ONLY difference from the baseline chat payload's own
    prompt is that one added ``current_body_state: ...`` field."""
    model = FakeModel(
        response=ModelResponse(text="phrased!", model_id="deepseek-chat", finish_reason="stop")
    )
    stage = ComposeStage(config=_config(), template_composer=TemplateComposer())

    await stage.run(_ctx_with(model, _chat_compose_request_artifact()))
    _, baseline_user_msg = model.calls[0]

    grounded_payload = {**_PAYLOAD, "current_body_state": "resting_hr_daily: bpm=62"}
    grounded_artifact = Artifact(
        kind=COMPOSE_REQUEST,
        produced_by="compose_dispatch",
        provenance=Provenance(source="system", confidence=1.0, recorded_at=_FIXED_NOW),
        data=compose_request_to_artifact_data(_ITEM_ID, ItemKind.CHAT, grounded_payload),
    )
    await stage.run(_ctx_with(model, grounded_artifact))
    _, grounded_user_msg = model.calls[1]

    assert "current_body_state: resting_hr_daily: bpm=62" in grounded_user_msg.content
    # no other payload change: stripping exactly the one rendered field (plus its "; " join)
    # recovers the baseline prompt byte-for-byte.
    stripped = grounded_user_msg.content.replace(
        "current_body_state: resting_hr_daily: bpm=62; ", ""
    )
    assert stripped == baseline_user_msg.content


# --- TK-298 (ISS-30 fold-in): pin _GROUNDING_ONLY_KEYS to the exact set bootstrap.py's
# asr_context_hook closure can stamp -----------------------------------------------------------


def test_grounding_only_keys_pinned_to_the_exact_context_hook_stampable_set() -> None:
    """``_GROUNDING_ONLY_KEYS`` must equal EXACTLY the keys ``assemble_runtime``'s shared
    ``asr_context_hook`` closure can stamp onto a chat payload: ``replying_to`` (TK-289),
    ``known_user_context``/``context_calendar_today``/``context_recent_email`` (TK-290/TK-296),
    ``current_activity`` (TK-311, DEC-68(d)(1)), and now ``current_body_state`` (TK-347, R7) — the
    SIX-key set, grown DELIBERATELY. This pin exists precisely so a future grounding key added to
    that closure without a matching addition here fails loudly instead of silently reopening the
    v2.165 degrade leak (a grounding field dumped verbatim to the typed chat pane) — TK-311/TK-347
    both cite this test per their briefings."""

    assert frozenset(
        {
            "replying_to",
            "known_user_context",
            "context_calendar_today",
            "context_recent_email",
            "current_activity",
            "current_body_state",
        }
    ) == _GROUNDING_ONLY_KEYS


# --- wire round-trips: json.dumps + inverse must be lossless (Q-49 regressions) -------------------


def test_compose_request_artifact_data_is_json_native_and_round_trips() -> None:
    data = compose_request_to_artifact_data(_ITEM_ID, ItemKind.DRAFT, {"subject": "hi", "n": 1})

    serialized = json.dumps(data)
    assert compose_request_from_artifact_data(json.loads(serialized)) == (
        _ITEM_ID,
        ItemKind.DRAFT,
        {"subject": "hi", "n": 1},
    )
    # AC5 (TK-279, DEC-60b): voice_turn is additive — absent/omitted defaults False.
    assert compose_request_voice_turn_from_artifact_data(json.loads(serialized)) is False


def test_compose_request_voice_turn_is_additive_and_round_trips() -> None:
    """TK-279 (DEC-60b): voice_turn=True rides the compose-request wire plain-JSON-native."""
    data = compose_request_to_artifact_data(
        _ITEM_ID, ItemKind.CHAT, _PAYLOAD, held_chat=True, voice_turn=True
    )

    serialized = json.dumps(data)
    round_tripped = json.loads(serialized)
    assert compose_request_from_artifact_data(round_tripped) == (_ITEM_ID, ItemKind.CHAT, _PAYLOAD)
    assert compose_request_voice_turn_from_artifact_data(round_tripped) is True


def test_composed_output_artifact_data_is_json_native_and_round_trips() -> None:
    data = composed_output_to_artifact_data("hello there", _ITEM_ID, ItemKind.REFLECTION, True)

    serialized = json.dumps(data)
    assert composed_output_from_artifact_data(json.loads(serialized)) == (
        "hello there",
        _ITEM_ID,
        ItemKind.REFLECTION,
        True,
    )
    # AC4 (TK-272, DEC-57): held_chat is ADDITIVE — absent/omitted defaults False, never a
    # KeyError, and every caller predating TK-272 (like the positional call above) is unaffected.
    assert composed_output_held_chat_from_artifact_data(json.loads(serialized)) is False
    # AC5 (TK-279, DEC-60b): voice_turn is a SECOND additive field — same absent-defaults-False
    # posture.
    assert composed_output_voice_turn_from_artifact_data(json.loads(serialized)) is False


def test_composed_output_held_chat_is_additive_and_round_trips() -> None:
    """AC4 (TK-272, DEC-57): held_chat=True rides the composed-output wire plain-JSON-native."""
    data = composed_output_to_artifact_data(
        "quiet reply", _ITEM_ID, ItemKind.CHAT, False, held_chat=True
    )

    serialized = json.dumps(data)
    round_tripped = json.loads(serialized)
    assert composed_output_from_artifact_data(round_tripped) == (
        "quiet reply",
        _ITEM_ID,
        ItemKind.CHAT,
        False,
    )
    assert composed_output_held_chat_from_artifact_data(round_tripped) is True


def test_composed_output_voice_turn_is_additive_and_round_trips() -> None:
    """TK-279 (DEC-60b): voice_turn=True rides the composed-output wire plain-JSON-native,
    independently of held_chat."""
    data = composed_output_to_artifact_data(
        "spoken reply", _ITEM_ID, ItemKind.CHAT, False, held_chat=True, voice_turn=True
    )

    serialized = json.dumps(data)
    round_tripped = json.loads(serialized)
    assert composed_output_from_artifact_data(round_tripped) == (
        "spoken reply",
        _ITEM_ID,
        ItemKind.CHAT,
        False,
    )
    assert composed_output_held_chat_from_artifact_data(round_tripped) is True
    assert composed_output_voice_turn_from_artifact_data(round_tripped) is True


# --- TemplateComposer.render is pure/deterministic ------------------------------------------------


def test_template_composer_render_is_pure_and_deterministic() -> None:
    composer = TemplateComposer()
    payload = {"subject": "Renewal notice", "sender": "billing@acme.com"}

    first = composer.render(ItemKind.GENERIC, payload)
    second = composer.render(ItemKind.GENERIC, dict(payload))

    assert first == second
    assert isinstance(first, str)
    assert first != ""
