"""TK-192 acceptance criteria — ElevenLabs + Deepgram Aura cloud TTS providers, riding the TK-191
pattern (EP-31, Q-100, Q-104, Q-105(c)).

AC1 (success + request shape + exactly-once playback): ``test_elevenlabs_speak_sends_expected_
request_and_plays_wrapped_pcm_exactly_once``, ``test_elevenlabs_speak_sends_model_id_when_model_
given``, ``test_elevenlabs_tts_adapter_satisfies_ttsadapter_protocol``, ``test_deepgram_aura_speak_
sends_expected_request_and_plays_returned_bytes_exactly_once``, ``test_deepgram_aura_speak_uses_
default_model_when_voice_id_not_given``, ``test_deepgram_aura_tts_adapter_satisfies_ttsadapter_
protocol``.
AC2 (transport/player failure raises, then SpeakSink end-to-end degrade, TK-165/CON-3 parity):
``test_speak_raises_on_transport_or_player_failure`` (parametrized over both providers),
``test_speak_sink_degrades_to_terminal_on_adapter_failure_text_unaffected`` (parametrized over
both providers).
Import hygiene (mirrors TK-191's AC3): ``test_tts_module_imports_without_httpx_installed``,
``test_*_construction_with_default_transport_raises_without_httpx``.

Every test rides a fake ``VoiceTransport`` + fake ``AudioPlayer`` — ZERO live network calls and
ZERO real audio playback (DEF-7).
"""

from __future__ import annotations

import importlib
import io
import sys
import wave
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from importlib.abc import MetaPathFinder
from importlib.machinery import ModuleSpec
from types import ModuleType

import pytest
from cogworx.claims.provenance import Artifact, Provenance
from cogworx.loop.result import Degraded

from tests.support.stage_context_fake import StageContextFake
from wombat.gate.models import ItemKind
from wombat.sinks.speak import SpeakSink
from wombat.sinks.tts_adapter import TTSAdapter
from wombat.stages.artifacts import (
    COMPOSED_OUTPUT,
    composed_output_to_artifact_data,
    spoken_output_from_artifact_data,
)
from wombat.voice.playback import AudioPlayer
from wombat.voice.transport import VoiceTransport, VoiceTransportError
from wombat.voice.tts import (
    DEEPGRAM_AURA_DEFAULT_MODEL,
    DEEPGRAM_AURA_TTS_URL,
    ELEVENLABS_TTS_URL_TEMPLATE,
    DeepgramAuraTTSAdapter,
    ElevenLabsTTSAdapter,
)

_PCM_BYTES = b"\x01\x02\x03\x04\x05\x06\x07\x08"  # headerless 16-bit mono 16kHz PCM (even length)
_AURA_WAV_BYTES = b"RIFF....WAVEfmt already-a-wav-container"


class _RecordingFakeTransport:
    """A fake ``VoiceTransport`` that records the ONE call made to it and returns canned bytes
    (AC1) — never touches the network (DEF-7)."""

    def __init__(self, *, status_code: int = 200, body: bytes = b"") -> None:
        self._status_code = status_code
        self._body = body
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
        self.calls.append({"url": url, "headers": headers, "content": content, "json": json})
        return self._status_code, self._body


class _RaisingFakeTransport:
    """A fake ``VoiceTransport`` that simulates the real ``HttpxVoiceTransport`` non-2xx
    contract: raises ``VoiceTransportError`` rather than returning a failure status (AC2)."""

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
        raise VoiceTransportError(f"voice transport POST {url} returned 401: 'unauthorized'")


class _RecordingFakePlayer:
    """A fake ``AudioPlayer`` that records every ``play()`` call (AC1) — never touches real audio
    hardware."""

    def __init__(self) -> None:
        self.calls: list[bytes] = []

    def play(self, wav_bytes: bytes) -> None:
        self.calls.append(wav_bytes)


class _RaisingFakePlayer:
    """A fake ``AudioPlayer`` whose ``play()`` always raises (AC2)."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc
        self.calls: list[bytes] = []

    def play(self, wav_bytes: bytes) -> None:
        self.calls.append(wav_bytes)
        raise self._exc


# --- AC1: ElevenLabs — success + request shape + exactly-once playback -------------------------


def test_elevenlabs_speak_sends_expected_request_and_plays_wrapped_pcm_exactly_once() -> None:
    transport = _RecordingFakeTransport(body=_PCM_BYTES)
    player = _RecordingFakePlayer()
    adapter = ElevenLabsTTSAdapter(
        "el-secret", voice_id="voice-abc123", transport=transport, player=player
    )

    adapter.speak("You have a new alert.")

    assert len(transport.calls) == 1
    call = transport.calls[0]
    expected_url = (
        f"{ELEVENLABS_TTS_URL_TEMPLATE.format(voice_id='voice-abc123')}?output_format=pcm_16000"
    )
    assert call["url"] == expected_url
    assert call["headers"] == {"xi-api-key": "el-secret"}
    assert call["json"] == {"text": "You have a new alert."}

    assert len(player.calls) == 1
    played_bytes = player.calls[0]
    with wave.open(io.BytesIO(played_bytes), "rb") as wav_file:
        assert wav_file.getnchannels() == 1
        assert wav_file.getsampwidth() == 2
        assert wav_file.getframerate() == 16000
        assert wav_file.readframes(wav_file.getnframes()) == _PCM_BYTES


def test_elevenlabs_speak_sends_model_id_when_model_given() -> None:
    transport = _RecordingFakeTransport(body=_PCM_BYTES)
    player = _RecordingFakePlayer()
    adapter = ElevenLabsTTSAdapter(
        "el-secret", voice_id="voice-abc123", model="eleven_turbo_v2", transport=transport,
        player=player,
    )

    adapter.speak("hello")

    assert transport.calls[0]["json"] == {"text": "hello", "model_id": "eleven_turbo_v2"}


def test_elevenlabs_tts_adapter_satisfies_ttsadapter_protocol() -> None:
    """Structural ``TTSAdapter`` conformance via a typed assignment (mypy-checked) — the same
    idiom TK-191's ``FishAudioTTSAdapter`` test uses."""
    transport = _RecordingFakeTransport(body=_PCM_BYTES)
    player = _RecordingFakePlayer()
    adapter: TTSAdapter = ElevenLabsTTSAdapter(
        "el-secret", voice_id="voice-abc123", transport=transport, player=player
    )
    adapter.speak("hello")
    assert len(player.calls) == 1


# --- AC1: Deepgram Aura — success + request shape + exactly-once playback -----------------------


def test_deepgram_aura_speak_sends_expected_request_and_plays_returned_bytes_exactly_once() -> None:
    transport = _RecordingFakeTransport(body=_AURA_WAV_BYTES)
    player = _RecordingFakePlayer()
    adapter = DeepgramAuraTTSAdapter(
        "dg-secret", voice_id="aura-luna-en", transport=transport, player=player
    )

    adapter.speak("You have a new alert.")

    assert len(transport.calls) == 1
    call = transport.calls[0]
    expected_url = (
        f"{DEEPGRAM_AURA_TTS_URL}?model=aura-luna-en&encoding=linear16"
        "&sample_rate=24000&container=wav"
    )
    assert call["url"] == expected_url
    assert call["headers"] == {"Authorization": "Token dg-secret"}
    assert call["json"] == {"text": "You have a new alert."}
    assert player.calls == [_AURA_WAV_BYTES]


def test_deepgram_aura_speak_uses_default_model_when_voice_id_not_given() -> None:
    transport = _RecordingFakeTransport(body=_AURA_WAV_BYTES)
    player = _RecordingFakePlayer()
    adapter = DeepgramAuraTTSAdapter("dg-secret", transport=transport, player=player)

    adapter.speak("hello")

    call = transport.calls[0]
    assert isinstance(call["url"], str)
    assert f"model={DEEPGRAM_AURA_DEFAULT_MODEL}&" in call["url"]


def test_deepgram_aura_tts_adapter_satisfies_ttsadapter_protocol() -> None:
    """Structural ``TTSAdapter`` conformance via a typed assignment (mypy-checked)."""
    transport = _RecordingFakeTransport(body=_AURA_WAV_BYTES)
    player = _RecordingFakePlayer()
    adapter: TTSAdapter = DeepgramAuraTTSAdapter(
        "dg-secret", voice_id="aura-luna-en", transport=transport, player=player
    )
    adapter.speak("hello")
    assert player.calls == [_AURA_WAV_BYTES]


# --- AC2: transport/player failure raises, then SpeakSink end-to-end degrade --------------------


_AdapterFactory = Callable[[VoiceTransport, AudioPlayer], TTSAdapter]


def _make_elevenlabs_adapter(transport: VoiceTransport, player: AudioPlayer) -> TTSAdapter:
    return ElevenLabsTTSAdapter(
        "el-secret", voice_id="voice-abc123", transport=transport, player=player
    )


def _make_deepgram_adapter(transport: VoiceTransport, player: AudioPlayer) -> TTSAdapter:
    return DeepgramAuraTTSAdapter(
        "dg-secret", voice_id="aura-luna-en", transport=transport, player=player
    )


@pytest.mark.parametrize("make_adapter", [_make_elevenlabs_adapter, _make_deepgram_adapter])
@pytest.mark.parametrize(
    ("transport", "player"),
    [
        pytest.param(
            _RaisingFakeTransport(), _RecordingFakePlayer(), id="transport-failure"
        ),
        pytest.param(
            _RecordingFakeTransport(body=_AURA_WAV_BYTES),
            _RaisingFakePlayer(RuntimeError("playback device busy")),
            id="player-failure",
        ),
    ],
)
def test_speak_raises_on_transport_or_player_failure(
    make_adapter: _AdapterFactory,
    transport: VoiceTransport,
    player: AudioPlayer,
) -> None:
    adapter = make_adapter(transport, player)
    with pytest.raises(Exception):  # noqa: B017 — either VoiceTransportError or RuntimeError
        adapter.speak("hello")


_FIXED_NOW = datetime(2026, 7, 9, 12, 0, 0, tzinfo=UTC)
_ITEM_ID = "gate-item-1"
_ITEM_KIND = ItemKind.GENERIC
_TEXT = "You have a new alert."


def _composed_output_artifact() -> Artifact:
    return Artifact(
        kind=COMPOSED_OUTPUT,
        produced_by="compose",
        provenance=Provenance(source="system", confidence=1.0, recorded_at=_FIXED_NOW),
        data=composed_output_to_artifact_data(_TEXT, _ITEM_ID, _ITEM_KIND, False),
    )


@pytest.mark.parametrize("make_adapter", [_make_elevenlabs_adapter, _make_deepgram_adapter])
@pytest.mark.parametrize(
    ("transport", "player"),
    [
        pytest.param(
            _RaisingFakeTransport(), _RecordingFakePlayer(), id="transport-failure"
        ),
        pytest.param(
            _RecordingFakeTransport(body=_AURA_WAV_BYTES),
            _RaisingFakePlayer(RuntimeError("playback device busy")),
            id="player-failure",
        ),
    ],
)
async def test_speak_sink_degrades_to_terminal_on_adapter_failure_text_unaffected(
    make_adapter: _AdapterFactory,
    transport: VoiceTransport,
    player: AudioPlayer,
) -> None:
    """AC2 end-to-end: the real ``SpeakSink`` wired to either new adapter whose transport or
    player fails degrades to a terminal ``Degraded(to=None)`` carrying ``spoken=False,
    degraded=True`` — the composed text itself is untouched (TK-165/TK-191 parity, CON-3)."""
    adapter = make_adapter(transport, player)
    stage = SpeakSink(voice_enabled=True, adapter=adapter)
    compose_artifact = _composed_output_artifact()
    snapshot = compose_artifact.model_copy(deep=True)
    ctx = StageContextFake(
        now_fn=lambda: _FIXED_NOW, last_output_map={"compose": compose_artifact}
    )

    result = await stage.run(ctx)

    assert isinstance(result, Degraded)
    assert result.to is None
    assert result.reason
    _item_id, _item_kind, spoken, degraded = spoken_output_from_artifact_data(result.output.data)
    assert spoken is False
    assert degraded is True
    assert compose_artifact == snapshot  # the composed text wire artifact is untouched


# --- Import hygiene (mirrors TK-191's AC3) -------------------------------------------------------


class _BlockedFinder(MetaPathFinder):
    """A meta-path finder that fails the import of one named module (and its submodules)."""

    def __init__(self, blocked: str) -> None:
        self._blocked = blocked

    def find_spec(
        self, fullname: str, path: Sequence[str] | None, target: ModuleType | None = None
    ) -> ModuleSpec | None:
        if fullname == self._blocked or fullname.startswith(f"{self._blocked}."):
            raise ModuleNotFoundError(f"No module named {fullname!r} (simulated absence, TK-202)")
        return None


def _simulate_absent(monkeypatch: pytest.MonkeyPatch, module_name: str) -> None:
    """Simulate ``module_name`` being genuinely not installed (TK-202/Q-103), robust to the
    module actually being present."""
    monkeypatch.delitem(sys.modules, module_name, raising=False)
    monkeypatch.setattr(sys, "meta_path", [_BlockedFinder(module_name), *sys.meta_path])


def test_tts_module_imports_without_httpx_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Importing ``wombat.voice.tts`` never touches ``httpx`` — only constructing the default
    ``HttpxVoiceTransport`` does."""
    _simulate_absent(monkeypatch, "httpx")
    assert "httpx" not in sys.modules
    importlib.reload(importlib.import_module("wombat.voice.tts"))
    assert "httpx" not in sys.modules


def test_elevenlabs_tts_adapter_construction_with_default_transport_raises_without_httpx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Constructing an ``ElevenLabsTTSAdapter`` WITHOUT an explicit ``transport`` (the default
    arg, which lazily builds a real ``HttpxVoiceTransport``) raises ``ImportError`` when the
    ``voice-cloud`` extra is absent — the real, unmocked lazy-import-failure path. An explicit
    ``player`` fake is supplied so only the transport's lazy import is exercised."""
    _simulate_absent(monkeypatch, "httpx")
    with pytest.raises(ImportError):
        ElevenLabsTTSAdapter("el-secret", voice_id="voice-abc123", player=_RecordingFakePlayer())


def test_deepgram_aura_tts_adapter_construction_with_default_transport_raises_without_httpx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Constructing a ``DeepgramAuraTTSAdapter`` WITHOUT an explicit ``transport`` (the default
    arg, which lazily builds a real ``HttpxVoiceTransport``) raises ``ImportError`` when the
    ``voice-cloud`` extra is absent — the real, unmocked lazy-import-failure path."""
    _simulate_absent(monkeypatch, "httpx")
    with pytest.raises(ImportError):
        DeepgramAuraTTSAdapter("dg-secret", player=_RecordingFakePlayer())
