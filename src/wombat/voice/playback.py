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

Before that validation runs, ``play()`` first calls ``_normalize_sentinel_sizes`` (TK-264, ISS-17):
Fish.audio's streaming WAV encoder writes an unknown-length SENTINEL into the RIFF and/or ``data``
chunk size fields (observed live: ``0xFFFFFF00``) over an otherwise complete, self-consistent body.
``wave.open`` does not reject this — it derives a huge ``nframes`` from the sentinel and *succeeds*,
so it is the TK-262 overrun check that would otherwise reject every such response. If (and only if)
a size field's value is exactly one of the known sentinel values, it is rewritten to the buffer's
actual length; any other value (including a genuinely truncated declaration) is left untouched and
still flows into validation unmodified — the TK-262 rejection contract stands verbatim.
"""

from __future__ import annotations

import io
import wave
from typing import Protocol

#: Streaming-encoder unknown-length sentinels (TK-264, ISS-17) — the all-ones-truncated class.
#: Kept as an explicit, closed set (no range heuristics): only these exact values are rewritten.
_SENTINEL_SIZES = frozenset({0xFFFFFFFF, 0xFFFFFF00})


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

        First patches any unknown-length SENTINEL RIFF/``data`` size fields to the buffer's
        actual lengths (TK-264, ISS-17). Then raises ``ValueError`` (TK-262, DEC-53a) — naming
        the defect class and the byte length — before ``winsound.PlaySound`` is ever called, iff
        the (possibly-patched) bytes are empty, are not valid RIFF/WAVE framing, or declare frame
        data that overruns the buffer's actual length.
        """
        wav_bytes = _normalize_sentinel_sizes(wav_bytes)
        _validate_wav_bytes(wav_bytes)
        self._winsound.PlaySound(wav_bytes, self._winsound.SND_MEMORY)


def _normalize_sentinel_sizes(wav_bytes: bytes) -> bytes:
    """Rewrite RIFF/``data`` chunk size fields that carry a known unknown-length SENTINEL
    (TK-264, ISS-17) to the buffer's actual byte lengths; every other value — including a
    genuinely truncated declaration — passes through untouched.

    Returns ``wav_bytes`` UNCHANGED (same object) when the buffer is not parseable as RIFF/WAVE,
    when no ``data`` sub-chunk can be located, or when neither size field carries a sentinel
    value — so validation raises its existing, unmodified errors on anything this cannot repair.
    """
    if len(wav_bytes) < 12 or wav_bytes[0:4] != b"RIFF" or wav_bytes[8:12] != b"WAVE":
        return wav_bytes

    pos = 12
    data_size_offset: int | None = None
    data_start: int | None = None
    while pos + 8 <= len(wav_bytes):
        chunk_id = wav_bytes[pos : pos + 4]
        chunk_size = int.from_bytes(wav_bytes[pos + 4 : pos + 8], "little")
        chunk_data_start = pos + 8
        if chunk_id == b"data":
            data_size_offset = pos + 4
            data_start = chunk_data_start
            break
        pos = chunk_data_start + chunk_size + (chunk_size % 2)  # chunks are word-padded

    if data_size_offset is None or data_start is None:
        return wav_bytes

    riff_size = int.from_bytes(wav_bytes[4:8], "little")
    data_size = int.from_bytes(wav_bytes[data_size_offset : data_size_offset + 4], "little")
    patch_riff = riff_size in _SENTINEL_SIZES
    patch_data = data_size in _SENTINEL_SIZES
    if not patch_riff and not patch_data:
        return wav_bytes

    patched = bytearray(wav_bytes)
    if patch_riff:
        patched[4:8] = (len(wav_bytes) - 8).to_bytes(4, "little")
    if patch_data:
        actual_data_bytes = len(wav_bytes) - data_start
        patched[data_size_offset : data_size_offset + 4] = actual_data_bytes.to_bytes(4, "little")
    return bytes(patched)


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
