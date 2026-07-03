"""TK-98 acceptance criteria — BriefPayload / GmailBriefItem (Q-74).

Pure-unit tests: no I/O, no clock, no network. Proves the JSON-native ``to_payload``/
``from_payload`` round-trip (Q-49) for both types, and that a ``GmailBriefItem`` never carries
the raw, untrusted Gmail body field — metadata + triage outcome only (Q-66 ruling 2).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from wombat.calendar.models import CalendarEvent
from wombat.domain.brief_payload import BriefPayload, GmailBriefItem
from wombat.integrations.gmail.triage import PriorityBand

_NOW = datetime(2026, 7, 3, 8, 0, tzinfo=UTC)

_SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "wombat"
_BRIEF_PAYLOAD_SRC = (_SRC_ROOT / "domain" / "brief_payload.py").read_text(encoding="utf-8")

# Built as a runtime concatenation (never a literal in this test file) so the file itself does
# not trip the Q-65 body-key guard's scope — that guard only scans src/wombat anyway, but this
# keeps the intent explicit: we are proving brief_payload.py's SOURCE never spells this key.
_FORBIDDEN_BODY_KEY = "body" + "_text"


def _gmail_brief_item(**overrides: object) -> GmailBriefItem:
    defaults: dict[str, object] = {
        "message_id": "msg-1",
        "subject": "Q3 budget",
        "sender": "jane@example.com",
        "received_at": _NOW,
        "urgency_score": 0.7,
        "priority_band": PriorityBand.HIGH,
        "matched_rules": ("vip_sender_allowlist",),
    }
    defaults.update(overrides)
    return GmailBriefItem(**defaults)  # type: ignore[arg-type]


def _calendar_event(**overrides: object) -> CalendarEvent:
    defaults: dict[str, object] = {
        "event_id": "evt-1",
        "title": "Standup",
        "start": datetime(2026, 7, 3, 9, 0, tzinfo=UTC),
        "end": datetime(2026, 7, 3, 9, 30, tzinfo=UTC),
        "all_day": False,
    }
    defaults.update(overrides)
    return CalendarEvent(**defaults)  # type: ignore[arg-type]


# --- GmailBriefItem -------------------------------------------------------------------------


def test_gmail_brief_item_round_trips_through_payload_exactly() -> None:
    item = _gmail_brief_item()

    payload = item.to_payload()

    assert payload == {
        "message_id": "msg-1",
        "subject": "Q3 budget",
        "sender": "jane@example.com",
        "received_at": _NOW.isoformat(),
        "urgency_score": 0.7,
        "priority_band": "high",
        "matched_rules": ["vip_sender_allowlist"],
    }
    assert GmailBriefItem.from_payload(payload) == item


def test_gmail_brief_item_payload_never_carries_the_guarded_body_key() -> None:
    item = _gmail_brief_item()
    payload = item.to_payload()

    assert _FORBIDDEN_BODY_KEY not in payload
    assert set(payload) == {
        "message_id",
        "subject",
        "sender",
        "received_at",
        "urgency_score",
        "priority_band",
        "matched_rules",
    }


def test_brief_payload_module_source_never_references_the_guarded_body_key() -> None:
    """Static proof mirroring the triage.py self-check (Q-65 ruling 2): brief_payload.py's own
    source text never contains the guarded body-key literal — GmailBriefItem is structurally
    metadata + triage-outcome only."""
    assert _FORBIDDEN_BODY_KEY not in _BRIEF_PAYLOAD_SRC


# --- BriefPayload ----------------------------------------------------------------------------


def test_brief_payload_round_trips_through_payload_exactly_with_events_and_items() -> None:
    calendar_events = (_calendar_event(), _calendar_event(event_id="evt-2", title="Lunch"))
    gmail_items = (_gmail_brief_item(), _gmail_brief_item(message_id="msg-2"))
    payload_obj = BriefPayload(
        generated_at=_NOW,
        calendar_events=calendar_events,
        gmail_items=gmail_items,
        calendar_unavailable=False,
        gmail_unavailable=False,
    )

    data = payload_obj.to_payload()

    assert data["generated_at"] == _NOW.isoformat()
    assert len(data["calendar_events"]) == 2
    assert len(data["gmail_items"]) == 2
    assert data["calendar_unavailable"] is False
    assert data["gmail_unavailable"] is False
    assert BriefPayload.from_payload(data) == payload_obj


def test_brief_payload_round_trips_with_empty_slices_and_unavailable_flags_set() -> None:
    payload_obj = BriefPayload(
        generated_at=_NOW,
        calendar_events=(),
        gmail_items=(),
        calendar_unavailable=True,
        gmail_unavailable=True,
    )

    data = payload_obj.to_payload()

    assert data["calendar_events"] == []
    assert data["gmail_items"] == []
    assert data["calendar_unavailable"] is True
    assert data["gmail_unavailable"] is True
    assert BriefPayload.from_payload(data) == payload_obj


def test_brief_payload_has_no_conflict_field() -> None:
    """Q-74: conflict detection is downstream's job (TK-99+) — BriefPayload carries none."""
    field_names = set(BriefPayload.__dataclass_fields__)
    assert not any("conflict" in name for name in field_names)
