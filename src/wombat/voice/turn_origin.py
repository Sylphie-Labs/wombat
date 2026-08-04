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

TK-343 critical repair: ``claims_suppressed()`` is a context manager ``sinks.speak.SpeakSink``
uses to protect a claimed-but-unrelated origin from a PROACTIVE (gate-surfaced) item speaking
through the SAME shared adapter/writer_factory chain as a genuine voice-turn reply. Without it, a
proactive surfacing that happens to speak between a ``POST /v1/voice`` accept and that turn's own
reply would call ``take()`` first and steal the fresh origin — sealing UNSOLICITED audio for the
watch (wire-contract §5 / AC5 violation) while the real reply, finding the slot empty, plays
locally. ``take()`` itself (and every existing caller/test of it) is UNCHANGED — the suppression
default (``True``, i.e. claims permitted) preserves ``take()``'s exact prior behavior for any
caller that never enters ``claims_suppressed()``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
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
        # TK-343 critical repair: True (claims permitted) is the byte-identical default every
        # existing/direct take() caller sees -- only claims_suppressed() below ever flips it.
        self._claim_permitted = True

    @contextmanager
    def claims_suppressed(self) -> Iterator[None]:
        """TK-343 critical repair: within this ``with`` block, ``take()`` returns ``None``
        WITHOUT touching whatever the slot currently holds — a call made here can never steal a
        fresh origin meant for a later, eligible caller. Restores the PRIOR permitted-ness
        afterward (not unconditionally ``True``) even if the wrapped code raises, so nesting is
        harmless."""
        previous = self._claim_permitted
        self._claim_permitted = False
        try:
            yield
        finally:
            self._claim_permitted = previous

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
        took.

        TK-343 critical repair: inside a ``claims_suppressed()`` block, returns ``None``
        immediately WITHOUT touching the slot at all — a suppressed call leaves a genuinely fresh
        origin untouched for whichever LATER, non-suppressed call claims it."""
        if not self._claim_permitted:
            return None
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
