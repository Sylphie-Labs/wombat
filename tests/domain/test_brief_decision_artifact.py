"""TK-99 acceptance criteria — BriefBucket / BriefDecisionArtifact (Q-75).

Pure-unit tests: no I/O, no clock, no network. Proves the JSON-native ``to_payload``/
``from_payload`` round-trip (Q-49) for both types, that ``item_kind`` is always stamped
``ItemKind.BRIEF.value`` ("brief"), that gate SCORING keys (urgency/load) never cross into the
wire form (Q-50 mouth-facing boundary), and that the sealed artifact is immutable (frozen).
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest

from wombat.calendar.conflict import DailyConflict, conflict_to_payload
from wombat.calendar.models import CalendarEvent, CalendarEventItem, Conflict
from wombat.domain.brief_decision_artifact import BriefBucket, BriefDecisionArtifact
from wombat.domain.brief_payload import GmailBriefItem
from wombat.gate.models import ItemKind
from wombat.integrations.gmail.triage import PriorityBand

_NOW = datetime(2026, 7, 3, 8, 0, tzinfo=UTC)


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
        "start": _NOW + timedelta(hours=1),
        "end": _NOW + timedelta(hours=1, minutes=30),
        "all_day": False,
    }
    defaults.update(overrides)
    return CalendarEvent(**defaults)  # type: ignore[arg-type]


def _daily_conflict() -> DailyConflict:
    return DailyConflict(
        day=_NOW.date(),
        conflict=Conflict(
            incumbent=CalendarEventItem(event_id="evt-a", title="Standup", start=540, end=600),
            movable=CalendarEventItem(event_id="evt-b", title="1:1", start=570, end=630),
        ),
    )


def _bucket() -> BriefBucket:
    return BriefBucket(
        recap=(_gmail_brief_item(), _gmail_brief_item(message_id="msg-2")),
        conflict=(conflict_to_payload(_daily_conflict()),),
        prep=(_calendar_event(), _calendar_event(event_id="evt-2", title="Lunch")),
    )


# --- BriefBucket ------------------------------------------------------------------------------


def test_brief_bucket_round_trips_through_payload_exactly() -> None:
    bucket = _bucket()

    data = bucket.to_payload()

    assert len(data["recap"]) == 2
    assert len(data["conflict"]) == 1
    assert len(data["prep"]) == 2
    assert BriefBucket.from_payload(data) == bucket


def test_brief_bucket_round_trips_when_empty() -> None:
    bucket = BriefBucket()

    data = bucket.to_payload()

    assert data == {"recap": [], "conflict": [], "prep": []}
    assert BriefBucket.from_payload(data) == bucket


def test_brief_bucket_conflict_entry_carries_the_one_stamping_helpers_shape() -> None:
    bucket = _bucket()
    data = bucket.to_payload()

    assert data["conflict"][0] == conflict_to_payload(_daily_conflict())
    assert data["conflict"][0]["event_class"] == "calendar_conflict"


# --- BriefDecisionArtifact ----------------------------------------------------------------------


def test_brief_decision_artifact_round_trips_through_payload_exactly() -> None:
    artifact = BriefDecisionArtifact(
        bucket=_bucket(), calendar_unavailable=False, gmail_unavailable=True
    )

    data = artifact.to_payload()

    assert BriefDecisionArtifact.from_payload(data) == artifact


def test_brief_decision_artifact_stamps_item_kind_brief() -> None:
    artifact = BriefDecisionArtifact(
        bucket=BriefBucket(), calendar_unavailable=False, gmail_unavailable=False
    )

    assert artifact.item_kind == "brief"
    assert artifact.item_kind == ItemKind.BRIEF.value
    assert artifact.to_payload()["item_kind"] == "brief"


def test_brief_decision_artifact_wire_form_carries_no_gate_scoring_keys() -> None:
    """Q-50: the gate's ScoredItem.urgency/load NEVER cross into the sealed wire form — only
    each family's own canonical to_payload()/conflict_to_payload() shape does."""
    artifact = BriefDecisionArtifact(
        bucket=_bucket(), calendar_unavailable=False, gmail_unavailable=False
    )

    def _assert_no_scoring_keys(node: object) -> None:
        if isinstance(node, dict):
            assert "urgency" not in node
            assert "load" not in node
            assert "score" not in node
            for value in node.values():
                _assert_no_scoring_keys(value)
        elif isinstance(node, list):
            for value in node:
                _assert_no_scoring_keys(value)

    _assert_no_scoring_keys(artifact.to_payload())


def test_brief_decision_artifact_is_frozen_and_immutable() -> None:
    artifact = BriefDecisionArtifact(
        bucket=_bucket(), calendar_unavailable=False, gmail_unavailable=False
    )

    with pytest.raises(dataclasses.FrozenInstanceError):
        artifact.calendar_unavailable = True  # type: ignore[misc]


def test_brief_bucket_is_frozen_and_immutable() -> None:
    bucket = _bucket()

    with pytest.raises(dataclasses.FrozenInstanceError):
        bucket.recap = ()  # type: ignore[misc]
