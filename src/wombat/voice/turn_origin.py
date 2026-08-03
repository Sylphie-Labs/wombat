"""wombat.voice.turn_origin — LastTurnOriginRegister: the runtime's memory of which remote device
originated the turn currently in flight (TK-343, DEC-79).

Mirrors ``voice.reply_context.LastSpokenRegister`` in SHAPE — a single-slot, in-memory,
injected-epoch-seconds-clock record that restarts forget entirely (no persistence, exactly like
``LastSpokenRegister`` — a reply across a process restart isn't a conversation). It threads state
between the SAME two kinds of points DEC-64 already threads state between: a WRITE at
``devices.voice_ingest.VoiceIngestHandler.handle``'s ``POST /v1/voice`` accept (the only writer —
nothing else ever calls ``note_origin``), and a READ at ``voice.select``'s writer_factory closure
(the composition seam that decides, per utterance, whether THIS reply rides the remote sink).

``take()`` is deliberately a CONSUMING read, unlike ``LastSpokenRegister.current()``'s repeatable
peek — this is the one place the two registers' shapes diverge, and the divergence is load-bearing
rather than incidental: ``LastTurnOriginRegister``'s entire job is handing off exactly ONE claim to
whichever reply speaks next, so that a remote turn's own reply and an unrelated later reply (a
second turn from the laptop, TK-343 AC2) provably route differently from the SAME closure without
either caller ever comparing item identities (which the frozen ``voice.tts``/``sinks.speak`` call
shapes have no room to carry). Once claimed — or once aged past ``ttl_seconds`` — the slot is empty
until the next ``note_origin``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import NamedTuple

# Pinned default (mirrors reply_context.LAST_SPOKEN_TTL_SECONDS): "near the DEC-64 reply window".
# Threaded from config.wombat_reply_window_seconds at bootstrap.assemble_runtime, exactly like
# LastSpokenRegister's own ttl_seconds — every call site that omits it gets this default.
TURN_ORIGIN_TTL_SECONDS = 120.0


class TurnOrigin(NamedTuple):
    """One claimed remote turn's origin: the device that sent it, and the server-minted
    ``utterance_id`` ``VoiceIngestHandler.handle`` returned for that same POST /v1/voice."""

    device_id: str
    utterance_id: str


class LastTurnOriginRegister:
    """Single-slot record of the most recently accepted remote voice turn's origin.

    ``clock`` returns epoch seconds (mirrors ``LastSpokenRegister``'s own injected clock).
    ``ttl_seconds`` (keyword-only, default ``TURN_ORIGIN_TTL_SECONDS``) is the freshness window
    ``take()`` reads against.
    """

    def __init__(
        self, *, clock: Callable[[], float], ttl_seconds: float = TURN_ORIGIN_TTL_SECONDS
    ) -> None:
        self._clock = clock
        self._ttl_seconds = ttl_seconds
        self._device_id: str | None = None
        self._utterance_id: str | None = None
        self._noted_at: float | None = None

    def note_origin(self, device_id: str, utterance_id: str) -> None:
        """Record ``device_id``/``utterance_id`` as the newest claimable origin — newest wins,
        replacing whatever the single slot held before (even an as-yet-unclaimed one)."""
        self._device_id = device_id
        self._utterance_id = utterance_id
        self._noted_at = self._clock()

    def take(self) -> TurnOrigin | None:
        """Claim and CLEAR the slot. Returns the origin IFF one was present and its age is within
        ``ttl_seconds``; ``None`` when nothing has been noted, the slot was already claimed, or it
        has aged out — and in every one of those cases the slot is left (or confirmed) empty, so a
        second call in a row always returns ``None`` regardless of which branch the first call
        took."""
        if self._device_id is None or self._utterance_id is None or self._noted_at is None:
            return None
        device_id, utterance_id, noted_at = self._device_id, self._utterance_id, self._noted_at
        self._device_id = None
        self._utterance_id = None
        self._noted_at = None
        if self._clock() - noted_at > self._ttl_seconds:
            return None
        return TurnOrigin(device_id=device_id, utterance_id=utterance_id)


__all__ = ["TURN_ORIGIN_TTL_SECONDS", "LastTurnOriginRegister", "TurnOrigin"]
