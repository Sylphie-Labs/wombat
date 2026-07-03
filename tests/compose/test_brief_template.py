"""TK-100 acceptance criteria — render_brief_lines (Q-77).

Pure function tests: no model, no I/O, no Postgres. Covers the fixed section ordering, the
all-empty quiet line, the degrade lines, the Q-76 conflict-rendering fork (both-present ->
precise sealed times; either-missing -> an honest day-level line), and the Q-50 boundary (no
scoring key or raw id ever leaks into the rendered text).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from wombat.calendar.models import CalendarEvent
from wombat.compose.brief_template import render_brief_lines
from wombat.domain.brief_decision_artifact import BriefBucket, BriefDecisionArtifact
from wombat.domain.brief_payload import GmailBriefItem
from wombat.integrations.gmail.triage import PriorityBand
from wombat.rating.params import EventClass

# EDT (UTC-4) in July -- proves .astimezone(tz) is actually applied, not just passed through UTC.
_TZ = ZoneInfo("America/New_York")
_NOW = datetime(2026, 7, 3, 8, 0, tzinfo=UTC)


def _event(event_id: str, title: str, start_hour_utc: int, end_hour_utc: int) -> CalendarEvent:
    start = datetime(2026, 7, 3, start_hour_utc, 0, tzinfo=UTC)
    end = datetime(2026, 7, 3, end_hour_utc, 0, tzinfo=UTC)
    return CalendarEvent(event_id=event_id, title=title, start=start, end=end, all_day=False)


def _gmail(message_id: str, subject: str, sender: str) -> GmailBriefItem:
    return GmailBriefItem(
        message_id=message_id,
        subject=subject,
        sender=sender,
        received_at=_NOW,
        urgency_score=0.9,
        priority_band=PriorityBand.HIGH,
        matched_rules=("vip_sender",),
    )


def _conflict(
    *,
    incumbent_id: str,
    incumbent_title: str,
    movable_id: str,
    movable_title: str,
    day: str = "2026-07-03",
) -> dict[str, Any]:
    return {
        "event_class": EventClass.CALENDAR_CONFLICT.value,
        "day": day,
        "incumbent_event_id": incumbent_id,
        "incumbent_title": incumbent_title,
        "movable_event_id": movable_id,
        "movable_title": movable_title,
    }


def _artifact(
    *,
    recap: tuple[GmailBriefItem, ...] = (),
    conflict: tuple[dict[str, Any], ...] = (),
    prep: tuple[CalendarEvent, ...] = (),
    calendar_unavailable: bool = False,
    gmail_unavailable: bool = False,
) -> BriefDecisionArtifact:
    return BriefDecisionArtifact(
        bucket=BriefBucket(recap=recap, conflict=conflict, prep=prep),
        calendar_unavailable=calendar_unavailable,
        gmail_unavailable=gmail_unavailable,
    )


# ------------------------------------------------------------------------------ section ordering


def test_three_section_ordering_is_conflicts_then_prep_then_recap() -> None:
    incumbent = _event("evt-1", "Standup", 13, 14)
    movable = _event("evt-2", "1:1 with Sam", 13, 15)
    artifact = _artifact(
        recap=(_gmail("m-1", "Renewal notice", "billing@acme.com"),),
        conflict=(
            _conflict(
                incumbent_id="evt-1",
                incumbent_title="Standup",
                movable_id="evt-2",
                movable_title="1:1 with Sam",
            ),
        ),
        prep=(incumbent, movable),
    )

    rendered = render_brief_lines(artifact, tz=_TZ)

    conflicts_idx = rendered.index("Conflicts:")
    prep_idx = rendered.index("Prep:")
    recap_idx = rendered.index("Recap:")
    assert conflicts_idx < prep_idx < recap_idx


# ------------------------------------------------------------------------------------ empty case


def test_all_buckets_empty_renders_the_fixed_quiet_line() -> None:
    artifact = _artifact()

    rendered = render_brief_lines(artifact, tz=_TZ)

    assert rendered == "Nothing else on the brief this morning."


# --------------------------------------------------------------------------------- degrade lines


def test_calendar_and_gmail_unavailable_lines_render_first() -> None:
    artifact = _artifact(calendar_unavailable=True, gmail_unavailable=True)

    rendered = render_brief_lines(artifact, tz=_TZ)
    lines = rendered.splitlines()

    assert lines[0] == "Calendar is unavailable right now."
    assert lines[1] == "Gmail is unavailable right now."
    # bucket is still empty -> the quiet line still appears, after the degrade lines.
    assert lines[2] == "Nothing else on the brief this morning."


def test_only_gmail_unavailable_renders_only_that_line() -> None:
    artifact = _artifact(gmail_unavailable=True)

    rendered = render_brief_lines(artifact, tz=_TZ)

    assert "Calendar is unavailable" not in rendered
    assert "Gmail is unavailable right now." in rendered


# ---------------------------------------------------------------------------- Q-76 both-present


def test_q76_both_conflicting_events_present_in_prep_renders_precise_local_times() -> None:
    incumbent = _event("evt-1", "Standup", 13, 14)  # 13:00-14:00 UTC -> 09:00-10:00 EDT
    movable = _event("evt-2", "1:1 with Sam", 13, 15)  # -> 09:00-11:00 EDT
    artifact = _artifact(
        conflict=(
            _conflict(
                incumbent_id="evt-1",
                incumbent_title="Standup",
                movable_id="evt-2",
                movable_title="1:1 with Sam",
            ),
        ),
        prep=(incumbent, movable),
    )

    rendered = render_brief_lines(artifact, tz=_TZ)

    assert "Standup 09:00-10:00 conflicts with 1:1 with Sam 09:00-11:00" in rendered
    # No re-derived overlap window and no ISO day fallback text in this branch.
    assert "2026-07-03" not in rendered


# ----------------------------------------------------------------------------- Q-76 one-missing


def test_q76_one_conflicting_event_missing_from_prep_renders_day_level_line() -> None:
    incumbent = _event("evt-1", "Standup", 13, 14)
    # "evt-2" (movable) is NOT selected into the sealed prep bucket.
    artifact = _artifact(
        conflict=(
            _conflict(
                incumbent_id="evt-1",
                incumbent_title="Standup",
                movable_id="evt-2",
                movable_title="1:1 with Sam",
                day="2026-07-03",
            ),
        ),
        prep=(incumbent,),
    )

    rendered = render_brief_lines(artifact, tz=_TZ)

    # The exact rendering: the CONFLICT line is the honest day-level fallback (no invented time
    # for either side of it); the incumbent's OWN sealed time only appears in the separate Prep
    # section, since it independently made the sealed prep bucket.
    assert rendered == (
        "Conflicts:\n"
        "- Standup conflicts with 1:1 with Sam on 2026-07-03\n"
        "Prep:\n"
        "- Standup 09:00-10:00"
    )


def test_q76_both_conflicting_events_missing_from_prep_also_renders_day_level_line() -> None:
    artifact = _artifact(
        conflict=(
            _conflict(
                incumbent_id="evt-1",
                incumbent_title="Standup",
                movable_id="evt-2",
                movable_title="1:1 with Sam",
                day="2026-07-03",
            ),
        ),
        prep=(),
    )

    rendered = render_brief_lines(artifact, tz=_TZ)

    assert "Standup conflicts with 1:1 with Sam on 2026-07-03" in rendered


# --------------------------------------------------------------------------------------- Q-50


def test_q50_no_scoring_key_or_raw_id_ever_leaks_into_rendered_text() -> None:
    incumbent = _event("evt-raw-id-1", "Standup", 13, 14)
    movable = _event("evt-raw-id-2", "1:1 with Sam", 13, 15)
    artifact = _artifact(
        recap=(_gmail("msg-raw-id-3", "Renewal notice", "billing@acme.com"),),
        conflict=(
            _conflict(
                incumbent_id="evt-raw-id-1",
                incumbent_title="Standup",
                movable_id="evt-raw-id-2",
                movable_title="1:1 with Sam",
            ),
        ),
        prep=(incumbent, movable),
    )

    rendered = render_brief_lines(artifact, tz=_TZ)

    for banned in (
        "urgency_score",
        "priority_band",
        "matched_rules",
        "event_class",
        "0.9",
        "vip_sender",
        "evt-raw-id-1",
        "evt-raw-id-2",
        "msg-raw-id-3",
    ):
        assert banned not in rendered
