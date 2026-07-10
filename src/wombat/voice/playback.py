"""wombat.voice.playback — the local audio-playback seam cloud TTS providers ride (TK-191,
EP-31, CST-1).

``AudioPlayer`` is a minimal structural ``Protocol`` — one method, ``play`` — so a cloud
``TTSAdapter`` (``wombat.voice.tts.FishAudioTTSAdapter``) never depends on a concrete playback
library type; tests inject a recording fake, never touching real audio hardware.

``WinsoundPlayer`` is the ONE concrete adapter: Python's stdlib ``winsound`` module (Windows-only,
zero new dependency per CST-1 — Jim's laptop). ``import winsound`` happens LAZILY inside
``__init__``, NEVER at this module's top level, so merely importing ``wombat.voice.playback``
never fails on a non-Windows platform or a checkout that has not installed any extra. On a
non-Windows platform, constructing ``WinsoundPlayer`` raises the real, unmocked ``ImportError``
``winsound`` itself raises there — that IS the documented platform guard (CST-1); this module adds
no additional check.
"""

from __future__ import annotations

from typing import Protocol


class AudioPlayer(Protocol):
    """Something that can play a WAV clip aloud, once, synchronously."""

    def play(self, wav_bytes: bytes) -> None: ...


class WinsoundPlayer:
    """Local playback via the stdlib ``winsound`` module (Windows-only, CST-1). Lazily imports
    ``winsound`` at construction time; ``play()`` is a thin, blocking wrapper over
    ``winsound.PlaySound`` with ``SND_MEMORY`` (the in-memory-bytes playback mode)."""

    def __init__(self) -> None:
        import winsound  # lazy: only imported when actually constructing this player (CST-1)

        self._winsound = winsound

    def play(self, wav_bytes: bytes) -> None:
        """Play ``wav_bytes`` once, blocking until playback finishes."""
        self._winsound.PlaySound(wav_bytes, self._winsound.SND_MEMORY)


__all__ = ["AudioPlayer", "WinsoundPlayer"]
