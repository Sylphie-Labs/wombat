"""brief_template — the terse rendering of a sealed BriefDecisionArtifact (TK-100, Q-77).

ONE pure function, ``render_brief_lines``, is the SINGLE SOURCE OF TRUTH for the brief's
user-facing text: ``BriefComposeStage`` uses its output BOTH as the model's user message AND as
the S8 fallback body, so the model path and the degrade path can never drift apart (mirrors
``wombat.compose.templates.format_payload_fields``'s "one source of user-facing content"
posture, TK-8).

Reads ONLY the sealed ``BriefDecisionArtifact`` (TK-99) — never raw source data, never gate
scoring. Q-50 BOUNDARY: this module never renders ``urgency_score``/``priority_band``/
``matched_rules``/``event_class`` or any raw ``event_id`` — only human-facing content (titles,
local times, subjects, senders).

SECTIONS, IN ORDER: honest degrade lines (``calendar_unavailable``/``gmail_unavailable``) ->
Conflicts -> Prep -> Recap; a fixed quiet line replaces the three item sections when all three
buckets are empty.

Q-76 CONFLICT RENDERING (option b, enrich-from-sealed-prep): a conflict entry is cross-referenced
against the sealed ``prep`` bucket by ``incumbent_event_id``/``movable_event_id``. If BOTH
conflicting events are present in ``prep``, each is rendered with its OWN sealed start-end times
(no overlap window is computed, no TK-74 kernel re-derivation). If EITHER is missing, an honest
day-level line names the two titles and the conflict's ISO ``day`` instead of inventing a time.
"""

from __future__ import annotations

from typing import Any
from zoneinfo import ZoneInfo

from wombat.calendar.models import CalendarEvent
from wombat.domain.brief_decision_artifact import BriefDecisionArtifact
from wombat.domain.brief_payload import GmailBriefItem

# A fixed, terse steward instruction (mirrors compose.py's _SYSTEM_INSTRUCTION posture) — no
# prompt iteration (mvp).
BRIEF_SYSTEM_INSTRUCTION = (
    "You are a quiet steward delivering this morning's brief. The lines below are the "
    "already-decided brief contents. Phrase them for the user in a few terse, calm lines — "
    "do not add, omit, or invent anything beyond what is given. No preamble."
)

_QUIET_LINE = "Nothing else on the brief this morning."
_CALENDAR_UNAVAILABLE_LINE = "Calendar is unavailable right now."
_GMAIL_UNAVAILABLE_LINE = "Gmail is unavailable right now."


def _render_prep_line(event: CalendarEvent, tz: ZoneInfo) -> str:
    """One prep event's title + its OWN local start-end times (never a computed window)."""
    start_local = event.start.astimezone(tz)
    end_local = event.end.astimezone(tz)
    return f"{event.title} {start_local.strftime('%H:%M')}-{end_local.strftime('%H:%M')}"


def _render_recap_line(item: GmailBriefItem) -> str:
    return f"{item.subject} from {item.sender}"


def _render_conflict_line(
    entry: dict[str, Any], prep_by_id: dict[str, CalendarEvent], tz: ZoneInfo
) -> str:
    """Q-76: both-present -> each event's own sealed times; either-missing -> a day-level line."""
    incumbent = prep_by_id.get(entry["incumbent_event_id"])
    movable = prep_by_id.get(entry["movable_event_id"])
    if incumbent is not None and movable is not None:
        return f"{_render_prep_line(incumbent, tz)} conflicts with {_render_prep_line(movable, tz)}"
    return f"{entry['incumbent_title']} conflicts with {entry['movable_title']} on {entry['day']}"


def render_brief_lines(artifact: BriefDecisionArtifact, *, tz: ZoneInfo) -> str:
    """Render the sealed ``BriefDecisionArtifact`` as terse, human-facing lines (pure).

    Same input -> same output, every time; no model call, no I/O, no clock. This is the ONE
    string both the model prompt and the S8 template fallback are built from.
    """
    lines: list[str] = []

    if artifact.calendar_unavailable:
        lines.append(_CALENDAR_UNAVAILABLE_LINE)
    if artifact.gmail_unavailable:
        lines.append(_GMAIL_UNAVAILABLE_LINE)

    bucket = artifact.bucket
    prep_by_id = {event.event_id: event for event in bucket.prep}

    if bucket.conflict:
        lines.append("Conflicts:")
        for entry in bucket.conflict:
            lines.append(f"- {_render_conflict_line(entry, prep_by_id, tz)}")

    if bucket.prep:
        lines.append("Prep:")
        for event in bucket.prep:
            lines.append(f"- {_render_prep_line(event, tz)}")

    if bucket.recap:
        lines.append("Recap:")
        for item in bucket.recap:
            lines.append(f"- {_render_recap_line(item)}")

    if not bucket.conflict and not bucket.prep and not bucket.recap:
        lines.append(_QUIET_LINE)

    return "\n".join(lines)


__all__ = ["BRIEF_SYSTEM_INSTRUCTION", "render_brief_lines"]
