"""tests/integration/test_fish_expressive_arc.py — TK-329 acceptance criteria (DEC-71/DEC-72
done-bar, EP-31).

Fakes-only end-to-end proof that the Fish s2.1-pro expressive speak path composes correctly
across the seams landed by TK-326 (``FishAudioTTSAdapter``'s ``model`` header),
TK-327 (``voice.expressive``'s ``TAG_DEFINITIONS``/``ALLOWED_TAGS``/``EXPRESSIVE_FISH_MODELS``),
and TK-328 (``voice.select``'s ``build_tts_adapter_with_info``/``TTSBuildInfo`` and
``build_speech_shape_stage``'s ``expressive_tags`` threading). ZERO live network calls and ZERO
real audio playback throughout (DEF-7) — every test rides a fake ``VoiceTransport`` + fake
``AudioPlayer``, mirroring ``tests/voice/test_tts_fish.py``'s own fakes.

AC1 (expressive e2e, DEC-69/DEC-72i): ``test_expressive_e2e_tagged_reply_reaches_fish_verbatim_
pane_stays_clean`` — an allowed-tagged shaped reply rides speech_shape -> speak -> the real
``FishAudioTTSAdapter``, landing in the fake transport's body VERBATIM under the pinned
``s2.1-pro`` model header, playback firing exactly once, the composed/pane text carrying zero
bracketed tags, and the sent text independently re-proven to pass the SAME validator
(validate-then-send is structural).
AC2 (no-placebo e2e, DEC-55f blast radius): ``test_no_placebo_e2e_out_of_set_tag_degrades_before_
any_transport_contact`` — an out-of-set opening tag is rejected to silence before speech_shape
ever returns text, so ``SpeakSink`` degrades with ZERO transport calls; text delivery unaffected.
AC3 (inertness e2e, the key-gate pin): ``test_inertness_e2e_non_expressive_boots_match_pre_arc_
baseline`` — local/elevenlabs/fish-s1/fish-s2-without-key each yield
``expressive_tags=False`` via the SAME formula ``assemble_runtime`` uses
(``info.fish_primary and info.fish_model in EXPRESSIVE_FISH_MODELS``), a byte-identical
``SpeechShapeStage`` instruction, and an empty allowed-tag set that rejects any bracketed token.
DEF-18: ``test_speak_full_replies_path_structurally_free_of_expressive_instruction`` — the
``wombat_speak_full_replies`` opt-in path makes ZERO model calls regardless of
``expressive_tags``, so Fish's expressive instruction can never reach it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from cogworx.claims.provenance import Artifact, Provenance
from cogworx.loop.result import Degraded, Done, Transition
from cogworx.model.base import ModelResponse, Usage

import wombat.voice.select as select_module
from tests.support.stage_context_fake import FakeModel, StageContextFake
from wombat.config import WombatConfig
from wombat.gate.models import ItemKind
from wombat.sinks.speak import SpeakSink
from wombat.stages.artifacts import (
    COMPOSED_OUTPUT,
    composed_output_to_artifact_data,
    speech_output_from_artifact_data,
    spoken_output_from_artifact_data,
)
from wombat.stages.speech_shape import SpeechShapeStage
from wombat.voice.expressive import ALLOWED_TAGS, EXPRESSIVE_FISH_MODELS, find_disallowed_token
from wombat.voice.select import build_tts_adapter_with_info
from wombat.voice.tts import FishAudioTTSAdapter

_FIXED_NOW = datetime(2026, 7, 31, 12, 0, 0, tzinfo=UTC)
_ITEM_ID = "gate-item-fish-arc-1"
_ITEM_KIND = ItemKind.GENERIC
_COMPOSED_TEXT = "Your first meeting is at nine and nothing else needs you before then."
_WAV_BYTES = b"RIFF....WAVEfmt returned-audio"
_VOICE_ID = "voice-jims-clone"
_FISH_API_KEY = "fish-secret-arc-key"


@pytest.fixture(autouse=True)
def _no_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """TK-202/Q-103: chdir off the repo root so pydantic-settings' ``env_file=".env"`` resolution
    (relative to CWD) can never pick up the operator's populated .env — mirrors ``tests/voice/
    test_select.py``'s own ``_no_env_file`` fixture, made autouse here since every ``WombatConfig``
    built in this module must stay isolated from Jim's real voice-provider settings."""
    monkeypatch.chdir(tmp_path)


# ------------------------------------------------------------------------------------------ fakes


class _RecordingFakeTransport:
    """Records every POST and returns canned WAV bytes — never touches the network (DEF-7)."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def post(
        self,
        url: str,
        *,
        headers: dict[str, str],
        content: bytes | None = None,
        json: dict[str, object] | None = None,
        data: dict[str, str] | None = None,
        files: dict[str, tuple[str, bytes]] | None = None,
    ) -> tuple[int, bytes]:
        self.calls.append({"url": url, "headers": headers, "json": json})
        return 200, _WAV_BYTES


class _RecordingFakePlayer:
    """Records every ``play()`` call — never touches real audio hardware."""

    def __init__(self) -> None:
        self.calls: list[bytes] = []

    def play(self, wav_bytes: bytes) -> None:
        self.calls.append(wav_bytes)


class _FakeVoiceKeyStore:
    """The in-memory fake — unit tests never touch the real vault (Q-57(a))."""

    def __init__(self, *, initial: dict[str, str] | None = None) -> None:
        self._values = dict(initial or {})

    def get(self, provider: str) -> str | None:
        return self._values.get(provider)

    def set(self, provider: str, key: str) -> None:
        self._values[provider] = key

    def delete(self, provider: str) -> None:
        self._values.pop(provider, None)


class _FakeLocalTTS:
    """Stands in for ``Pyttsx3Adapter`` — no real OS TTS engine init."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def speak(self, text: str) -> None:
        self.calls.append(text)


class _RecordingCloudTTS:
    """A cloud TTS stand-in matching every real cloud TTS class's constructor shape (``api_key``
    positional, ``voice_id``/``model`` optional keyword) — never raises, never touches a network."""

    def __init__(
        self, api_key: str, *, voice_id: str | None = None, model: str | None = None
    ) -> None:
        self.api_key = api_key
        self.voice_id = voice_id
        self.model = model

    def speak(self, text: str) -> None:
        pass


def _config(**overrides: object) -> WombatConfig:
    values: dict[str, object] = {
        "deepseek_api_key": "sk-test",
        "deepseek_base_url": "https://api.deepseek.com",
    }
    values.update(overrides)
    return WombatConfig(**values)  # type: ignore[arg-type]


def _compose_artifact(text: str = _COMPOSED_TEXT) -> Artifact:
    return Artifact(
        kind=COMPOSED_OUTPUT,
        produced_by="compose",
        provenance=Provenance(source="system", confidence=1.0, recorded_at=_FIXED_NOW),
        data=composed_output_to_artifact_data(text, _ITEM_ID, _ITEM_KIND, False),
    )


def _response(text: str) -> ModelResponse:
    return ModelResponse(
        text=text,
        model_id="deepseek-chat",
        finish_reason="stop",
        usage=Usage(prompt_tokens=10, completion_tokens=5),
    )


def _fish_adapter(
    transport: _RecordingFakeTransport, player: _RecordingFakePlayer
) -> FishAudioTTSAdapter:
    return FishAudioTTSAdapter(
        _FISH_API_KEY, voice_id=_VOICE_ID, model="s2.1-pro", transport=transport, player=player
    )


# --------------------------------------------------------------------------------------------AC1


async def test_expressive_e2e_tagged_reply_reaches_fish_verbatim_pane_stays_clean() -> None:
    """AC1: a FakeModel reply carrying ONLY allowed steward tags rides speech_shape -> speak ->
    the real ``FishAudioTTSAdapter``, landing in the fake transport's body VERBATIM under the
    pinned ``s2.1-pro`` model header, with playback firing exactly once — while the composed pane
    text carries zero bracketed tags (DEC-69) and the sent text independently re-proves it passes
    the SAME validator the shaping stage runs (DEC-72i, validate-then-send is structural)."""
    tagged_reply = (
        "[soft tone] Your first meeting is at nine. [break] Nothing else needs you before then."
    )
    model = FakeModel(response=_response(tagged_reply))
    transport = _RecordingFakeTransport()
    player = _RecordingFakePlayer()
    adapter = _fish_adapter(transport, player)
    compose_artifact = _compose_artifact()

    shape_stage = SpeechShapeStage(
        config=_config(), voice_enabled=True, adapter_present=True, expressive_tags=True
    )
    shape_ctx = StageContextFake(
        now_fn=lambda: _FIXED_NOW, model_fake=model, last_output_map={"compose": compose_artifact}
    )
    shape_result = await shape_stage.run(shape_ctx)

    assert isinstance(shape_result, Transition)
    assert shape_result.to == "speak"

    speak_stage = SpeakSink(voice_enabled=True, adapter=adapter)
    speak_ctx = StageContextFake(
        now_fn=lambda: _FIXED_NOW,
        last_output_map={"compose": compose_artifact, "speech_shape": shape_result.output},
    )
    speak_result = await speak_stage.run(speak_ctx)

    assert isinstance(speak_result, Done)
    _item_id, _item_kind, spoken, degraded = spoken_output_from_artifact_data(
        speak_result.output.data
    )
    assert spoken is True
    assert degraded is False

    assert len(transport.calls) == 1
    call = transport.calls[0]
    assert call["headers"] == {"Authorization": f"Bearer {_FISH_API_KEY}", "model": "s2.1-pro"}
    assert call["json"] == {
        "text": tagged_reply,
        "reference_id": _VOICE_ID,
        "format": "wav",
    }
    assert player.calls == [_WAV_BYTES]  # playback fires exactly once

    # DEC-69: tags are voice-only — the pane/journal (composed) text carries zero bracketed tags.
    pane_text = str(compose_artifact.data["text"])
    assert "[" not in pane_text
    assert "]" not in pane_text

    # DEC-72i: validate-then-send is structural — the transport only ever received text that
    # passes the SAME validator the shaping stage runs (find_disallowed_token over ALLOWED_TAGS).
    sent_json = call["json"]
    assert isinstance(sent_json, dict)
    assert find_disallowed_token(str(sent_json["text"]), ALLOWED_TAGS) is None


# --------------------------------------------------------------------------------------------AC2


async def test_no_placebo_e2e_out_of_set_tag_degrades_before_any_transport_contact() -> None:
    """AC2: a FakeModel reply opening with an out-of-set tag (``[screaming]``) is rejected to
    silence by the SAME shaping validator (DEC-55f no-placebo) — ``SpeakSink`` degrades with ZERO
    transport calls; the composed text (delivery) is untouched (DEC-55f blast radius)."""
    out_of_set_reply = "[screaming] Your first meeting is at nine."
    model = FakeModel(response=_response(out_of_set_reply))
    transport = _RecordingFakeTransport()
    player = _RecordingFakePlayer()
    adapter = _fish_adapter(transport, player)
    compose_artifact = _compose_artifact()
    snapshot = compose_artifact.model_copy(deep=True)

    shape_stage = SpeechShapeStage(
        config=_config(), voice_enabled=True, adapter_present=True, expressive_tags=True
    )
    shape_ctx = StageContextFake(
        now_fn=lambda: _FIXED_NOW, model_fake=model, last_output_map={"compose": compose_artifact}
    )
    shape_result = await shape_stage.run(shape_ctx)

    assert isinstance(shape_result, Transition)
    _sp_item_id, _sp_item_kind, sp_text, sp_degraded = speech_output_from_artifact_data(
        shape_result.output.data
    )
    assert sp_text is None
    assert sp_degraded is True  # rejected to silence — the DEC-55f no-placebo posture

    speak_stage = SpeakSink(voice_enabled=True, adapter=adapter)
    speak_ctx = StageContextFake(
        now_fn=lambda: _FIXED_NOW,
        last_output_map={"compose": compose_artifact, "speech_shape": shape_result.output},
    )
    speak_result = await speak_stage.run(speak_ctx)

    assert isinstance(speak_result, Degraded)
    assert speak_result.to is None
    assert transport.calls == []  # ZERO provider contact — degrade fires before any speak() call
    assert player.calls == []
    assert compose_artifact == snapshot  # text delivery unaffected


# --------------------------------------------------------------------------------------------AC3


@pytest.mark.parametrize(
    ("label", "overrides", "store_initial"),
    [
        pytest.param("local", {"wombat_tts_provider": "local"}, {}, id="local"),
        pytest.param(
            "elevenlabs",
            {"wombat_tts_provider": "elevenlabs", "wombat_tts_voice_id": "voice-123"},
            {"elevenlabs": "cloud-key"},
            id="elevenlabs",
        ),
        pytest.param(
            "fish-s1",
            {
                "wombat_tts_provider": "fish",
                "wombat_tts_voice_id": "voice-123",
                "wombat_fish_model": "s1",
            },
            {"fish": "cloud-key"},
            id="fish-s1",
        ),
        pytest.param(
            "fish-s2-no-key",
            {
                "wombat_tts_provider": "fish",
                "wombat_tts_voice_id": "voice-123",
                "wombat_fish_model": "s2.1-pro",
            },
            {},
            id="fish-s2-no-key",
        ),
    ],
)
async def test_inertness_e2e_non_expressive_boots_match_pre_arc_baseline(
    monkeypatch: pytest.MonkeyPatch,
    label: str,
    overrides: dict[str, object],
    store_initial: dict[str, str],
) -> None:
    """AC3 (the key-gate pin end-to-end): every boot shape OTHER than a genuinely constructed
    fish primary on an ``EXPRESSIVE_FISH_MODELS`` member yields ``expressive_tags=False`` via the
    SAME formula ``assemble_runtime`` uses (``info.fish_primary and info.fish_model in
    EXPRESSIVE_FISH_MODELS``) — the resulting ``SpeechShapeStage`` instruction is byte-identical
    to the pre-arc baseline, and its empty allowed-tag set rejects any bracketed token
    end-to-end (DEF-18/DEC-72c)."""
    monkeypatch.setattr(select_module, "Pyttsx3Adapter", _FakeLocalTTS)
    monkeypatch.setattr(select_module, "FishAudioTTSAdapter", _RecordingCloudTTS)
    monkeypatch.setattr(select_module, "ElevenLabsTTSAdapter", _RecordingCloudTTS)
    monkeypatch.setattr(select_module, "DeepgramAuraTTSAdapter", _RecordingCloudTTS)
    store = _FakeVoiceKeyStore(initial=store_initial)
    config = _config(**overrides)

    adapter, info = build_tts_adapter_with_info(config, key_store=store)
    expressive_tags = info.fish_primary and info.fish_model in EXPRESSIVE_FISH_MODELS

    assert expressive_tags is False, label

    baseline = SpeechShapeStage(config=_config(), voice_enabled=True, adapter_present=True)
    stage = SpeechShapeStage(
        config=_config(),
        voice_enabled=True,
        adapter_present=adapter is not None,
        expressive_tags=expressive_tags,
    )
    assert stage._system_instruction == baseline._system_instruction, label

    model = FakeModel(response=_response("[calm] a reply that tries to use a bracket tag."))
    ctx = StageContextFake(
        now_fn=lambda: _FIXED_NOW,
        model_fake=model,
        last_output_map={"compose": _compose_artifact()},
    )
    result = await stage.run(ctx)

    assert isinstance(result, Transition)
    _item_id, _item_kind, text, degraded = speech_output_from_artifact_data(result.output.data)
    assert text is None, label
    assert degraded is True, label  # empty allowed set rejects ANY bracketed token


# ------------------------------------------------------------------------------------------ DEF-18


async def test_speak_full_replies_path_structurally_free_of_expressive_instruction() -> None:
    """DEF-18: ``wombat_speak_full_replies=True`` SKIPS the shaping model call entirely (ZERO
    model calls) regardless of ``expressive_tags`` — the full-replies path never reads
    ``self._system_instruction``, so Fish's expressive instruction can never reach it."""
    model = FakeModel(raises=AssertionError("the full-replies path must never call the model"))
    stage = SpeechShapeStage(
        config=_config(),
        voice_enabled=True,
        adapter_present=True,
        expressive_tags=True,
        speak_full_replies=True,
    )
    compose_artifact = _compose_artifact("Plain reply text.")
    ctx = StageContextFake(
        now_fn=lambda: _FIXED_NOW,
        model_fake=model,
        last_output_map={"compose": compose_artifact},
    )

    result = await stage.run(ctx)

    assert isinstance(result, Transition)
    _item_id, _item_kind, text, degraded = speech_output_from_artifact_data(result.output.data)
    assert text == "Plain reply text."
    assert degraded is False
    assert model.calls == []  # zero model calls — the instruction (tagged or not) is never used
