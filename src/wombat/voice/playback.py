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

``play()`` validates ``wav_bytes`` BEFORE ever handing them to ``winsound.PlaySound`` (TK-262,
DEC-53a, ISS-15): ``winsound.PlaySound`` trusts RIFF-declared sizes blindly and takes a native
access violation on truncated/malformed buffers, killing the whole process with no traceback.
``wave.open`` over an ``io.BytesIO`` wrapper proves RIFF/WAVE framing, ``fmt`` sanity, and readable
params; an explicit check on top of that proves the declared frame data does not overrun the
actual buffer length (``wave`` itself does not check this — it will happily report parameters read
from a truncated header). Any defect raises ``ValueError`` naming the defect class and the byte
length, before ``PlaySound`` is ever called.
"""

from __future__ import annotations

import io
import wave
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
        """Play ``wav_bytes`` once, blocking until playback finishes.

        Raises ``ValueError`` (TK-262, DEC-53a) — naming the defect class and the byte length —
        before ``winsound.PlaySound`` is ever called, iff ``wav_bytes`` is empty, is not valid
        RIFF/WAVE framing, or declares frame data that overruns the buffer's actual length.
        """
        _validate_wav_bytes(wav_bytes)
        self._winsound.PlaySound(wav_bytes, self._winsound.SND_MEMORY)


def _validate_wav_bytes(wav_bytes: bytes) -> None:
    """Prove ``wav_bytes`` is playable WAV audio, or raise ``ValueError`` naming the defect class
    and the byte length (TK-262, DEC-53a) — called strictly before any native playback call."""
    length = len(wav_bytes)
    if length == 0:
        msg = f"empty audio buffer ({length} bytes): cannot play"
        raise ValueError(msg)

    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as wav:
            nframes = wav.getnframes()
            nchannels = wav.getnchannels()
            sampwidth = wav.getsampwidth()
    except (wave.Error, EOFError) as exc:
        msg = f"malformed WAV framing ({length} bytes): {exc}"
        raise ValueError(msg) from exc

    declared_data_bytes = nframes * nchannels * sampwidth
    if declared_data_bytes > length:
        msg = (
            f"truncated WAV audio ({length} bytes): declared frame data of "
            f"{declared_data_bytes} bytes overruns the buffer"
        )
        raise ValueError(msg)


__all__ = ["AudioPlayer", "WinsoundPlayer"]
