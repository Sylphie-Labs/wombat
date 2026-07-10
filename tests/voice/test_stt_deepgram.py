"""TK-189 acceptance criteria — Deepgram cloud STT pattern-setter (EP-31, Q-100, Q-104).

AC1 (success + request shape + Transcriber conformance): ``test_transcribe_returns_transcript_
and_sends_expected_request``, ``test_deepgram_transcriber_satisfies_transcriber_protocol``.
AC2 (non-2xx / malformed body both raise, ASRSource untouched): ``test_transcribe_raises_
on_non_2xx_response``, ``test_transcribe_raises_on_malformed_response_body``.
AC3 (clean-checkout import bar / lazy httpx): ``test_transport_and_stt_modules_import_without_
httpx_installed``, ``test_deepgram_transcriber_construction_with_default_transport_raises_
without_httpx``.

Every test rides a fake ``VoiceTransport`` — ZERO live network calls (DEF-7).
"""

from __future__ import annotations

import importlib
import json
import sys
from collections.abc import Sequence
from importlib.abc import MetaPathFinder
from importlib.machinery import ModuleSpec
from pathlib import Path
from types import ModuleType

import pytest

from wombat.sources.asr import Transcriber
from wombat.voice.stt import DeepgramTranscriber, DeepgramTranscriptionError
from wombat.voice.transport import VoiceTransport, VoiceTransportError

_CANNED_SUCCESS_BODY = json.dumps(
    {
        "results": {
            "channels": [
                {"alternatives": [{"transcript": "the quick brown fox"}]},
            ]
        }
    }
).encode("utf-8")


class _RecordingFakeTransport:
    """A fake ``VoiceTransport`` that records the ONE call made to it and returns a canned
    ``(status_code, body_bytes)`` pair (AC1) — never touches the network (DEF-7)."""

    def __init__(self, *, status_code: int = 200, body: bytes = _CANNED_SUCCESS_BODY) -> None:
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
    ) -> tuple[int, bytes]:
        raise VoiceTransportError(f"voice transport POST {url} returned 401: 'unauthorized'")


@pytest.fixture
def audio_fixture(tmp_path: Path) -> Path:
    fixture = tmp_path / "clip.wav"
    fixture.write_bytes(b"RIFF....WAVEfmt ")  # content is opaque to the transcriber
    return fixture


def test_transcribe_returns_transcript_and_sends_expected_request(audio_fixture: Path) -> None:
    transport = _RecordingFakeTransport()
    transcriber = DeepgramTranscriber(api_key="dg-secret", model="nova-2", transport=transport)

    transcript = transcriber.transcribe(audio_fixture)

    assert transcript == "the quick brown fox"
    assert len(transport.calls) == 1
    call = transport.calls[0]
    assert call["url"] == "https://api.deepgram.com/v1/listen?model=nova-2"
    assert call["headers"] == {"Authorization": "Token dg-secret"}
    assert call["content"] == audio_fixture.read_bytes()


def test_transcribe_without_model_omits_the_query_param(audio_fixture: Path) -> None:
    transport = _RecordingFakeTransport()
    transcriber = DeepgramTranscriber(api_key="dg-secret", transport=transport)

    transcriber.transcribe(audio_fixture)

    assert transport.calls[0]["url"] == "https://api.deepgram.com/v1/listen"


def test_deepgram_transcriber_satisfies_transcriber_protocol(audio_fixture: Path) -> None:
    """Structural ``Transcriber`` conformance via a typed assignment (mypy-checked) — the same
    idiom TK-164's ``TTSAdapter`` test uses, since ``sources.asr.Transcriber`` is not
    ``runtime_checkable`` and this ticket does not touch ``sources/asr.py``."""
    transport = _RecordingFakeTransport()
    transcriber: Transcriber = DeepgramTranscriber(api_key="dg-secret", transport=transport)
    assert transcriber.transcribe(audio_fixture) == "the quick brown fox"


def test_transcribe_raises_on_non_2xx_response(audio_fixture: Path) -> None:
    transcriber = DeepgramTranscriber(api_key="dg-secret", transport=_RaisingFakeTransport())
    with pytest.raises(VoiceTransportError):
        transcriber.transcribe(audio_fixture)


@pytest.mark.parametrize(
    "malformed_body",
    [
        b"{}",
        b"not json at all",
        json.dumps({"results": {"channels": []}}).encode("utf-8"),
        json.dumps({"results": {"channels": [{"alternatives": []}]}}).encode("utf-8"),
        json.dumps(
            {"results": {"channels": [{"alternatives": [{"transcript": None}]}]}}
        ).encode("utf-8"),
    ],
)
def test_transcribe_raises_on_malformed_response_body(
    audio_fixture: Path, malformed_body: bytes
) -> None:
    transport = _RecordingFakeTransport(body=malformed_body)
    transcriber = DeepgramTranscriber(api_key="dg-secret", transport=transport)
    with pytest.raises(DeepgramTranscriptionError):
        transcriber.transcribe(audio_fixture)


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


def test_transport_and_stt_modules_import_without_httpx_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC3: importing either module never touches ``httpx`` — only constructing
    ``HttpxVoiceTransport`` does."""
    _simulate_absent(monkeypatch, "httpx")
    assert "httpx" not in sys.modules
    importlib.reload(importlib.import_module("wombat.voice.transport"))
    importlib.reload(importlib.import_module("wombat.voice.stt"))
    assert "httpx" not in sys.modules


def test_deepgram_transcriber_construction_with_default_transport_raises_without_httpx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC3: constructing a ``DeepgramTranscriber`` WITHOUT an explicit ``transport`` (the
    default arg, which lazily builds a real ``HttpxVoiceTransport``) raises ``ImportError`` when
    the ``voice-cloud`` extra is absent — the real, unmocked lazy-import-failure path."""
    _simulate_absent(monkeypatch, "httpx")
    with pytest.raises(ImportError):
        DeepgramTranscriber(api_key="dg-secret")


def test_httpx_voice_transport_construction_raises_without_httpx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from wombat.voice.transport import HttpxVoiceTransport

    _simulate_absent(monkeypatch, "httpx")
    with pytest.raises(ImportError):
        HttpxVoiceTransport()


def test_voice_transport_protocol_is_runtime_checkable() -> None:
    transport: VoiceTransport = _RecordingFakeTransport()
    assert isinstance(transport, VoiceTransport)
