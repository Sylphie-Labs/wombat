"""wombat.voice.stt — cloud speech-to-text providers, behind the EXISTING
``sources.asr.Transcriber`` protocol (TK-189, EP-31, Q-100, Q-104).

This is the pattern-setter for every later cloud voice provider (TK-190/191/192, Q-100): a thin
``httpx`` REST call over ``voice.transport.VoiceTransport`` — no vendor SDK. ``DeepgramTranscriber``
implements ``sources.asr.Transcriber`` (``transcribe(path: Path) -> str``, synchronous, may
raise on failure — verified at ``src/wombat/sources/asr.py:67-75``) VERBATIM, so it drops
straight into ``ASRSource`` unchanged; nothing here touches ``sources/asr.py``.

Q-104 ruling (binding) — key sourcing: ``api_key`` is a PLAIN constructor arg. NO config/keyring/
``resolve_provider_key`` reads happen in this module — resolving and selecting a provider's key
is TK-193's job entirely.

``transcribe()`` reads the audio file's bytes and makes ONE POST to the Deepgram prerecorded
endpoint (``DEEPGRAM_LISTEN_URL``, ``?model=<model>`` appended when ``model`` is set), with
header ``Authorization: Token <api_key>`` and the raw audio bytes as the body. The transcript is
read from ``results.channels[0].alternatives[0].transcript``; a non-2xx response (raised by the
transport as ``VoiceTransportError``) or a missing/malformed transcript field (raised here as
``DeepgramTranscriptionError``) RAISES — this NEVER returns a lying empty transcript.

The default ``transport`` is a lazily-constructed ``HttpxVoiceTransport`` (built at
``DeepgramTranscriber.__init__`` time when no ``transport`` is injected) — so constructing a
``DeepgramTranscriber`` WITHOUT an explicit fake transport, on a checkout without the
``voice-cloud`` extra, raises ``ImportError`` there; merely importing this module never does
(Q-46/Q-72).

DEC-28 (zero egress by default): nothing here is constructed anywhere in ``src`` outside of a
caller wombat doesn't yet have — this ticket only sets the pattern. TK-193 wires selection;
nothing here is reachable from boot.
"""

from __future__ import annotations

import json as json_module
from pathlib import Path
from urllib.parse import urlencode

from wombat.voice.transport import HttpxVoiceTransport, VoiceTransport

# Best-effort endpoint pin (Q-104) — truthed later by the DEF-7 live smokes (TK-190+).
DEEPGRAM_LISTEN_URL = "https://api.deepgram.com/v1/listen"


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


__all__ = ["DEEPGRAM_LISTEN_URL", "DeepgramTranscriber", "DeepgramTranscriptionError"]
