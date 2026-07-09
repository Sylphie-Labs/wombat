"""wombat.user_model.feedback_source — explicit-feedback capture channel (TK-51, EP-12,
Q-20/ISS-6, Q-86 ruling).

CAPTURE ONLY: this module turns a one-line "was this useful?" response into a queued,
durably-parseable ``FeedbackSignal``. It does NOT decide an outcome (the useful ->
``OUTCOME_LOAD_BEARING`` / not_useful -> ``OUTCOME_REGRETTED`` fold is TK-50's job, same
batch), does NOT write a claim (TK-45/TK-175), and does NOT wire live emission or boot
registration (TK-175 owns both). ``response`` is a CLOSED ``useful``/``not_useful`` enum —
never free text, never a motive (NG-1).

FRAME: dependency-light by design (Q-86 ruling 1) — TK-50 imports ``FeedbackSignal`` from
THIS module, so it stays free of cog-worx imports and I/O at import time.

Three pieces:
  * ``feedback_affordance(item_ref)`` — a pure function producing the one-line prompt.
    Its live emission point (the trail renderer / a stage) does not exist yet; TK-175 owns
    wiring it in.
  * ``FeedbackSignal`` — the frozen, JSON-native wire type. The ``SourceEvent`` payload a
    ``FeedbackInputSource`` produces is exactly ``FeedbackSignal.to_payload()``:
    ``{"kind": "feedback", "item_ref": ..., "response": ...}`` (Q-49). Q-86 ruling 4:
    ``event_key`` is ``"<item_ref>:<response>"`` — a repeated identical response dedups via
    queue idempotency; a changed answer is a new item.
  * ``FeedbackInputSource`` — a ``PushSource`` (TK-161) registered under id ``"feedback"``.
    The push channel (inherited ``push(SourceEvent)``) is the SAME entry a later spoken
    yes/no will use (ASR TK-162). The v1 file channel is an OPTIONAL ``feedback_file`` ctor
    arg (``None`` -> no-op, CON-3): when set, ``poll()`` additionally reads lines newly
    appended to that file since the previous poll, parsing each with the deterministic
    grammar ``"<item_ref> y|n"`` (also accepts ``yes``/``no``, case-insensitive). A
    malformed line is logged as a warning and skipped — it never raises, so one bad line
    can never kill the poll loop.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from wombat.sources.base import PushSource, SourceEvent

_log = logging.getLogger(__name__)

FeedbackResponse = Literal["useful", "not_useful"]

_AFFORDANCE_QUESTION = "was this useful? [y/n]"


def feedback_affordance(item_ref: str) -> str:
    """The pure affordance token (Q-86 ruling 3, AC1): one line containing both the literal
    question text ``"was this useful? [y/n]"`` and ``item_ref``, recoverable from the line by
    simple containment. This function has no live emission point yet — TK-175 owns wiring it
    into the trail renderer / a stage; this ticket only defines the token."""
    return f"{_AFFORDANCE_QUESTION} {item_ref}"


def _validate_response(value: str) -> FeedbackResponse:
    """The one place a raw string is checked against the closed response vocabulary."""
    if value == "useful":
        return "useful"
    if value == "not_useful":
        return "not_useful"
    raise ValueError(f"FeedbackSignal: response must be 'useful' or 'not_useful', got {value!r}")


@dataclass(frozen=True, slots=True)
class FeedbackSignal:
    """One captured explicit-feedback response (Q-86 ruling 1).

    ``item_ref`` is the canonical TK-12 identity string (``ItemRef.idempotency_key()``);
    ``response`` is the CLOSED ``useful``/``not_useful`` vocabulary — never free text (NG-1).
    """

    item_ref: str
    response: FeedbackResponse

    def __post_init__(self) -> None:
        _validate_response(self.response)

    def event_key(self) -> str:
        """Q-86 ruling 4: ``"<item_ref>:<response>"`` — a repeated identical response dedups
        via queue idempotency; a changed answer derives a different key (a new item)."""
        return f"{self.item_ref}:{self.response}"

    def to_payload(self) -> dict[str, Any]:
        """JSON-native wire form (Q-49) — the exact ``SourceEvent`` payload shape a
        ``FeedbackInputSource`` produces. The one shape ``from_payload`` round-trips exactly."""
        return {"kind": "feedback", "item_ref": self.item_ref, "response": self.response}

    @staticmethod
    def from_payload(d: dict[str, Any]) -> FeedbackSignal:
        """Inverse of ``to_payload`` — exact round-trip: ``from_payload(s.to_payload()) == s``."""
        kind = d.get("kind")
        if kind != "feedback":
            raise ValueError(f"FeedbackSignal.from_payload: expected kind='feedback', got {kind!r}")
        return FeedbackSignal(
            item_ref=d["item_ref"],
            response=_validate_response(d["response"]),
        )


def _parse_response_token(token: str) -> FeedbackResponse | None:
    """Parse a single trailing token per the line grammar: y/yes -> useful, n/no ->
    not_useful (case-insensitive), anything else -> ``None`` (malformed, never raises)."""
    normalized = token.strip().lower()
    if normalized in ("y", "yes"):
        return "useful"
    if normalized in ("n", "no"):
        return "not_useful"
    return None


def _parse_feedback_line(line: str) -> tuple[str, FeedbackResponse] | None:
    """Parse one feedback-file line: ``"<item_ref> y|n"`` (also yes/no, case-insensitive).
    Returns ``None`` for a malformed line (blank, missing token, or unrecognized response) —
    the caller logs a warning and skips; this function never raises."""
    stripped = line.strip()
    if not stripped:
        return None
    parts = stripped.rsplit(None, 1)
    if len(parts) != 2:
        return None
    item_ref, token = parts
    response = _parse_response_token(token)
    if response is None:
        return None
    return item_ref, response


class FeedbackInputSource(PushSource):
    """The explicit-feedback ``InputSource`` (Q-86 ruling 2), registered under id
    ``"feedback"``.

    Push channel: inherited ``PushSource.push(SourceEvent)`` — the SAME entry a later spoken
    yes/no (ASR TK-162) will use; this class does not override it.

    File channel (v1, optional): when ``feedback_file`` is given, ``poll()`` additionally
    reads lines newly appended to it since the previous poll (offset-tracked by line count,
    so an already-read line is never re-parsed) and parses each with the deterministic
    grammar above. A malformed line is a warning + skip, never a raise (CON-3: poll() must
    never kill the source's loop). ``feedback_file=None`` (the default) makes the file
    channel a pure no-op — the channel is purely additive.
    """

    __slots__ = ("_feedback_file", "_lines_read")

    def __init__(
        self,
        poll_interval_seconds: float,
        feedback_file: str | Path | None = None,
    ) -> None:
        super().__init__(id="feedback", poll_interval_seconds=poll_interval_seconds)
        self._feedback_file = Path(feedback_file) if feedback_file is not None else None
        self._lines_read = 0

    async def poll(self) -> list[SourceEvent]:
        """Drain pushed events (``PushSource.poll()``), then append any newly-parsed events
        from the feedback file (if configured). Absence of both is fine (AC4, CON-3): returns
        ``[]``, never raises."""
        events = await super().poll()
        events.extend(self._poll_file())
        return events

    def _poll_file(self) -> list[SourceEvent]:
        """Read and parse only the lines appended to ``_feedback_file`` since the previous
        call. No file configured, or the file not (yet) existing, is a no-op (CON-3)."""
        if self._feedback_file is None or not self._feedback_file.exists():
            return []
        lines = self._feedback_file.read_text(encoding="utf-8").splitlines()
        new_lines = lines[self._lines_read :]
        self._lines_read = len(lines)

        events: list[SourceEvent] = []
        for raw_line in new_lines:
            parsed = _parse_feedback_line(raw_line)
            if parsed is None:
                _log.warning("feedback source: skipping malformed line: %r", raw_line)
                continue
            item_ref, response = parsed
            signal = FeedbackSignal(item_ref=item_ref, response=response)
            events.append(SourceEvent(event_key=signal.event_key(), payload=signal.to_payload()))
        return events


__all__ = ["FeedbackInputSource", "FeedbackResponse", "FeedbackSignal", "feedback_affordance"]
