"""TK-267 — SpeechShapeStage acceptance criteria (DEC-55).

All PURE: no Postgres, no real network. ``support.stage_context_fake`` is importable via the
``pythonpath = ["tests"]`` pytest setting (mirrors ``tests/unit/test_compose_stage.py`` and
``tests/stages/test_chat_reply.py``).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from cogworx.claims.provenance import Artifact, Provenance
from cogworx.loop.result import Transition
from cogworx.model.base import ModelResponse, Usage

from tests.support.stage_context_fake import FakeModel, StageContextFake
from wombat.config import ConfigurationError, WombatConfig
from wombat.gate.models import ItemKind
from wombat.persona.builder import Mouth
from wombat.persona.expression import guard_suffix
from wombat.stages.artifacts import (
    COMPOSED_OUTPUT,
    SPEECH_OUTPUT,
    composed_output_to_artifact_data,
    speech_output_from_artifact_data,
    speech_output_to_artifact_data,
)
from wombat.stages.speech_shape import SpeechShapeStage, _shape_speech_text

_FIXED_NOW = datetime(2026, 7, 19, 12, 0, 0, tzinfo=UTC)
_ITEM_ID = "i-1"
_ITEM_KIND = ItemKind.GENERIC
_COMPOSED_TEXT = "**Important**: check [this link](https://example.com) for details. #urgent"


def _config(api_key: str = "sk-test") -> WombatConfig:
    return WombatConfig(deepseek_api_key=api_key, deepseek_base_url="https://api.deepseek.com")


def _compose_output_artifact(
    text: str = _COMPOSED_TEXT, *, held_chat: bool = False, voice_turn: bool = False
) -> Artifact:
    return Artifact(
        kind=COMPOSED_OUTPUT,
        produced_by="compose",
        provenance=Provenance(source="system", confidence=1.0, recorded_at=_FIXED_NOW),
        data=composed_output_to_artifact_data(
            text, _ITEM_ID, _ITEM_KIND, False, held_chat=held_chat, voice_turn=voice_turn
        ),
    )


class _Unset:
    """A sentinel distinguishing "caller didn't pass compose_output" from "explicitly None"."""


_UNSET = _Unset()


def _ctx(
    model: FakeModel | None = None, *, compose_output: Artifact | None | _Unset = _UNSET
) -> StageContextFake:
    resolved: Artifact | None = (
        _compose_output_artifact() if isinstance(compose_output, _Unset) else compose_output
    )
    return StageContextFake(
        now_fn=lambda: _FIXED_NOW,
        last_output_map={"compose": resolved},
        model_fake=model,
    )


def _response(text: str | None) -> ModelResponse:
    return ModelResponse(
        text=text,
        model_id="deepseek-chat",
        finish_reason="stop",
        usage=Usage(prompt_tokens=10, completion_tokens=5),
    )


# --- name/transitions ----------------------------------------------------------------------------


def test_stage_declares_name_and_speak_as_its_only_edge() -> None:
    stage = SpeechShapeStage(config=_config(), voice_enabled=False, adapter_present=False)
    assert stage.name == "speech_shape"
    assert stage.transitions == ("speak",)


# --- AC1: voice on + adapter + fake model summary -> exactly one model call, clean speech text ----


async def test_ac1_voice_on_and_adapter_calls_model_once_and_carries_the_summary() -> None:
    model = FakeModel(response=_response("You have a new alert about your account."))
    stage = SpeechShapeStage(config=_config(), voice_enabled=True, adapter_present=True)
    ctx = _ctx(model)

    result = await stage.run(ctx)

    assert isinstance(result, Transition)
    assert result.to == "speak"
    assert result.output.kind == SPEECH_OUTPUT
    item_id, item_kind, text, degraded = speech_output_from_artifact_data(result.output.data)
    assert item_id == _ITEM_ID
    assert item_kind is _ITEM_KIND
    assert text == "You have a new alert about your account."
    assert degraded is False

    assert len(model.calls) == 1
    system_msg, user_msg = model.calls[0]
    assert system_msg.role == "system"
    assert user_msg.role == "user"
    # the fixed prompt carries the guard suffix verbatim (Mouth.COMPOSE's -- no fifth mouth)
    assert system_msg.content.endswith(guard_suffix(Mouth.COMPOSE))
    # the model sees the FULL composed text (it summarizes it), never sees anything else
    assert user_msg.content == _COMPOSED_TEXT


# --- DEC-57/TK-272: held_chat=True takes the EXACT voice-off pass-through, zero model calls -------


async def test_held_chat_is_a_zero_model_call_pass_through_even_with_voice_fully_wired() -> None:
    model = FakeModel(response=_response("this would have been the summary"))
    stage = SpeechShapeStage(config=_config(), voice_enabled=True, adapter_present=True)
    ctx = _ctx(model, compose_output=_compose_output_artifact(held_chat=True))

    result = await stage.run(ctx)

    assert isinstance(result, Transition)
    assert result.to == "speak"
    item_id, item_kind, text, degraded = speech_output_from_artifact_data(result.output.data)
    assert item_id == _ITEM_ID
    assert item_kind is _ITEM_KIND
    assert text is None
    assert degraded is False  # quiet-by-design, never a degraded outcome
    assert model.calls == []  # ZERO model calls


# --- TK-279 (DEC-60b): held_chat AND voice_turn falls through to the real shaping call ------------


async def test_held_voice_turn_falls_through_to_real_shaping_call() -> None:
    model = FakeModel(response=_response("You have a new alert about your account."))
    stage = SpeechShapeStage(config=_config(), voice_enabled=True, adapter_present=True)
    ctx = _ctx(model, compose_output=_compose_output_artifact(held_chat=True, voice_turn=True))

    result = await stage.run(ctx)

    assert isinstance(result, Transition)
    assert result.to == "speak"
    _item_id, _item_kind, text, degraded = speech_output_from_artifact_data(result.output.data)
    assert text == "You have a new alert about your account."
    assert degraded is False
    assert len(model.calls) == 1  # exactly ONE shaping call


async def test_held_voice_turn_but_voice_disabled_stays_a_quiet_pass_through() -> None:
    model = FakeModel(response=_response("should never be used"))
    stage = SpeechShapeStage(config=_config(), voice_enabled=False, adapter_present=True)
    ctx = _ctx(model, compose_output=_compose_output_artifact(held_chat=True, voice_turn=True))

    result = await stage.run(ctx)

    assert model.calls == []
    assert isinstance(result, Transition)
    _item_id, _item_kind, text, degraded = speech_output_from_artifact_data(result.output.data)
    assert text is None
    assert degraded is False  # honest pass-through, never marked degraded


async def test_held_voice_turn_shaping_failure_degrades_never_falls_back_to_composed_text() -> None:
    model = FakeModel(raises=ConnectionError("503 Service Unavailable"))
    stage = SpeechShapeStage(config=_config(), voice_enabled=True, adapter_present=True)
    ctx = _ctx(model, compose_output=_compose_output_artifact(held_chat=True, voice_turn=True))

    result = await stage.run(ctx)

    assert isinstance(result, Transition)
    _item_id, _item_kind, text, degraded = speech_output_from_artifact_data(result.output.data)
    assert text is None
    assert degraded is True
    assert text != _COMPOSED_TEXT


# --- AC2: the no-placebo validator, one case per enumerated closed token class + overlong ---------


@pytest.mark.parametrize(
    "raw_text",
    [
        pytest.param("**bold text**", id="bold"),
        pytest.param("*italic text*", id="italic-asterisk"),
        pytest.param("_italic text_", id="italic-underscore"),
        pytest.param("# a heading", id="heading"),
        pytest.param("some `code` here", id="backtick"),
        pytest.param("see [this link](https://example.com)", id="markdown-link"),
        pytest.param("visit https://example.com now", id="raw-url"),
        pytest.param("- a bullet point", id="bullet-list-marker"),
        pytest.param("1. a numbered item", id="numbered-list-marker"),
        pytest.param("x" * 401, id="overlong"),
    ],
)
def test_ac2_validator_rejects_every_forbidden_token_class_and_overlong(raw_text: str) -> None:
    assert _shape_speech_text(raw_text) is None


def test_ac2_validator_accepts_plain_spoken_text() -> None:
    clean = "You have a new alert about your account."
    assert _shape_speech_text(clean) == clean


# --- TK-317 (DEC-69a): anchored one-shot leading speaker-label strip ------------------------------


@pytest.mark.parametrize(
    ("raw_text", "expected"),
    [
        pytest.param("wombat: answer is here", "answer is here", id="lowercase-wombat"),
        pytest.param(
            "Wombat: The meeting is at 3pm.", "The meeting is at 3pm.", id="capitalized-wombat"
        ),
        pytest.param("Steward: on my way", "on my way", id="steward"),
        pytest.param("assistant: plain reply", "plain reply", id="assistant"),
    ],
)
def test_ac1_leading_speaker_label_is_stripped_and_remainder_is_byte_identical(
    raw_text: str, expected: str
) -> None:
    assert _shape_speech_text(raw_text) == expected


def test_ac2b_non_leading_colon_is_never_touched() -> None:
    # the colon here sits well past the ~32-char leading-label window, so it is left alone.
    text = "This sentence runs on for quite a while before its only colon: untouched"
    assert _shape_speech_text(text) == text


def test_ac2b_double_label_strips_exactly_one() -> None:
    assert _shape_speech_text("A: B: rest") == "B: rest"


def test_ac3b_bold_leading_label_still_rejects_to_none() -> None:
    # DEC-55f markdown reject-to-silence posture stands: '**' isn't a letter, so the TK-317 strip
    # never fires here, and the unmodified forbidden loop rejects the bold marker as before.
    assert _shape_speech_text("**Wombat**: bold label") is None


def test_ac4b_label_prefixed_body_fits_cap_only_after_the_strip() -> None:
    # the strip must run BEFORE the length check so the remaining body keeps the full budget.
    body = "x" * 400
    assert _shape_speech_text(f"Wombat: {body}") == body
    assert len(f"Wombat: {body}") > 400  # the raw text alone would have failed the cap


async def test_ac1_stage_carries_a_label_stripped_summary_through_run() -> None:
    model = FakeModel(response=_response("Wombat: The meeting is at 3pm."))
    stage = SpeechShapeStage(config=_config(), voice_enabled=True, adapter_present=True)
    ctx = _ctx(model)

    result = await stage.run(ctx)

    assert isinstance(result, Transition)
    _item_id, _item_kind, text, degraded = speech_output_from_artifact_data(result.output.data)
    assert text == "The meeting is at 3pm."
    assert degraded is False


# --- TK-303 (DEC-67e): max_chars is injectable, defaulting to _MAX_SPEECH_CHARS -------------------


def test_ac2_default_max_chars_keeps_the_400_char_behavior() -> None:
    from wombat.stages.speech_shape import _MAX_SPEECH_CHARS

    at_limit = "x" * _MAX_SPEECH_CHARS
    over_limit = "x" * (_MAX_SPEECH_CHARS + 1)
    assert _shape_speech_text(at_limit) == at_limit
    assert _shape_speech_text(over_limit) is None


def test_ac2_injected_max_chars_800_passes_a_600_char_reply_whole() -> None:
    text = "x" * 600
    assert _shape_speech_text(text, max_chars=800) == text
    # still rejected at the default 400-char bound
    assert _shape_speech_text(text) is None


async def test_ac2_stage_with_max_chars_800_carries_a_600_char_model_reply_whole() -> None:
    reply = "x" * 600
    model = FakeModel(response=_response(reply))
    stage = SpeechShapeStage(
        config=_config(), voice_enabled=True, adapter_present=True, max_chars=800
    )
    ctx = _ctx(model)

    result = await stage.run(ctx)

    assert isinstance(result, Transition)
    _item_id, _item_kind, text, degraded = speech_output_from_artifact_data(result.output.data)
    assert text == reply
    assert degraded is False


def test_stage_default_max_chars_preserves_the_400_char_ctor_default() -> None:
    from wombat.stages.speech_shape import _MAX_SPEECH_CHARS

    stage = SpeechShapeStage(config=_config(), voice_enabled=False, adapter_present=False)
    assert stage._max_chars == _MAX_SPEECH_CHARS


async def test_ac2_forbidden_model_text_degrades_to_no_speech_and_never_reaches_tts() -> None:
    model = FakeModel(response=_response("**Important**: check https://example.com"))
    stage = SpeechShapeStage(config=_config(), voice_enabled=True, adapter_present=True)
    ctx = _ctx(model)

    result = await stage.run(ctx)

    assert isinstance(result, Transition)
    assert result.to == "speak"
    _item_id, _item_kind, text, degraded = speech_output_from_artifact_data(result.output.data)
    assert text is None
    assert degraded is True


# --- AC3: model raising (exception/timeout) -> no speech, degraded, never composed text -----------


async def test_ac3_model_exception_degrades_to_no_speech_never_composed_text() -> None:
    model = FakeModel(raises=ConnectionError("503 Service Unavailable"))
    stage = SpeechShapeStage(config=_config(), voice_enabled=True, adapter_present=True)
    ctx = _ctx(model)

    result = await stage.run(ctx)

    assert isinstance(result, Transition)
    assert result.to == "speak"
    _item_id, _item_kind, text, degraded = speech_output_from_artifact_data(result.output.data)
    assert text is None
    assert degraded is True
    assert text != _COMPOSED_TEXT  # DEC-55c: never falls back to the composed text


async def test_ac3_model_timeout_degrades_to_no_speech_within_bound() -> None:
    import time

    model = FakeModel(sleep_seconds=5.0)
    stage = SpeechShapeStage(
        config=_config(), voice_enabled=True, adapter_present=True, timeout_seconds=0.05
    )
    ctx = _ctx(model)

    start = time.monotonic()
    result = await stage.run(ctx)
    elapsed = time.monotonic() - start

    assert elapsed < 1.0
    assert isinstance(result, Transition)
    _item_id, _item_kind, text, degraded = speech_output_from_artifact_data(result.output.data)
    assert text is None
    assert degraded is True


async def test_ac3_blank_model_response_degrades_to_no_speech() -> None:
    model = FakeModel(response=_response(None))
    stage = SpeechShapeStage(config=_config(), voice_enabled=True, adapter_present=True)
    ctx = _ctx(model)

    result = await stage.run(ctx)

    assert isinstance(result, Transition)
    _item_id, _item_kind, text, degraded = speech_output_from_artifact_data(result.output.data)
    assert text is None
    assert degraded is True


# --- AC4: voice off, and separately no adapter -> ZERO model calls, pass-through, non-degraded ----


async def test_ac4_voice_disabled_makes_zero_model_calls_and_is_a_pass_through() -> None:
    model = FakeModel(response=_response("should never be used"))
    stage = SpeechShapeStage(config=_config(), voice_enabled=False, adapter_present=True)
    ctx = _ctx(model)

    result = await stage.run(ctx)

    assert model.calls == []
    assert isinstance(result, Transition)
    assert result.to == "speak"
    _item_id, _item_kind, text, degraded = speech_output_from_artifact_data(result.output.data)
    assert text is None
    assert degraded is False


async def test_ac4_no_adapter_makes_zero_model_calls_and_is_a_pass_through() -> None:
    model = FakeModel(response=_response("should never be used"))
    stage = SpeechShapeStage(config=_config(), voice_enabled=True, adapter_present=False)
    ctx = _ctx(model)

    result = await stage.run(ctx)

    assert model.calls == []
    assert isinstance(result, Transition)
    assert result.to == "speak"
    _item_id, _item_kind, text, degraded = speech_output_from_artifact_data(result.output.data)
    assert text is None
    assert degraded is False


def test_ac4_voice_disabled_never_requires_a_deepseek_key_at_construction() -> None:
    # voice off -> the mouth will never be called -> a blank key must not fail construction.
    blank_config = _config(api_key="")
    SpeechShapeStage(config=blank_config, voice_enabled=False, adapter_present=False)


def test_ac4_voice_on_and_adapter_present_requires_a_deepseek_key_at_construction() -> None:
    blank_config = _config(api_key="")
    with pytest.raises(ConfigurationError):
        SpeechShapeStage(config=blank_config, voice_enabled=True, adapter_present=True)


# --- no compose output yet raises (mirrors ComposeStage/ChatReplyStage/SpeakSink's own posture) ---


async def test_no_compose_output_yet_raises_runtime_error() -> None:
    stage = SpeechShapeStage(config=_config(), voice_enabled=True, adapter_present=True)
    ctx = _ctx(compose_output=None)

    with pytest.raises(RuntimeError):
        await stage.run(ctx)


# --- TK-9 layer 2: pre-call ceiling gate + post-call accounting -----------------------------------


class _StubLedger:
    def __init__(self, *, spent_today: int = 0, read_raises: bool = False) -> None:
        self.spent_today = spent_today
        self.read_raises = read_raises
        self.added: list[int] = []

    def tokens_spent_today(self) -> int:
        if self.read_raises:
            raise RuntimeError("ledger read boom")
        return self.spent_today

    def add_tokens(self, amount: int) -> int:
        self.added.append(amount)
        return self.spent_today + amount


async def test_layer2_pre_call_ceiling_reached_skips_the_model_call() -> None:
    model = FakeModel(response=_response("should never be used"))
    ledger = _StubLedger(spent_today=1000)
    stage = SpeechShapeStage(
        config=_config(),
        voice_enabled=True,
        adapter_present=True,
        spend_ledger=ledger,  # type: ignore[arg-type]
        daily_token_ceiling=1000,
    )
    ctx = _ctx(model)

    result = await stage.run(ctx)

    assert model.calls == []
    assert isinstance(result, Transition)
    _item_id, _item_kind, text, degraded = speech_output_from_artifact_data(result.output.data)
    assert text is None
    assert degraded is True


async def test_layer2_ledger_read_failure_fails_closed_without_calling_the_model() -> None:
    model = FakeModel(response=_response("should never be used"))
    ledger = _StubLedger(read_raises=True)
    stage = SpeechShapeStage(
        config=_config(),
        voice_enabled=True,
        adapter_present=True,
        spend_ledger=ledger,  # type: ignore[arg-type]
        daily_token_ceiling=1000,
    )
    ctx = _ctx(model)

    result = await stage.run(ctx)

    assert model.calls == []
    assert isinstance(result, Transition)
    _item_id, _item_kind, text, degraded = speech_output_from_artifact_data(result.output.data)
    assert text is None
    assert degraded is True


async def test_layer2_successful_call_accounts_tokens_on_the_ledger() -> None:
    model = FakeModel(response=_response("A clean spoken summary."))
    ledger = _StubLedger(spent_today=0)
    stage = SpeechShapeStage(
        config=_config(),
        voice_enabled=True,
        adapter_present=True,
        spend_ledger=ledger,  # type: ignore[arg-type]
        daily_token_ceiling=1000,
    )
    ctx = _ctx(model)

    result = await stage.run(ctx)

    assert isinstance(result, Transition)
    _item_id, _item_kind, text, degraded = speech_output_from_artifact_data(result.output.data)
    assert text == "A clean spoken summary."
    assert degraded is False
    assert ledger.added == [15]  # 10 prompt + 5 completion


# --- wire round-trip (Q-49) -------------------------------------------------------------------


def test_speech_output_artifact_data_round_trips_including_none_text() -> None:
    data = speech_output_to_artifact_data(_ITEM_ID, _ITEM_KIND, "hello", False)
    assert speech_output_from_artifact_data(data) == (_ITEM_ID, _ITEM_KIND, "hello", False)

    degraded_data = speech_output_to_artifact_data(_ITEM_ID, _ITEM_KIND, None, True)
    assert speech_output_from_artifact_data(degraded_data) == (_ITEM_ID, _ITEM_KIND, None, True)
