"""wombat.voice.remote_sinks — BufferedUtteranceSink, SealedUtteranceStore, and the GET
/v1/utterance wire adapter UtteranceFetchHandler (TK-343, DEC-79, wire-contract.md §5).

``BufferedUtteranceSink`` is a fourth ``voice.stream_playback.AudioOutputStream`` implementation
— the SAME four-method shape ``StreamingAudioWriter`` already drives ``sounddevice.
RawOutputStream`` through — so it drops into ``StreamingAudioWriter(stream_factory=...)`` exactly
where the real hardware stream normally opens. Frame discipline (never handed a torn odd-byte
chunk), the headerless raw-PCM wire format, and ``STREAM_SAMPLE_RATE`` are all INHERITED from
``StreamingAudioWriter``/``voice.tts`` — this module re-derives none of them: it only accumulates
whatever whole frames ``StreamingAudioWriter.write`` already cut and hands it, then seals+publishes
that buffer on ``stop()``.

``SealedUtteranceStore`` is the single-slot, TTL-bound, single-fetch-then-discard buffer
``BufferedUtteranceSink.stop`` publishes into and ``GET /v1/utterance`` (via
``UtteranceFetchHandler``) drains — mirrors ``voice.turn_origin.LastTurnOriginRegister``'s shape
(single slot, injected clock, in-memory, restart-forgets) but with the wire spec's OWN pinned
freshness window: ``devices.surface.UTTERANCE_TTL_SECONDS`` (DEC-83 §4/§5 — the SAME constant a
device reads off ``GET /v1/health``, never a second literal).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from wombat.devices.surface import UTTERANCE_TTL_SECONDS
from wombat.voice.stream_playback import STREAM_SAMPLE_RATE

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SealedUtterance:
    """One synthesized, not-yet-fetched remote reply — exactly what ``GET /v1/utterance`` (§5)
    serves: the server-minted ``utterance_id`` (the SAME one ``POST /v1/voice`` returned for the
    originating turn), the ORIGINATING device's id (never the fetcher's — wire-contract.md §5's
    load-bearing cross-device fallback distinction), and the raw headerless PCM bytes."""

    utterance_id: str
    origin_device_id: str
    pcm: bytes


class SealedUtteranceStore:
    """Single-slot, TTL-bound, single-fetch-then-discard store for ONE sealed remote reply at a
    time (TK-343, wire-contract.md §5). ``publish`` (newest wins, mirrors every other single-slot
    register in this codebase) is called by ``BufferedUtteranceSink.stop``; ``take`` is called by
    ``UtteranceFetchHandler`` on every ``GET /v1/utterance``. No disk, no queue, no second slot —
    an unfetched utterance is simply gone once ``ttl_seconds`` passes, exactly as gone as one that
    was already fetched."""

    def __init__(
        self, *, clock: Callable[[], float], ttl_seconds: float = UTTERANCE_TTL_SECONDS
    ) -> None:
        self._clock = clock
        self._ttl_seconds = ttl_seconds
        self._utterance: SealedUtterance | None = None
        self._sealed_at: float | None = None

    def publish(self, utterance: SealedUtterance) -> None:
        self._utterance = utterance
        self._sealed_at = self._clock()

    def take(self) -> SealedUtterance | None:
        """Claim and CLEAR the slot. ``None`` when empty or aged past ``ttl_seconds`` (§5: this is
        the ORDINARY not-yet-answer, never an error) — either way the slot is left empty, so an
        immediate repeat call always finds nothing (single-fetch-then-discard)."""
        if self._utterance is None or self._sealed_at is None:
            return None
        utterance, sealed_at = self._utterance, self._sealed_at
        self._utterance = None
        self._sealed_at = None
        age_seconds = self._clock() - sealed_at
        if age_seconds > self._ttl_seconds:
            # TK-343 AC7 repair: SpeakSink already fired on_spoken/updated LastSpokenRegister for
            # this utterance the instant it was sealed -- an expiry that reaches HERE means the
            # device never fetched it, i.e. wombat recorded a spoken reply nobody heard. ONE loud
            # warning names it rather than the silent discard this branch had before.
            logger.warning(
                "voice: sealed utterance %r (origin device %r) expired unfetched after %.1fs -- "
                "the reply was recorded as spoken but never delivered",
                utterance.utterance_id,
                utterance.origin_device_id,
                age_seconds,
            )
            return None
        return utterance


class BufferedUtteranceSink:
    """A ``voice.stream_playback.AudioOutputStream`` that accumulates raw PCM in memory instead of
    opening real audio hardware (TK-343). Constructed fresh per utterance (via the ``stream_
    factory`` closure ``voice.select`` wires into a ``StreamingAudioWriter``) over a fixed
    ``origin_device_id``/``utterance_id`` pair claimed from ``turn_origin.LastTurnOriginRegister``
    at that same moment.

    ``write`` only ever receives whole frames — ``StreamingAudioWriter.write`` already cuts any
    carried-remainder odd byte before ever calling a stream's ``write`` (frame discipline lives
    THERE, never re-implemented here). ``stop`` (``StreamingAudioWriter.finish``'s call) seals the
    accumulated bytes and publishes them; ``abort`` (a mid-stream failure) discards them — an
    aborted utterance is never published, never partially visible to a fetch. ``close`` is a
    no-op: there is no real device handle to release."""

    def __init__(
        self, *, origin_device_id: str, utterance_id: str, store: SealedUtteranceStore
    ) -> None:
        self._origin_device_id = origin_device_id
        self._utterance_id = utterance_id
        self._store = store
        self._buffer = bytearray()

    def write(self, data: bytes) -> None:
        self._buffer += data

    def stop(self) -> None:
        self._store.publish(
            SealedUtterance(
                utterance_id=self._utterance_id,
                origin_device_id=self._origin_device_id,
                pcm=bytes(self._buffer),
            )
        )

    def abort(self) -> None:
        self._buffer = bytearray()

    def close(self) -> None:
        pass


class UtteranceFetchHandler:
    """``GET /v1/utterance`` (TK-343, R1, wire-contract.md §5): the OPTIONAL handler object
    ``devices.surface.DeviceSurface`` accepts as its ``utterance_fetch_handler`` kwarg — a thin
    wire adapter over ONE ``SealedUtteranceStore``, the SAME instance
    ``BufferedUtteranceSink`` publishes into. Constructed at the composition root iff
    ``config.wombat_remote_voice`` is true; ``None`` leaves the route indistinguishable from an
    unknown path (DEC-78(b))."""

    def __init__(self, *, store: SealedUtteranceStore) -> None:
        self._store = store

    async def handle(self) -> tuple[int, dict[str, str], bytes]:
        """``None`` slot -> ``(204, {}, b"")`` (§5: the ordinary not-yet answer, never an error).
        A sealed utterance -> ``(200, <the five wire headers>, <raw PCM>)``."""
        sealed = self._store.take()
        if sealed is None:
            return 204, {}, b""
        headers = {
            "Content-Type": "application/octet-stream",
            "X-Wombat-Utterance-Id": sealed.utterance_id,
            "X-Wombat-Origin-Device-Id": sealed.origin_device_id,
            "X-Wombat-Sample-Rate-Hz": str(STREAM_SAMPLE_RATE),
            "X-Wombat-Audio-Format": "pcm_s16le",
            "X-Wombat-Channels": "1",
        }
        return 200, headers, sealed.pcm


__all__ = [
    "BufferedUtteranceSink",
    "SealedUtterance",
    "SealedUtteranceStore",
    "UtteranceFetchHandler",
]
