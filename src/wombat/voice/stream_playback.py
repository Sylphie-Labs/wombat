"""wombat.voice.stream_playback — the local streaming-playback seam Fish's chunked TTS response
rides (TK-331, EP-31, DEC-73c).

This module OWNS the ONE shared streaming-format constant, ``STREAM_SAMPLE_RATE``: TK-332's Fish
streaming request reads this exact value for its ``sample_rate`` field, so the request and this
writer structurally cannot disagree (DEC-73d).

``StreamingAudioWriter`` opens ONE raw-PCM output stream (16-bit signed mono at
``STREAM_SAMPLE_RATE`` — 2 bytes per frame) via ``sounddevice.RawOutputStream``. ``__init__``
performs the LAZY ``sounddevice`` import (Q-46/Q-72 precedent) ONLY when no ``stream_factory`` is
injected — raising the real, unmocked ``ImportError`` there when the ``voice-cloud`` extra is
absent — but NEVER opens the audio device at construction time; the real stream opens on the
FIRST ``write()`` call, built by a factory closure captured at construction. Every unit test
injects its own fake ``stream_factory``, so ``sounddevice`` need not even be installed to run the
plain suite, and no test ever touches real audio hardware (DEF-7).

FRAME DISCIPLINE (``write()``): raw PCM carries no header to parse, so the TK-262/TK-264
poisoned-WAV class structurally cannot occur on this path — but a torn (odd-byte) frame must
still never be handed to the stream. Incoming bytes are cut to the largest whole-frame multiple;
any remainder is carried forward and prepended to the next ``write()`` call, never submitted on
its own.

``finish()`` blocks until every queued frame is audibly drained (``stream.stop()`` — the real
``sounddevice`` blocking-mode contract, DEC-73c) before closing the stream. ``abort()`` stops
immediately WITHOUT draining (``stream.abort()``). Neither call is a no-op only when a stream was
actually opened; calling either before any ``write()`` is a harmless no-op. ANY fake-stream (or
real stream) write failure RAISES, never swallowed — CON-3: the caller owns the degrade.

CRASH POSTURE (DEC-73c, recorded): in-process for v1 — the July winsound segfault class was
native RIFF-header parsing of poisoned bytes, which raw fixed-format PCM writing never performs.
The escalation trigger is already recorded, not built here: any native fault attributed to this
writer moves it behind the DEC-54 subprocess-isolation pattern, no re-litigation.

``streaming_available()`` attempts the lazy ``sounddevice`` import ONLY — never constructing a
factory or opening a stream — so TK-332's Fish adapter branch can probe availability without ever
touching audio hardware during boot.

MODULE SEPARATION: this module is OUTPUT-ONLY. It opens no microphone/capture stream and names
no capture-side audio-library symbol anywhere in its source (a structural test enforces this —
see ``tests/voice/test_stream_playback.py`` for the exact forbidden-token list). The DEC-68a
``observe_mic.py`` no-capture structural pin lives in a DIFFERENT module and stays untouched by
this ticket.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

#: The one shared streaming-format constant (DEC-73d) — TK-332's Fish streaming request reads
#: this exact value for its ``sample_rate`` field; request and writer structurally cannot
#: disagree. 16-bit mono PCM.
STREAM_SAMPLE_RATE = 44100

#: 16-bit mono PCM: exactly one int16 sample per frame, 2 bytes. Every buffer handed to the
#: stream must be a whole multiple of this (frame discipline, DEC-73c).
FRAME_BYTES = 2


class AudioOutputStream(Protocol):
    """A raw-PCM output stream, structurally matching ``sounddevice.RawOutputStream``'s real
    method surface (``write``/``stop``/``abort``/``close``) — so the production factory needs no
    adapter shim, and tests inject a recording fake instead, never real audio hardware."""

    def write(self, data: bytes) -> None: ...
    def stop(self) -> None: ...
    def abort(self) -> None: ...
    def close(self) -> None: ...


#: A zero-arg callable that opens and returns a fresh ``AudioOutputStream`` — invoked at most
#: once per ``StreamingAudioWriter``, lazily, on the first ``write()`` call.
StreamFactory = Callable[[], AudioOutputStream]


class StreamingAudioWriter:
    """Writes raw 16-bit mono PCM chunks to ONE ``sounddevice`` output stream, opened lazily on
    first ``write()`` (DEC-73c). Every unit test injects ``stream_factory`` to drive a fake
    stream — real audio hardware is never touched outside production use."""

    def __init__(self, *, stream_factory: StreamFactory | None = None) -> None:
        if stream_factory is not None:
            self._stream_factory: StreamFactory = stream_factory
        else:
            import sounddevice  # lazy import (Q-46/Q-72 precedent) — voice-cloud extra

            self._stream_factory = lambda: sounddevice.RawOutputStream(
                samplerate=STREAM_SAMPLE_RATE, channels=1, dtype="int16"
            )
        self._stream: AudioOutputStream | None = None
        self._pending: bytes = b""

    def _opened_stream(self) -> AudioOutputStream:
        if self._stream is None:
            self._stream = self._stream_factory()
        return self._stream

    def write(self, chunk: bytes) -> None:
        """Submit ``chunk`` for playback. Cuts the (carried-remainder-prefixed) bytes to the
        largest whole-frame multiple and submits ONLY that; any leftover remainder is carried
        forward into the next call — a torn frame is never submitted (frame discipline,
        DEC-73c). Raises whatever the underlying stream's ``write`` raises — never caught here
        (CON-3, the caller owns the degrade)."""
        data = self._pending + chunk
        whole_length = (len(data) // FRAME_BYTES) * FRAME_BYTES
        to_write = data[:whole_length]
        self._pending = data[whole_length:]
        if to_write:
            self._opened_stream().write(to_write)

    def finish(self) -> None:
        """Block until every queued frame is audibly drained (``stream.stop()``, DEC-73c), then
        close the stream. A no-op when nothing was ever written."""
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
        self._stream = None
        self._pending = b""

    def abort(self) -> None:
        """Stop immediately WITHOUT draining queued frames (``stream.abort()``), then close the
        stream. A no-op when nothing was ever written."""
        if self._stream is not None:
            self._stream.abort()
            self._stream.close()
        self._stream = None
        self._pending = b""


def streaming_available() -> bool:
    """Attempt the lazy ``sounddevice`` import ONLY — never opening a stream — so a caller (the
    TK-332 Fish adapter branch) can probe availability without ever touching audio hardware."""
    try:
        import sounddevice  # noqa: F401  — probe-only lazy import, nothing opened
    except ImportError:
        return False
    return True


__all__ = [
    "FRAME_BYTES",
    "STREAM_SAMPLE_RATE",
    "AudioOutputStream",
    "StreamFactory",
    "StreamingAudioWriter",
    "streaming_available",
]
