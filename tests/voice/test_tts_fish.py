"""TK-191 acceptance criteria — Fish Audio cloud TTS pattern-setter (EP-31, Q-100, Q-104).

AC1 (success + request shape + exactly-once playback): ``test_speak_sends_expected_request_and_
plays_returned_bytes_exactly_once``, ``test_fish_audio_tts_adapter_satisfies_ttsadapter_
protocol``.
AC2 (transport/player failure raises, then SpeakSink end-to-end degrade, TK-165/CON-3 parity):
``test_speak_raises_on_transport_or_player_failure``,
``test_speak_sink_degrades_to_terminal_on_adapter_failure_text_unaffected``.
AC3 (clean-checkout import bar / lazy httpx+winsound): ``test_tts_module_imports_without_httpx_
installed``, ``test_fish_audio_tts_adapter_construction_with_default_transport_raises_without_
httpx``.

TK-326 (DEC-71a/DEC-72a): every construction now passes ``model="s2.1-pro"`` — the request-shape
assertion in ``test_speak_sends_expected_request_and_plays_returned_bytes_exactly_once`` proves the
``model`` HTTP header rides alongside the untouched ``Authorization`` header and the JSON body
stays byte-identical.

Every test rides a fake ``VoiceTransport`` + fake ``AudioPlayer`` — ZERO live network calls and
ZERO real audio playback (DEF-7).
"""

from __future__ import annotations

import importlib
import sys
from collections.abc import Sequence
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
from wombat.voice.transport import VoiceTransport, VoiceTransportError
from wombat.voice.tts import FISH_AUDIO_TTS_URL, FishAudioTTSAdapter

_WAV_BYTES = b"RIFF....WAVEfmt returned-audio"


class _RecordingFakeTransport:
    """A fake ``VoiceTransport`` that records the ONE call made to it and returns canned WAV
    bytes (AC1) — never touches the network (DEF-7)."""

    def __init__(self, *, status_code: int = 200, body: bytes = _WAV_BYTES) -> None:
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


# --- AC1: success + request shape + exactly-once playback --------------------------------------


def test_speak_sends_expected_request_and_plays_returned_bytes_exactly_once() -> None:
    transport = _RecordingFakeTransport()
    player = _RecordingFakePlayer()
    adapter = FishAudioTTSAdapter(
        "fish-secret",
        voice_id="voice-abc123",
        model="s2.1-pro",
        transport=transport,
        player=player,
    )

    adapter.speak("You have a new alert.")

    assert len(transport.calls) == 1
    call = transport.calls[0]
    assert call["url"] == FISH_AUDIO_TTS_URL
    assert call["headers"] == {"Authorization": "Bearer fish-secret", "model": "s2.1-pro"}
    assert call["json"] == {
        "text": "You have a new alert.",
        "reference_id": "voice-abc123",
        "format": "wav",
    }
    assert player.calls == [_WAV_BYTES]


def test_fish_audio_tts_adapter_satisfies_ttsadapter_protocol() -> None:
    """Structural ``TTSAdapter`` conformance via a typed assignment (mypy-checked) — the same
    idiom TK-189's ``DeepgramTranscriber`` test uses."""
    transport = _RecordingFakeTransport()
    player = _RecordingFakePlayer()
    adapter: TTSAdapter = FishAudioTTSAdapter(
        "fish-secret",
        voice_id="voice-abc123",
        model="s2.1-pro",
        transport=transport,
        player=player,
    )
    adapter.speak("hello")
    assert player.calls == [_WAV_BYTES]


# --- AC2: transport/player failure raises, then SpeakSink end-to-end degrade -------------------


@pytest.mark.parametrize(
    ("transport", "player"),
    [
        pytest.param(_RaisingFakeTransport(), _RecordingFakePlayer(), id="transport-failure"),
        pytest.param(
            _RecordingFakeTransport(),
            _RaisingFakePlayer(RuntimeError("playback device busy")),
            id="player-failure",
        ),
    ],
)
def test_speak_raises_on_transport_or_player_failure(
    transport: VoiceTransport, player: _RecordingFakePlayer | _RaisingFakePlayer
) -> None:
    adapter = FishAudioTTSAdapter(
        "fish-secret",
        voice_id="voice-abc123",
        model="s2.1-pro",
        transport=transport,
        player=player,
    )
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


@pytest.mark.parametrize(
    ("transport", "player"),
    [
        pytest.param(_RaisingFakeTransport(), _RecordingFakePlayer(), id="transport-failure"),
        pytest.param(
            _RecordingFakeTransport(),
            _RaisingFakePlayer(RuntimeError("playback device busy")),
            id="player-failure",
        ),
    ],
)
async def test_speak_sink_degrades_to_terminal_on_adapter_failure_text_unaffected(
    transport: VoiceTransport, player: _RecordingFakePlayer | _RaisingFakePlayer
) -> None:
    """AC2 end-to-end: the real ``SpeakSink`` wired to a ``FishAudioTTSAdapter`` whose transport
    or player fails degrades to a terminal ``Degraded(to=None)`` carrying ``spoken=False,
    degraded=True`` — the composed text itself is untouched (TK-165 parity, CON-3)."""
    adapter = FishAudioTTSAdapter(
        "fish-secret",
        voice_id="voice-abc123",
        model="s2.1-pro",
        transport=transport,
        player=player,
    )
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


# --- AC3: clean-checkout import bar --------------------------------------------------------------


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
    """AC3: importing ``wombat.voice.tts`` never touches ``httpx`` — only constructing the
    default ``HttpxVoiceTransport`` does."""
    _simulate_absent(monkeypatch, "httpx")
    assert "httpx" not in sys.modules
    importlib.reload(importlib.import_module("wombat.voice.tts"))
    assert "httpx" not in sys.modules


def test_fish_audio_tts_adapter_construction_with_default_transport_raises_without_httpx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC3: constructing a ``FishAudioTTSAdapter`` WITHOUT an explicit ``transport`` (the default
    arg, which lazily builds a real ``HttpxVoiceTransport``) raises ``ImportError`` when the
    ``voice-cloud`` extra is absent — the real, unmocked lazy-import-failure path. An explicit
    ``player`` fake is supplied so only the transport's lazy import is exercised."""
    _simulate_absent(monkeypatch, "httpx")
    with pytest.raises(ImportError):
        FishAudioTTSAdapter(
            "fish-secret",
            voice_id="voice-abc123",
            model="s2.1-pro",
            player=_RecordingFakePlayer(),
        )
