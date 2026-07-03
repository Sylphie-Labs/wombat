"""wombat.domain.brief_payload — BriefPayload wire type (TK-98, Q-74).

Mirrors ``wombat.calendar.models.CalendarEvent`` (TK-72): frozen dataclasses + JSON-native
``to_payload``/``from_payload`` wire helpers an Artifact's ``data`` round-trips through exactly
(Q-49). ``BriefGatherStage`` (this same ticket) is the only producer.

``GmailBriefItem`` is built from ``GmailMessageItem`` METADATA ONLY (``message_id``, ``subject``,
``sender``, ``received_at``) plus a ``wombat.integrations.gmail.triage.triage_message`` result
(``urgency_score``, ``priority_band``, ``matched_rules``). It must NEVER carry the raw, untrusted
message body field — brief files are not on the Q-65 body-key guard's allowlist
(``tests/integrations/gmail/test_body_key_guard.py``), so this is a structural requirement, not
just a convention: this module never references that guarded field name.

``BriefPayload`` packs one calendar slice + one Gmail slice with independent per-source
availability flags (``calendar_unavailable`` / ``gmail_unavailable``) — set by ``BriefGatherStage``
when that source's read fails. There is deliberately NO conflict field: conflict detection between
the two slices is downstream's job (TK-99+), not this ticket's.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from wombat.calendar.models import CalendarEvent
from wombat.integrations.gmail.triage import PriorityBand


@dataclass(frozen=True, slots=True)
class GmailBriefItem:
    """One Gmail message's brief-ready metadata + triage outcome (Q-74).

    Built ONLY from ``GmailMessageItem``'s metadata fields and a ``triage_message`` result —
    NEVER from the raw message body. ``received_at`` is a timezone-AWARE instant, matching
    ``GmailMessageItem``.
    """

    message_id: str
    subject: str
    sender: str
    received_at: datetime
    urgency_score: float
    priority_band: PriorityBand
    matched_rules: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.received_at.tzinfo is None:
            raise ValueError(
                f"GmailBriefItem {self.message_id!r}: received_at is naive (must be aware)"
            )

    def to_payload(self) -> dict[str, Any]:
        """JSON-native wire form (Q-49): ``received_at`` as an ISO-8601 string, ``priority_band``
        as its string value. The one shape ``from_payload`` round-trips exactly."""
        return {
            "message_id": self.message_id,
            "subject": self.subject,
            "sender": self.sender,
            "received_at": self.received_at.isoformat(),
            "urgency_score": self.urgency_score,
            "priority_band": self.priority_band.value,
            "matched_rules": list(self.matched_rules),
        }

    @staticmethod
    def from_payload(d: dict[str, Any]) -> GmailBriefItem:
        """Inverse of ``to_payload`` — exact round-trip: ``from_payload(i.to_payload()) == i``."""
        return GmailBriefItem(
            message_id=d["message_id"],
            subject=d["subject"],
            sender=d["sender"],
            received_at=datetime.fromisoformat(d["received_at"]),
            urgency_score=d["urgency_score"],
            priority_band=PriorityBand(d["priority_band"]),
            matched_rules=tuple(d["matched_rules"]),
        )


@dataclass(frozen=True, slots=True)
class BriefPayload:
    """The structured payload ``BriefGatherStage`` hands downstream (Q-74).

    ``calendar_events`` / ``gmail_items`` are stored verbatim (no dedup/mutation) — whatever the
    injected fetch callables returned. ``calendar_unavailable`` / ``gmail_unavailable`` are
    independent per-source degrade flags: a failed source's slice is an empty tuple and its flag
    is ``True``; the other source's slice/flag is unaffected. No conflict field (downstream's
    job, TK-99+).
    """

    generated_at: datetime
    calendar_events: tuple[CalendarEvent, ...]
    gmail_items: tuple[GmailBriefItem, ...]
    calendar_unavailable: bool
    gmail_unavailable: bool

    def __post_init__(self) -> None:
        if self.generated_at.tzinfo is None:
            raise ValueError("BriefPayload: generated_at is naive (must be aware)")

    def to_payload(self) -> dict[str, Any]:
        """JSON-native wire form (Q-49): ``generated_at`` as an ISO-8601 string, both event
        tuples as lists of their own ``to_payload()``. The one shape ``from_payload`` round-trips
        exactly."""
        return {
            "generated_at": self.generated_at.isoformat(),
            "calendar_events": [event.to_payload() for event in self.calendar_events],
            "gmail_items": [item.to_payload() for item in self.gmail_items],
            "calendar_unavailable": self.calendar_unavailable,
            "gmail_unavailable": self.gmail_unavailable,
        }

    @staticmethod
    def from_payload(d: dict[str, Any]) -> BriefPayload:
        """Inverse of ``to_payload`` — exact round-trip: ``from_payload(p.to_payload()) == p``."""
        return BriefPayload(
            generated_at=datetime.fromisoformat(d["generated_at"]),
            calendar_events=tuple(
                CalendarEvent.from_payload(raw) for raw in d["calendar_events"]
            ),
            gmail_items=tuple(GmailBriefItem.from_payload(raw) for raw in d["gmail_items"]),
            calendar_unavailable=d["calendar_unavailable"],
            gmail_unavailable=d["gmail_unavailable"],
        )


__all__ = ["BriefPayload", "GmailBriefItem"]
