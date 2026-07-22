"""LastSpokenRegister — the runtime's memory of what the Steward just said aloud (TK-288, DEC-64
gap A, half 1).

A single-slot, newest-wins, in-memory record fed by ``on_spoken`` hooks at both speak sites
(``sinks/speak.py``'s ``SpeakSink`` and ``stages/brief_deliver_stage.py``'s ``BriefDeliverStage``)
— each fires ONLY after its TTS call has actually returned without raising, i.e. only when the
Steward has genuinely been heard (never on a silent/voice-off/degraded/failed path). This module
owns ONLY the register itself; wiring the hooks into the two speak sites and threading ONE shared
instance through ``bootstrap.assemble_runtime`` is done at those call sites, not here.

DEC-63 no-knob precedent: NO config/params field. ``_MAX_SPOKEN_CHARS``/``LAST_SPOKEN_TTL_SECONDS``
are pinned module constants, not operator-tunable. NO persistence — a reply across a process
restart isn't a conversation (steered explicitly); the slot resets to empty every boot. NO
locking — every touch point (both speak sites, and TK-289's later PTT-reply consumer) runs on the
SAME event loop, so a plain mutable object is safe exactly like ``ChatReplyBroker``'s own
in-memory dicts.
"""

from __future__ import annotations

from collections.abc import Callable

# Pinned (DEC-63 no-knob precedent): NOT an operating param. Long enough to carry a real spoken
# reply's gist, short enough to never let a single note balloon the register's footprint.
_MAX_SPOKEN_CHARS = 600

# Pinned (DEC-63 no-knob precedent): NOT an operating param. TK-289's PTT-reply consumer treats
# anything spoken longer ago than this as stale context — "yes, do that" said five minutes later
# no longer refers to it.
LAST_SPOKEN_TTL_SECONDS = 120.0


class LastSpokenRegister:
    """Single-slot, newest-wins record of the last thing spoken aloud.

    ``clock`` returns epoch seconds (mirrors ``gate.pipeline.Clock``/``sources.presence``'s own
    epoch-seconds clock idiom) — injected so tests can fake elapsed time without real sleeps.
    """

    def __init__(self, *, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._item_id: str | None = None
        self._text: str | None = None
        self._spoken_at: float | None = None

    def note_spoken(self, item_id: str, text: str) -> None:
        """Record ``text`` (truncated to ``_MAX_SPOKEN_CHARS``) as the latest spoken utterance —
        newest wins, replacing whatever the single slot held before."""
        self._item_id = item_id
        self._text = text[:_MAX_SPOKEN_CHARS]
        self._spoken_at = self._clock()

    def current(self) -> str | None:
        """The last spoken text, IFF its age is within ``LAST_SPOKEN_TTL_SECONDS``; ``None`` when
        nothing has been spoken yet, or the slot has aged out."""
        if self._text is None or self._spoken_at is None:
            return None
        age = self._clock() - self._spoken_at
        if age > LAST_SPOKEN_TTL_SECONDS:
            return None
        return self._text


__all__ = ["LAST_SPOKEN_TTL_SECONDS", "LastSpokenRegister"]
