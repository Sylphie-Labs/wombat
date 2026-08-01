"""wombat.voice.tts — cloud text-to-speech providers, behind the EXISTING ``sinks.tts_adapter.
TTSAdapter`` protocol (TK-191, EP-31, Q-100, Q-104).

This is TK-189's transport pattern applied to the TTS half (Q-104: homed separately from
``voice.stt`` so neither cloud direction has to cross-import the other's module just to make an
HTTP call). ``FishAudioTTSAdapter`` implements ``sinks.tts_adapter.TTSAdapter`` (``speak(text:
str) -> None``, synchronous, speaks once, may raise — verified at
``src/wombat/sinks/tts_adapter.py:23-26``) VERBATIM, so it drops straight into ``SpeakSink``
unchanged; nothing here touches ``sinks/speak.py``.

Q-104 ruling (binding) — key/voice sourcing: ``api_key`` and ``voice_id`` are PLAIN constructor
args. NO config/keyring/``resolve_provider_key`` reads happen in this module — resolving and
selecting a provider's key and voice is TK-193's job entirely. Jim's real Fish Audio reference id
(on record in Q-101/DEC-28 for the DEF-7 live smoke) is NEVER a code default here — it belongs
only to the future env/keyring-gated live smoke.

``speak()`` makes ONE POST to the Fish Audio TTS endpoint (``FISH_AUDIO_TTS_URL``, a best-effort
pin per Q-104) with headers ``Authorization: Bearer <api_key>`` and ``model: <model>`` (TK-326,
DEC-71a/DEC-72a — pins the Fish engine version; the adapter never sent this header before) and a
JSON body carrying ``text``, ``reference_id`` (the configured ``voice_id``), and ``format: "wav"``.
The response body is the raw WAV audio bytes, played back via ONE ``player.play(...)`` call. ANY
transport or player failure RAISES — this module never catches: ``SpeakSink``'s existing
broad-except path (CON-3, ``src/wombat/sinks/speak.py``) is what converts an adapter failure into
a terminal ``Degraded`` without disturbing the already-composed/journaled text.

The default ``transport`` is a lazily-constructed ``HttpxVoiceTransport`` and the default
``player`` is a lazily-constructed ``WinsoundPlayer`` (both built at
``FishAudioTTSAdapter.__init__`` time when not injected) — so constructing a
``FishAudioTTSAdapter`` WITHOUT explicit fakes, on a checkout without the ``voice-cloud`` extra
or on a non-Windows platform, raises ``ImportError`` there; merely importing this module never
does (Q-46/Q-72).

DEC-28 (zero egress by default): nothing here is constructed anywhere in ``src`` outside of a
caller wombat doesn't yet have — this ticket only sets the pattern. TK-193 wires selection;
nothing here is reachable from boot.

TK-192 (EP-31, Q-105(c)) adds the remaining two launch-roster providers on the SAME pattern:
``ElevenLabsTTSAdapter`` and ``DeepgramAuraTTSAdapter``. ElevenLabs' response body is HEADERLESS
16-bit mono 16 kHz PCM (no RIFF/WAV container), so ``ElevenLabsTTSAdapter.speak`` wraps it into a
proper WAV image via ``_wrap_pcm16_mono_16k_as_wav`` (stdlib ``wave`` over ``io.BytesIO`` — zero
new dependency, CST-1) before the ONE ``player.play(...)`` call; ``WinsoundPlayer.play`` requires a
full RIFF/WAV image (``PlaySound`` + ``SND_MEMORY``). Deepgram Aura's response is ALREADY a WAV
container (``container=wav`` in the request), so it is played back verbatim, unwrapped.

TK-332 (DEC-73a/d/e): ``FishAudioTTSAdapter`` gains an optional keyword-only ``writer_factory`` (a
zero-arg callable returning a ``voice.stream_playback.StreamingAudioWriter``, default ``None`` —
wired at the ``voice.select`` construction seam, TK-332). When a ``writer_factory`` is present AND
``transport`` satisfies ``StreamingVoiceTransport`` (a plain ``isinstance`` check against the
``runtime_checkable`` protocol), ``speak()`` rides ``transport.stream`` instead of ``transport.
post``: body ``format: "pcm"``, ``sample_rate`` IDENTITY with ``stream_playback.STREAM_SAMPLE_
RATE`` (never a literal — request and writer structurally cannot disagree), ``latency: "low"``,
the ``model`` header intact — chunks are written to the writer in arrival order and ``speak()``
returns ONLY after ``writer.finish()`` drains (blocking-until-audio-done is preserved, keeping the
DEC-64 on_spoken fire-after-heard contract at both call sites). ANY failure once a chunk has
actually been written to the writer (transport death mid-stream, or the writer's own ``write``/
``finish`` raising) calls ``writer.abort()`` first, then raises ``PartialSpeechError(played_any=
True)``; a failure before any chunk is ever written (a non-2xx ``stream()`` call, or the writer
failing on its very first chunk) propagates the underlying exception UNCHANGED — no wrapping, no
``played_any=True`` masquerade. Without a ``writer_factory`` (or a non-streaming ``transport``),
``speak()`` is the ORIGINAL buffered path below, byte-identical: ``format: "wav"`` request,
``player.play(...)`` — TK-262/TK-264 handling untouched as the degrade path.
"""

from __future__ import annotations

import io
import wave
from collections.abc import Callable

from wombat.voice import stream_playback
from wombat.voice.playback import AudioPlayer, WinsoundPlayer
from wombat.voice.stream_playback import StreamingAudioWriter
from wombat.voice.transport import HttpxVoiceTransport, StreamingVoiceTransport, VoiceTransport

# Best-effort endpoint pin (Q-104) — truthed later by the DEF-7 live smoke.
FISH_AUDIO_TTS_URL = "https://api.fish.audio/v1/tts"

# Best-effort endpoint pins (Q-105(c)) — truthed later by the DEF-7 live smoke.
ELEVENLABS_TTS_URL_TEMPLATE = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
DEEPGRAM_AURA_TTS_URL = "https://api.deepgram.com/v1/speak"
DEEPGRAM_AURA_DEFAULT_MODEL = "aura-asteria-en"


class PartialSpeechError(RuntimeError):
    """TK-332 (DEC-73e): raised by ``FishAudioTTSAdapter.speak``'s streaming path when playback
    fails AFTER at least one chunk has already been written to the writer — the user genuinely
    heard the steward start. ``played_any`` is ``True`` in every case this module actually raises
    (the writer is ``abort()``-ed first); the attribute exists so a caller (``SpeakSink``) can
    still distinguish a hypothetical ``played_any=False`` construction from ``True`` without
    special-casing. A failure BEFORE any chunk is written propagates the underlying exception
    unchanged instead — never wrapped here."""

    def __init__(self, *, played_any: bool) -> None:
        detail = "partial playback occurred" if played_any else "no audio was played"
        super().__init__(f"Fish streaming speech failed ({detail})")
        self.played_any = played_any


def _wrap_pcm16_mono_16k_as_wav(pcm_bytes: bytes) -> bytes:
    """Wrap headerless 16-bit mono 16 kHz PCM samples (ElevenLabs' ``output_format=pcm_16000``
    response body) into a full RIFF/WAV image via stdlib ``wave`` (CST-1: zero new dependency) —
    ``WinsoundPlayer.play`` requires a complete WAV image, not raw PCM."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(16000)
        wav_file.writeframes(pcm_bytes)
    return buffer.getvalue()


class FishAudioTTSAdapter:
    """Cloud TTS via the Fish Audio REST endpoint (Q-104) — Jim's own configured path. Implements
    ``sinks.tts_adapter.TTSAdapter`` structurally (one method, ``speak``) — no inheritance
    required, so it drops straight into ``SpeakSink`` alongside ``Pyttsx3Adapter``.
    """

    def __init__(
        self,
        api_key: str,
        *,
        voice_id: str,
        model: str,
        transport: VoiceTransport | None = None,
        player: AudioPlayer | None = None,
        writer_factory: Callable[[], StreamingAudioWriter] | None = None,
    ) -> None:
        self._api_key = api_key
        self._voice_id = voice_id
        self._model = model
        self._transport: VoiceTransport = (
            transport if transport is not None else HttpxVoiceTransport()
        )
        self._player: AudioPlayer = player if player is not None else WinsoundPlayer()
        # TK-332 (DEC-73a/d/f): the streaming writer factory, wired at the voice.select
        # construction seam iff stream_playback.streaming_available() there — None (the default)
        # preserves the buffered path byte-identically for every other caller (including every
        # pre-TK-332 test).
        self._writer_factory = writer_factory

    def speak(self, text: str) -> None:
        """Speak ``text``. TK-332: iff a ``writer_factory`` was injected AND ``transport``
        satisfies ``StreamingVoiceTransport``, rides the low-latency streaming path (see the
        module docstring); otherwise this is the ORIGINAL buffered path — ONE Fish Audio TTS POST
        followed by ONE playback call, byte-identical. Raises on a transport failure
        (``VoiceTransportError``, non-2xx response) or a player failure — never caught here;
        ``SpeakSink`` owns the degrade (CON-3)."""
        transport = self._transport
        if self._writer_factory is not None and isinstance(transport, StreamingVoiceTransport):
            self._speak_streaming(text, transport, self._writer_factory)
            return
        headers = {"Authorization": f"Bearer {self._api_key}", "model": self._model}
        json_body: dict[str, object] = {
            "text": text,
            "reference_id": self._voice_id,
            "format": "wav",
        }
        _status, wav_bytes = self._transport.post(
            FISH_AUDIO_TTS_URL, headers=headers, json=json_body
        )
        self._player.play(wav_bytes)

    def _speak_streaming(
        self,
        text: str,
        transport: StreamingVoiceTransport,
        writer_factory: Callable[[], StreamingAudioWriter],
    ) -> None:
        """TK-332 (DEC-73a/d/e): the streaming half of ``speak()``. ``format: "pcm"``,
        ``sample_rate`` IDENTITY with ``stream_playback.STREAM_SAMPLE_RATE``, ``latency: "low"``
        (pinned constants, DEC-63 no-knob) — the ``model`` header stays intact. Chunks are written
        to the writer in arrival order; ``finish()`` drains before this returns (blocking-until-
        audio-done, DEC-64). Any failure once at least one chunk has been written aborts the
        writer and raises ``PartialSpeechError(played_any=True)``; a failure before any chunk is
        written propagates unchanged."""
        headers = {"Authorization": f"Bearer {self._api_key}", "model": self._model}
        json_body: dict[str, object] = {
            "text": text,
            "reference_id": self._voice_id,
            "format": "pcm",
            "sample_rate": stream_playback.STREAM_SAMPLE_RATE,
            "latency": "low",
        }
        writer = writer_factory()
        played_any = False
        try:
            for chunk in transport.stream(FISH_AUDIO_TTS_URL, headers=headers, json=json_body):
                writer.write(chunk)
                played_any = True
            writer.finish()
        except Exception as exc:
            if played_any:
                writer.abort()
                raise PartialSpeechError(played_any=True) from exc
            raise


class ElevenLabsTTSAdapter:
    """Cloud TTS via the ElevenLabs REST endpoint (Q-105(c)). Implements
    ``sinks.tts_adapter.TTSAdapter`` structurally (one method, ``speak``) — no inheritance
    required, so it drops straight into ``SpeakSink`` alongside ``Pyttsx3Adapter`` /
    ``FishAudioTTSAdapter``."""

    def __init__(
        self,
        api_key: str,
        *,
        voice_id: str,
        model: str | None = None,
        transport: VoiceTransport | None = None,
        player: AudioPlayer | None = None,
    ) -> None:
        self._api_key = api_key
        self._voice_id = voice_id
        self._model = model
        self._transport: VoiceTransport = (
            transport if transport is not None else HttpxVoiceTransport()
        )
        self._player: AudioPlayer = player if player is not None else WinsoundPlayer()

    def speak(self, text: str) -> None:
        """Speak ``text`` via ONE ElevenLabs TTS POST, wrapping the returned headerless PCM into a
        WAV image, followed by ONE playback call. Raises on a transport failure
        (``VoiceTransportError``, non-2xx response) or a player failure — never caught here;
        ``SpeakSink`` owns the degrade (CON-3)."""
        headers = {"xi-api-key": self._api_key}
        json_body: dict[str, object] = {"text": text}
        if self._model is not None:
            json_body["model_id"] = self._model
        url = (
            f"{ELEVENLABS_TTS_URL_TEMPLATE.format(voice_id=self._voice_id)}"
            "?output_format=pcm_16000"
        )
        _status, pcm_bytes = self._transport.post(url, headers=headers, json=json_body)
        wav_bytes = _wrap_pcm16_mono_16k_as_wav(pcm_bytes)
        self._player.play(wav_bytes)


class DeepgramAuraTTSAdapter:
    """Cloud TTS via the Deepgram Aura REST endpoint (Q-105(c)). Implements
    ``sinks.tts_adapter.TTSAdapter`` structurally (one method, ``speak``) — no inheritance
    required, so it drops straight into ``SpeakSink`` alongside ``Pyttsx3Adapter`` /
    ``FishAudioTTSAdapter``."""

    def __init__(
        self,
        api_key: str,
        *,
        voice_id: str | None = None,
        transport: VoiceTransport | None = None,
        player: AudioPlayer | None = None,
    ) -> None:
        self._api_key = api_key
        self._voice_id = voice_id
        self._transport: VoiceTransport = (
            transport if transport is not None else HttpxVoiceTransport()
        )
        self._player: AudioPlayer = player if player is not None else WinsoundPlayer()

    def speak(self, text: str) -> None:
        """Speak ``text`` via ONE Deepgram Aura TTS POST followed by ONE playback call — the
        response is already a WAV container, played back verbatim. Raises on a transport failure
        (``VoiceTransportError``, non-2xx response) or a player failure — never caught here;
        ``SpeakSink`` owns the degrade (CON-3)."""
        headers = {"Authorization": f"Token {self._api_key}"}
        json_body: dict[str, object] = {"text": text}
        model = self._voice_id if self._voice_id is not None else DEEPGRAM_AURA_DEFAULT_MODEL
        url = (
            f"{DEEPGRAM_AURA_TTS_URL}?model={model}&encoding=linear16"
            "&sample_rate=24000&container=wav"
        )
        _status, wav_bytes = self._transport.post(url, headers=headers, json=json_body)
        self._player.play(wav_bytes)


__all__ = [
    "DEEPGRAM_AURA_DEFAULT_MODEL",
    "DEEPGRAM_AURA_TTS_URL",
    "ELEVENLABS_TTS_URL_TEMPLATE",
    "FISH_AUDIO_TTS_URL",
    "DeepgramAuraTTSAdapter",
    "ElevenLabsTTSAdapter",
    "FishAudioTTSAdapter",
    "PartialSpeechError",
]
