"""wombat.voice.stt — cloud speech-to-text providers, behind the EXISTING
``sources.asr.Transcriber`` protocol (TK-189, EP-31, Q-100, Q-104).

This is the pattern-setter for every later cloud voice provider (TK-190/191/192, Q-100): a thin
``httpx`` REST call over ``voice.transport.VoiceTransport`` — no vendor SDK. ``DeepgramTranscriber``
implements ``sources.asr.Transcriber`` (``transcribe(path: Path) -> str``, synchronous, may
raise on failure — verified at ``src/wombat/sources/asr.py:67-75``) VERBATIM, so it drops
straight into ``ASRSource`` unchanged; nothing here touches ``sources/asr.py``.

TK-190 (EP-31, DEC-28) adds ``ElevenLabsScribeTranscriber`` and ``FishAudioTranscriber``, riding
the SAME pattern and the SAME ``Transcriber`` protocol, completing the launch STT roster.

Q-104 ruling (binding) — key sourcing: ``api_key`` is a PLAIN constructor arg. NO config/keyring/
``resolve_provider_key`` reads happen in this module — resolving and selecting a provider's key
is TK-193's job entirely.

``transcribe()`` reads the audio file's bytes and makes ONE POST to the Deepgram prerecorded
endpoint (``DEEPGRAM_LISTEN_URL``, ``?model=<model>`` appended when ``model`` is set), with
header ``Authorization: Token <api_key>`` and the raw audio bytes as the body. The transcript is
read from ``results.channels[0].alternatives[0].transcript``; a non-2xx response (raised by the
transport as ``VoiceTransportError``) or a missing/malformed transcript field (raised here as
``DeepgramTranscriptionError``) RAISES — this NEVER returns a lying empty transcript.

``ElevenLabsScribeTranscriber.transcribe()`` makes ONE multipart POST to the ElevenLabs
speech-to-text endpoint (``ELEVENLABS_SCRIBE_URL``) with header ``xi-api-key: <api_key>``, the
audio bytes as the ``file`` multipart part (named after ``path.name``), and a ``model_id`` form
field (``model`` when set, else ``"scribe_v1"``). The transcript is read from the top-level
``text`` field; a non-2xx response or a missing/malformed ``text`` field RAISES (the latter as
``ElevenLabsTranscriptionError``) — same never-lying-empty-transcript contract as Deepgram.

``FishAudioTranscriber.transcribe()`` makes ONE POST to the Fish Audio ASR endpoint
(``FISH_AUDIO_ASR_URL``) with headers ``Authorization: Bearer <api_key>`` and
``Content-Type: application/msgpack``, body a hand-rolled msgpack encoding (Q-105(b): NO msgpack
dependency — the ``voice-cloud`` extra stays httpx-ONLY, Q-100) of the one-key map
``{"audio": <audio_bytes>}`` (see ``_encode_fish_audio_request`` for the exact byte layout). The
transcript is read from the top-level ``text`` field; same raise contract as the other providers
(the malformed-body case raised as ``FishAudioTranscriptionError``).

The default ``transport`` is a lazily-constructed ``HttpxVoiceTransport`` (built at each
transcriber's ``__init__`` time when no ``transport`` is injected) — so constructing a
transcriber WITHOUT an explicit fake transport, on a checkout without the ``voice-cloud`` extra,
raises ``ImportError`` there; merely importing this module never does (Q-46/Q-72).

DEC-28 (zero egress by default): nothing here is constructed anywhere in ``src`` outside of a
caller wombat doesn't yet have — this ticket only sets the pattern. TK-193 wires selection;
nothing here is reachable from boot.
"""

from __future__ import annotations

import json as json_module
import struct
from pathlib import Path
from urllib.parse import urlencode

from wombat.voice.transport import HttpxVoiceTransport, VoiceTransport

# Best-effort endpoint pins (Q-104) — truthed later by the DEF-7 live smokes (TK-190+).
DEEPGRAM_LISTEN_URL = "https://api.deepgram.com/v1/listen"
ELEVENLABS_SCRIBE_URL = "https://api.elevenlabs.io/v1/speech-to-text"
FISH_AUDIO_ASR_URL = "https://api.fish.audio/v1/asr"


class DeepgramTranscriptionError(RuntimeError):
    """Raised by ``DeepgramTranscriber.transcribe`` when the Deepgram response body is missing
    or malformed at ``results.channels[0].alternatives[0].transcript`` — NEVER swallowed into a
    lying empty-string transcript. A non-2xx response instead surfaces as the transport's own
    ``VoiceTransportError`` (propagated unchanged)."""


class DeepgramTranscriber:
    """Cloud STT via the Deepgram prerecorded-audio REST endpoint (Q-104). Implements
    ``sources.asr.Transcriber`` structurally (one method, ``transcribe``) — no inheritance
    required, so it drops straight into ``ASRSource`` alongside ``FasterWhisperTranscriber``.
    """

    def __init__(
        self,
        api_key: str,
        *,
        model: str | None = None,
        transport: VoiceTransport | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._transport: VoiceTransport = (
            transport if transport is not None else HttpxVoiceTransport()
        )

    def transcribe(self, path: Path) -> str:
        """Transcribe the audio file at ``path`` via ONE Deepgram prerecorded-endpoint POST.
        Raises on a non-2xx response (``VoiceTransportError``, from the transport) or a missing/
        malformed transcript field (``DeepgramTranscriptionError``) — never a lying empty
        transcript."""
        audio_bytes = path.read_bytes()
        url = DEEPGRAM_LISTEN_URL
        if self._model:
            url = f"{url}?{urlencode({'model': self._model})}"
        headers = {"Authorization": f"Token {self._api_key}"}
        _status, body = self._transport.post(url, headers=headers, content=audio_bytes)
        try:
            payload = json_module.loads(body)
            transcript = payload["results"]["channels"][0]["alternatives"][0]["transcript"]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise DeepgramTranscriptionError(
                f"malformed Deepgram response body: {exc}"
            ) from exc
        if not isinstance(transcript, str):
            raise DeepgramTranscriptionError(
                "malformed Deepgram response: transcript field is not a string"
            )
        return transcript


class ElevenLabsTranscriptionError(RuntimeError):
    """Raised by ``ElevenLabsScribeTranscriber.transcribe`` when the ElevenLabs response body is
    missing or malformed at the top-level ``text`` field — NEVER swallowed into a lying
    empty-string transcript. A non-2xx response instead surfaces as the transport's own
    ``VoiceTransportError`` (propagated unchanged)."""


class FishAudioTranscriptionError(RuntimeError):
    """Raised by ``FishAudioTranscriber.transcribe`` when the Fish Audio response body is missing
    or malformed at the top-level ``text`` field — NEVER swallowed into a lying empty-string
    transcript. A non-2xx response instead surfaces as the transport's own
    ``VoiceTransportError`` (propagated unchanged)."""


class ElevenLabsScribeTranscriber:
    """Cloud STT via the ElevenLabs Scribe speech-to-text REST endpoint (Q-104, Q-105(b)).
    Implements ``sources.asr.Transcriber`` structurally (one method, ``transcribe``) — no
    inheritance required, so it drops straight into ``ASRSource`` alongside
    ``FasterWhisperTranscriber``/``DeepgramTranscriber``.
    """

    def __init__(
        self,
        api_key: str,
        *,
        model: str | None = None,
        transport: VoiceTransport | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._transport: VoiceTransport = (
            transport if transport is not None else HttpxVoiceTransport()
        )

    def transcribe(self, path: Path) -> str:
        """Transcribe the audio file at ``path`` via ONE multipart POST to the ElevenLabs Scribe
        endpoint. Raises on a non-2xx response (``VoiceTransportError``, from the transport) or a
        missing/malformed ``text`` field (``ElevenLabsTranscriptionError``) — never a lying empty
        transcript."""
        audio_bytes = path.read_bytes()
        headers = {"xi-api-key": self._api_key}
        _status, body = self._transport.post(
            ELEVENLABS_SCRIBE_URL,
            headers=headers,
            files={"file": (path.name, audio_bytes)},
            data={"model_id": self._model or "scribe_v1"},
        )
        try:
            payload = json_module.loads(body)
            transcript = payload["text"]
        except (KeyError, TypeError, ValueError) as exc:
            raise ElevenLabsTranscriptionError(
                f"malformed ElevenLabs response body: {exc}"
            ) from exc
        if not isinstance(transcript, str):
            raise ElevenLabsTranscriptionError(
                "malformed ElevenLabs response: text field is not a string"
            )
        return transcript


def _encode_fish_audio_request(audio_bytes: bytes) -> bytes:
    """Hand-roll the msgpack encoding of the one-key map ``{"audio": <audio_bytes>}`` (Q-105(b))
    — NO msgpack dependency; the ``voice-cloud`` extra stays httpx-ONLY (Q-100). Byte layout:
    fixmap-1 (``0x81``) + fixstr ``"audio"`` (``0xa5`` + ``b"audio"``) + bin32 (``0xc6`` + 4-byte
    big-endian length + raw bytes)."""
    return (
        b"\x81"
        b"\xa5audio"
        b"\xc6" + struct.pack(">I", len(audio_bytes)) + audio_bytes
    )


class FishAudioTranscriber:
    """Cloud STT via the Fish Audio ASR REST endpoint (Q-104, Q-105(b)). Implements
    ``sources.asr.Transcriber`` structurally (one method, ``transcribe``) — no inheritance
    required, so it drops straight into ``ASRSource`` alongside ``FasterWhisperTranscriber``/
    ``DeepgramTranscriber``.
    """

    def __init__(
        self,
        api_key: str,
        *,
        transport: VoiceTransport | None = None,
    ) -> None:
        self._api_key = api_key
        self._transport: VoiceTransport = (
            transport if transport is not None else HttpxVoiceTransport()
        )

    def transcribe(self, path: Path) -> str:
        """Transcribe the audio file at ``path`` via ONE msgpack POST to the Fish Audio ASR
        endpoint. Raises on a non-2xx response (``VoiceTransportError``, from the transport) or a
        missing/malformed ``text`` field (``FishAudioTranscriptionError``) — never a lying empty
        transcript."""
        audio_bytes = path.read_bytes()
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/msgpack",
        }
        content = _encode_fish_audio_request(audio_bytes)
        _status, body = self._transport.post(FISH_AUDIO_ASR_URL, headers=headers, content=content)
        try:
            payload = json_module.loads(body)
            transcript = payload["text"]
        except (KeyError, TypeError, ValueError) as exc:
            raise FishAudioTranscriptionError(
                f"malformed Fish Audio response body: {exc}"
            ) from exc
        if not isinstance(transcript, str):
            raise FishAudioTranscriptionError(
                "malformed Fish Audio response: text field is not a string"
            )
        return transcript


__all__ = [
    "DEEPGRAM_LISTEN_URL",
    "ELEVENLABS_SCRIBE_URL",
    "FISH_AUDIO_ASR_URL",
    "DeepgramTranscriber",
    "DeepgramTranscriptionError",
    "ElevenLabsScribeTranscriber",
    "ElevenLabsTranscriptionError",
    "FishAudioTranscriber",
    "FishAudioTranscriptionError",
]
