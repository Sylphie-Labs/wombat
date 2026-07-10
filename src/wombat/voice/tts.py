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
pin per Q-104) with header ``Authorization: Bearer <api_key>`` and a JSON body carrying ``text``,
``reference_id`` (the configured ``voice_id``), and ``format: "wav"``. The response body is the
raw WAV audio bytes, played back via ONE ``player.play(...)`` call. ANY transport or player
failure RAISES — this module never catches: ``SpeakSink``'s existing broad-except path (CON-3,
``src/wombat/sinks/speak.py``) is what converts an adapter failure into a terminal ``Degraded``
without disturbing the already-composed/journaled text.

The default ``transport`` is a lazily-constructed ``HttpxVoiceTransport`` and the default
``player`` is a lazily-constructed ``WinsoundPlayer`` (both built at
``FishAudioTTSAdapter.__init__`` time when not injected) — so constructing a
``FishAudioTTSAdapter`` WITHOUT explicit fakes, on a checkout without the ``voice-cloud`` extra
or on a non-Windows platform, raises ``ImportError`` there; merely importing this module never
does (Q-46/Q-72).

DEC-28 (zero egress by default): nothing here is constructed anywhere in ``src`` outside of a
caller wombat doesn't yet have — this ticket only sets the pattern. TK-193 wires selection;
nothing here is reachable from boot.
"""

from __future__ import annotations

from wombat.voice.playback import AudioPlayer, WinsoundPlayer
from wombat.voice.transport import HttpxVoiceTransport, VoiceTransport

# Best-effort endpoint pin (Q-104) — truthed later by the DEF-7 live smoke.
FISH_AUDIO_TTS_URL = "https://api.fish.audio/v1/tts"


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
        """Speak ``text`` via ONE Fish Audio TTS POST followed by ONE playback call. Raises on a
        transport failure (``VoiceTransportError``, non-2xx response) or a player failure —
        never caught here; ``SpeakSink`` owns the degrade (CON-3)."""
        headers = {"Authorization": f"Bearer {self._api_key}"}
        json_body: dict[str, object] = {
            "text": text,
            "reference_id": self._voice_id,
            "format": "wav",
        }
        _status, wav_bytes = self._transport.post(
            FISH_AUDIO_TTS_URL, headers=headers, json=json_body
        )
        self._player.play(wav_bytes)


__all__ = ["FISH_AUDIO_TTS_URL", "FishAudioTTSAdapter"]
