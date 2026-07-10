"""TK-190 acceptance criteria — ElevenLabs Scribe + Fish Audio cloud STT providers, riding the
TK-189 pattern (EP-31, Q-100, Q-104, Q-105).

AC1 (success + request shape + Transcriber conformance): ``test_elevenlabs_transcribe_returns_
transcript_and_sends_expected_request``, ``test_elevenlabs_transcriber_satisfies_transcriber_
protocol``, ``test_fish_audio_transcribe_returns_transcript_and_sends_expected_request``,
``test_fish_audio_transcriber_satisfies_transcriber_protocol``,
``test_encode_fish_audio_request_byte_layout``.
AC2 (non-2xx / malformed body both raise, ASRSource untouched): ``test_*_raises_on_non_2xx_
response``, ``test_*_raises_on_malformed_response_body``.
AC3 (clean-checkout import bar / lazy httpx): ``test_stt_module_imports_without_httpx_
installed``, ``test_*_construction_with_default_transport_raises_without_httpx``.

Every test rides a fake ``VoiceTransport`` — ZERO live network calls (DEF-7).
"""

from __future__ import annotations

import importlib
import json
import struct
import sys
from collections.abc import Sequence
from importlib.abc import MetaPathFinder
from importlib.machinery import ModuleSpec
from pathlib import Path
from types import ModuleType

import pytest

from wombat.sources.asr import Transcriber
from wombat.voice.stt import (
    ELEVENLABS_SCRIBE_URL,
    FISH_AUDIO_ASR_URL,
    ElevenLabsScribeTranscriber,
    FishAudioTranscriber,
    _encode_fish_audio_request,
)
from wombat.voice.transport import VoiceTransportError

# ``ElevenLabsTranscriptionError``/``FishAudioTranscriptionError`` are deliberately NOT imported
# at module level: ``test_stt_deepgram.py`` (collected/run before this file, alphabetically) has
# its own AC3 ``importlib.reload(wombat.voice.stt)`` that rebinds these class objects to new
# identities in the shared module dict. A module-level import here would freeze the PRE-reload
# identity, which no longer matches what the (reload-shared-globals) transcriber methods actually
# raise. The malformed-body tests below re-import locally, at call time, to always compare against
# the CURRENT live class.

_ELEVENLABS_SUCCESS_BODY = json.dumps({"text": "the quick brown fox"}).encode("utf-8")
_FISH_AUDIO_SUCCESS_BODY = json.dumps({"text": "the quick brown fox"}).encode("utf-8")


class _RecordingFakeTransport:
    """A fake ``VoiceTransport`` that records the ONE call made to it and returns a canned
    ``(status_code, body_bytes)`` pair (AC1) — never touches the network (DEF-7)."""

    def __init__(self, *, status_code: int = 200, body: bytes = b"{}") -> None:
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
        self.calls.append(
            {
                "url": url,
                "headers": headers,
                "content": content,
                "json": json,
                "data": data,
                "files": files,
            }
        )
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


@pytest.fixture
def audio_fixture(tmp_path: Path) -> Path:
    fixture = tmp_path / "clip.wav"
    fixture.write_bytes(b"RIFF....WAVEfmt ")  # content is opaque to the transcriber
    return fixture


# --- ElevenLabs Scribe: AC1 ---------------------------------------------------------------------


def test_elevenlabs_transcribe_returns_transcript_and_sends_expected_request(
    audio_fixture: Path,
) -> None:
    transport = _RecordingFakeTransport(body=_ELEVENLABS_SUCCESS_BODY)
    transcriber = ElevenLabsScribeTranscriber(
        api_key="el-secret", model="scribe_v2", transport=transport
    )

    transcript = transcriber.transcribe(audio_fixture)

    assert transcript == "the quick brown fox"
    assert len(transport.calls) == 1
    call = transport.calls[0]
    assert call["url"] == ELEVENLABS_SCRIBE_URL
    assert call["headers"] == {"xi-api-key": "el-secret"}
    assert call["files"] == {"file": ("clip.wav", audio_fixture.read_bytes())}
    assert call["data"] == {"model_id": "scribe_v2"}
    assert call["content"] is None
    assert call["json"] is None


def test_elevenlabs_transcribe_without_model_defaults_to_scribe_v1(
    audio_fixture: Path,
) -> None:
    transport = _RecordingFakeTransport(body=_ELEVENLABS_SUCCESS_BODY)
    transcriber = ElevenLabsScribeTranscriber(api_key="el-secret", transport=transport)

    transcriber.transcribe(audio_fixture)

    assert transport.calls[0]["data"] == {"model_id": "scribe_v1"}


def test_elevenlabs_transcriber_satisfies_transcriber_protocol(audio_fixture: Path) -> None:
    """Structural ``Transcriber`` conformance via a typed assignment (mypy-checked) — the same
    idiom TK-189's ``DeepgramTranscriber`` test uses, since ``sources.asr.Transcriber`` is not
    ``runtime_checkable`` and this ticket does not touch ``sources/asr.py``."""
    transport = _RecordingFakeTransport(body=_ELEVENLABS_SUCCESS_BODY)
    transcriber: Transcriber = ElevenLabsScribeTranscriber(api_key="el-secret", transport=transport)
    assert transcriber.transcribe(audio_fixture) == "the quick brown fox"


# --- ElevenLabs Scribe: AC2 ----------------------------------------------------------------------


def test_elevenlabs_transcribe_raises_on_non_2xx_response(audio_fixture: Path) -> None:
    transcriber = ElevenLabsScribeTranscriber(
        api_key="el-secret", transport=_RaisingFakeTransport()
    )
    with pytest.raises(VoiceTransportError):
        transcriber.transcribe(audio_fixture)


@pytest.mark.parametrize(
    "malformed_body",
    [
        b"{}",
        b"not json at all",
        json.dumps({"text": None}).encode("utf-8"),
        json.dumps({"text": 123}).encode("utf-8"),
    ],
)
def test_elevenlabs_transcribe_raises_on_malformed_response_body(
    audio_fixture: Path, malformed_body: bytes
) -> None:
    from wombat.voice.stt import ElevenLabsTranscriptionError  # see note near the top imports

    transport = _RecordingFakeTransport(body=malformed_body)
    transcriber = ElevenLabsScribeTranscriber(api_key="el-secret", transport=transport)
    with pytest.raises(ElevenLabsTranscriptionError):
        transcriber.transcribe(audio_fixture)


# --- Fish Audio ASR: AC1 -------------------------------------------------------------------------


def test_encode_fish_audio_request_byte_layout() -> None:
    """Pins the exact hand-rolled msgpack byte layout (Q-105(b)): fixmap-1 (0x81) + fixstr
    'audio' (0xa5 + b'audio') + bin32 (0xc6 + 4-byte big-endian length + raw bytes)."""
    audio_bytes = b"abc123"
    encoded = _encode_fish_audio_request(audio_bytes)
    expected = b"\x81" + b"\xa5audio" + b"\xc6" + struct.pack(">I", len(audio_bytes)) + audio_bytes
    assert encoded == expected
    assert encoded == (
        b"\x81\xa5audio\xc6\x00\x00\x00\x06abc123"
    )


def test_fish_audio_transcribe_returns_transcript_and_sends_expected_request(
    audio_fixture: Path,
) -> None:
    transport = _RecordingFakeTransport(body=_FISH_AUDIO_SUCCESS_BODY)
    transcriber = FishAudioTranscriber(api_key="fish-secret", transport=transport)

    transcript = transcriber.transcribe(audio_fixture)

    assert transcript == "the quick brown fox"
    assert len(transport.calls) == 1
    call = transport.calls[0]
    assert call["url"] == FISH_AUDIO_ASR_URL
    assert call["headers"] == {
        "Authorization": "Bearer fish-secret",
        "Content-Type": "application/msgpack",
    }
    assert call["content"] == _encode_fish_audio_request(audio_fixture.read_bytes())
    assert call["json"] is None
    assert call["data"] is None
    assert call["files"] is None


def test_fish_audio_transcriber_satisfies_transcriber_protocol(audio_fixture: Path) -> None:
    """Structural ``Transcriber`` conformance via a typed assignment (mypy-checked)."""
    transport = _RecordingFakeTransport(body=_FISH_AUDIO_SUCCESS_BODY)
    transcriber: Transcriber = FishAudioTranscriber(api_key="fish-secret", transport=transport)
    assert transcriber.transcribe(audio_fixture) == "the quick brown fox"


# --- Fish Audio ASR: AC2 -------------------------------------------------------------------------


def test_fish_audio_transcribe_raises_on_non_2xx_response(audio_fixture: Path) -> None:
    transcriber = FishAudioTranscriber(api_key="fish-secret", transport=_RaisingFakeTransport())
    with pytest.raises(VoiceTransportError):
        transcriber.transcribe(audio_fixture)


@pytest.mark.parametrize(
    "malformed_body",
    [
        b"{}",
        b"not json at all",
        json.dumps({"text": None}).encode("utf-8"),
        json.dumps({"text": 123}).encode("utf-8"),
    ],
)
def test_fish_audio_transcribe_raises_on_malformed_response_body(
    audio_fixture: Path, malformed_body: bytes
) -> None:
    from wombat.voice.stt import FishAudioTranscriptionError  # see note near the top imports

    transport = _RecordingFakeTransport(body=malformed_body)
    transcriber = FishAudioTranscriber(api_key="fish-secret", transport=transport)
    with pytest.raises(FishAudioTranscriptionError):
        transcriber.transcribe(audio_fixture)


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
    """Simulate ``module_name`` being genuinely not installed, regardless of whether it actually
    is on this machine (TK-202/Q-103): evict any cached import AND install a meta-path finder
    ahead of the real one so any subsequent import raises ``ModuleNotFoundError`` — robust to the
    module actually being present (e.g. transitively, via another extra)."""
    monkeypatch.delitem(sys.modules, module_name, raising=False)
    monkeypatch.setattr(sys, "meta_path", [_BlockedFinder(module_name), *sys.meta_path])


def test_stt_module_imports_without_httpx_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC3: importing ``wombat.voice.stt`` never touches ``httpx`` — only constructing
    ``HttpxVoiceTransport`` does."""
    _simulate_absent(monkeypatch, "httpx")
    assert "httpx" not in sys.modules
    importlib.reload(importlib.import_module("wombat.voice.transport"))
    importlib.reload(importlib.import_module("wombat.voice.stt"))
    assert "httpx" not in sys.modules


def test_elevenlabs_transcriber_construction_with_default_transport_raises_without_httpx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC3: constructing an ``ElevenLabsScribeTranscriber`` WITHOUT an explicit ``transport`` (the
    default arg, which lazily builds a real ``HttpxVoiceTransport``) raises ``ImportError`` when
    the ``voice-cloud`` extra is absent — the real, unmocked lazy-import-failure path."""
    _simulate_absent(monkeypatch, "httpx")
    with pytest.raises(ImportError):
        ElevenLabsScribeTranscriber(api_key="el-secret")


def test_fish_audio_transcriber_construction_with_default_transport_raises_without_httpx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC3: constructing a ``FishAudioTranscriber`` WITHOUT an explicit ``transport`` (the default
    arg, which lazily builds a real ``HttpxVoiceTransport``) raises ``ImportError`` when the
    ``voice-cloud`` extra is absent — the real, unmocked lazy-import-failure path."""
    _simulate_absent(monkeypatch, "httpx")
    with pytest.raises(ImportError):
        FishAudioTranscriber(api_key="fish-secret")
