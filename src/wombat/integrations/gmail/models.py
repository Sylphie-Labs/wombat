"""wombat.integrations.gmail.models — GmailMessageItem wire type (TK-75, EP-17, Q-65).

Mirrors ``wombat.calendar.models.CalendarEvent`` (TK-72): a frozen dataclass + JSON-native
``to_payload``/``from_payload`` wire helpers that a ``SourceEvent.payload`` round-trips through
exactly. ``GmailPoller`` (this same ticket) is the only producer.

THE BODY BOUNDARY (Q-65 ruling 3, the crux of this ticket): the raw, untrusted email body rides
under the SINGLE payload key ``body_text``. This module is one of the sanctioned producers a
build-time guard (``tests/integrations/gmail/test_body_key_guard.py``) allows to reference that
key — see that test's docstring for the full sanctioned-module list and why it differs from the
literal Q-65 briefing text.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class GmailMessageItem:
    """One Gmail inbox message, as fetched read-only via the Gmail REST v1 API.

    ``received_at`` is a timezone-AWARE instant (normalized to UTC by the producer,
    ``GmailPoller``). ``body_text`` is the raw, untrusted message body (Q-65 ruling 3) — the
    ONE field a build-time guard restricts to the sanctioned producer/consumer modules, on top
    of TK-148's runtime taint latch (two-layer defense). ``subject``/``sender`` are ordinary
    metadata, not guarded (deterministic triage over metadata needs no model, ruling 3).
    """

    message_id: str
    subject: str
    sender: str
    received_at: datetime
    body_text: str

    def __post_init__(self) -> None:
        if self.received_at.tzinfo is None:
            raise ValueError(
                f"GmailMessageItem {self.message_id!r}: received_at is naive (must be aware)"
            )

    def to_payload(self) -> dict[str, Any]:
        """JSON-native wire form (Q-49): ``received_at`` as an ISO-8601 string. The one shape
        ``SourceEvent.payload`` carries and ``from_payload`` round-trips exactly."""
        return {
            "message_id": self.message_id,
            "subject": self.subject,
            "sender": self.sender,
            "received_at": self.received_at.isoformat(),
            "body_text": self.body_text,
        }

    @staticmethod
    def from_payload(d: dict[str, Any]) -> GmailMessageItem:
        """Inverse of ``to_payload`` — exact round-trip: ``from_payload(i.to_payload()) == i``."""
        return GmailMessageItem(
            message_id=d["message_id"],
            subject=d["subject"],
            sender=d["sender"],
            received_at=datetime.fromisoformat(d["received_at"]),
            body_text=d["body_text"],
        )


__all__ = ["GmailMessageItem"]
