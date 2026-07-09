"""TK-98 acceptance criteria — BriefGatherStage (Q-74).

PURE stage tests (no Postgres, no network): inject bare zero-arg ``fetch_calendar``/
``fetch_gmail`` callables + a real (in-memory-built) ``TriageRules`` + the reusable
``StageContextFake`` (``tests/support/stage_context_fake.py``) and drive ``run(ctx)`` directly.

  AC1 (both sources present -> N calendar events + M gmail items, verbatim, no dupes/mutation,
      both unavailable flags False, Transition to brief_force_flush with kind=BRIEF_PAYLOAD):
      ``test_ac1_...``.
  AC2a (calendar fetch raises -> calendar empty + calendar_unavailable=True, gmail still
      collected, no raise): ``test_ac2a_...``.
  AC2b (gmail fetch raises -> gmail empty + gmail_unavailable=True, calendar still collected,
      no raise): ``test_ac2b_...``.
  AC2c (both raise -> both empty + both flags True, no raise): ``test_ac2c_...``.
  AC3 (the stage returns its Artifact and never touches ctx.journal): ``test_ac3_...``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import NoReturn

import pytest
from cogworx.loop.result import Transition

from tests.support.stage_context_fake import StageContextFake
from wombat.calendar.models import CalendarEvent
from wombat.integrations.gmail.models import GmailMessageItem
from wombat.integrations.gmail.triage import (
    PriorityBand,
    SenderAllowlistRule,
    SubjectKeywordRule,
    TriageRules,
)
from wombat.stages.artifacts import BRIEF_PAYLOAD
from wombat.stages.brief_gather_stage import BriefGatherStage

_FIXED_NOW = datetime(2026, 7, 3, 7, 0, tzinfo=UTC)

_RULES = TriageRules(
    version=1,
    sender_allowlist_rules=(
        SenderAllowlistRule(
            name="vip_sender_allowlist",
            senders=("boss@example.com",),
            urgency_score=0.9,
            priority_band=PriorityBand.HIGH,
        ),
    ),
    subject_keyword_rules=(
        SubjectKeywordRule(
            name="urgent_subject_keyword",
            keywords=("urgent",),
            urgency_score=0.8,
            priority_band=PriorityBand.HIGH,
        ),
    ),
)


def _calendar_event(event_id: str = "evt-1") -> CalendarEvent:
    return CalendarEvent(
        event_id=event_id,
        title="Standup",
        start=datetime(2026, 7, 3, 9, 0, tzinfo=UTC),
        end=datetime(2026, 7, 3, 9, 30, tzinfo=UTC),
        all_day=False,
    )


def _gmail_message(
    message_id: str = "msg-1", *, sender: str = "boss@example.com"
) -> GmailMessageItem:
    return GmailMessageItem(
        message_id=message_id,
        subject="fyi",
        sender=sender,
        received_at=_FIXED_NOW,
        body_text="irrelevant body content",
    )


@dataclass
class _JournalSpyStageContext(StageContextFake):
    """A ``StageContextFake`` that turns any ``ctx.journal`` access into a loud, specific
    failure (rather than the base fake's generic ``NotImplementedError``) so AC3's "never
    touches ctx.journal" claim is proven by an explicit spy, not just incidental behavior."""

    journal_accessed: bool = False

    @property
    def journal(self) -> NoReturn:
        self.journal_accessed = True
        raise AssertionError(
            "BriefGatherStage touched ctx.journal — stages never journal directly, the "
            "engine journals the returned Artifact"
        )


def _make_ctx() -> _JournalSpyStageContext:
    return _JournalSpyStageContext(now_fn=lambda: _FIXED_NOW)


def _make_stage(
    *,
    fetch_calendar: Callable[[], list[CalendarEvent]] | None = None,
    fetch_gmail: Callable[[], list[GmailMessageItem]] | None = None,
) -> BriefGatherStage:
    def _default_calendar() -> list[CalendarEvent]:
        return [_calendar_event()]

    def _default_gmail() -> list[GmailMessageItem]:
        return [_gmail_message()]

    return BriefGatherStage(
        fetch_calendar=fetch_calendar or _default_calendar,
        fetch_gmail=fetch_gmail or _default_gmail,
        triage_rules=_RULES,
        clock=lambda: _FIXED_NOW,
    )


# ------------------------------------------------------------------------------------------ AC1


async def test_ac1_both_sources_present_yields_verbatim_payload_and_transition() -> None:
    events = [_calendar_event("evt-1"), _calendar_event("evt-2")]
    messages = [_gmail_message("msg-1"), _gmail_message("msg-2"), _gmail_message("msg-3")]
    stage = _make_stage(fetch_calendar=lambda: events, fetch_gmail=lambda: messages)
    ctx = _make_ctx()

    result = await stage.run(ctx)

    assert isinstance(result, Transition)
    assert result.to == "brief_force_flush"
    assert result.output.kind == BRIEF_PAYLOAD
    assert result.output.produced_by == "brief_gather"

    data = result.output.data
    assert len(data["calendar_events"]) == 2
    assert [e["event_id"] for e in data["calendar_events"]] == ["evt-1", "evt-2"]
    assert len(data["gmail_items"]) == 3
    assert [i["message_id"] for i in data["gmail_items"]] == ["msg-1", "msg-2", "msg-3"]
    assert data["calendar_unavailable"] is False
    assert data["gmail_unavailable"] is False

    # Triage actually ran (boss@example.com matches the vip sender rule) — proves this is a
    # real triage outcome, not a stub/default value.
    assert data["gmail_items"][0]["priority_band"] == "high"
    assert "vip_sender_allowlist" in data["gmail_items"][0]["matched_rules"]

    # No dupes, no mutation of the raw source data: the calendar events round-trip identical
    # to the objects the fetch callable returned.
    assert [CalendarEvent.from_payload(e) for e in data["calendar_events"]] == events


# ------------------------------------------------------------------------------------------ AC2


async def test_ac2a_calendar_fetch_raises_degrades_calendar_only() -> None:
    def _raising_calendar() -> list[CalendarEvent]:
        raise RuntimeError("calendar source down")

    stage = _make_stage(fetch_calendar=_raising_calendar)
    ctx = _make_ctx()

    result = await stage.run(ctx)  # MUST NOT raise

    assert isinstance(result, Transition)
    data = result.output.data
    assert data["calendar_events"] == []
    assert data["calendar_unavailable"] is True
    assert len(data["gmail_items"]) == 1  # gmail still collected
    assert data["gmail_unavailable"] is False


async def test_ac2b_gmail_fetch_raises_degrades_gmail_only() -> None:
    def _raising_gmail() -> list[GmailMessageItem]:
        raise RuntimeError("gmail source down")

    stage = _make_stage(fetch_gmail=_raising_gmail)
    ctx = _make_ctx()

    result = await stage.run(ctx)  # MUST NOT raise

    assert isinstance(result, Transition)
    data = result.output.data
    assert data["gmail_items"] == []
    assert data["gmail_unavailable"] is True
    assert len(data["calendar_events"]) == 1  # calendar still collected
    assert data["calendar_unavailable"] is False


async def test_ac2c_both_sources_raise_degrades_both() -> None:
    def _raising_calendar() -> list[CalendarEvent]:
        raise RuntimeError("calendar source down")

    def _raising_gmail() -> list[GmailMessageItem]:
        raise RuntimeError("gmail source down")

    stage = _make_stage(fetch_calendar=_raising_calendar, fetch_gmail=_raising_gmail)
    ctx = _make_ctx()

    result = await stage.run(ctx)  # MUST NOT raise

    assert isinstance(result, Transition)
    data = result.output.data
    assert data["calendar_events"] == []
    assert data["calendar_unavailable"] is True
    assert data["gmail_items"] == []
    assert data["gmail_unavailable"] is True


async def test_ac2_gmail_triage_failure_also_degrades_gmail_cleanly() -> None:
    """Triage runs INSIDE the gmail guarded block (Q-74) — a triage-time failure must degrade
    gmail exactly like a fetch-time failure, without touching the calendar slice."""

    def _malformed_gmail() -> list[GmailMessageItem]:
        # A message whose sender is not a string blows up triage_message's .lower() call —
        # proving the guard covers triage, not just the fetch call itself.
        return [object()]  # type: ignore[list-item]

    stage = _make_stage(fetch_gmail=_malformed_gmail)
    ctx = _make_ctx()

    result = await stage.run(ctx)  # MUST NOT raise

    assert isinstance(result, Transition)
    data = result.output.data
    assert data["gmail_items"] == []
    assert data["gmail_unavailable"] is True
    assert len(data["calendar_events"]) == 1


# ------------------------------------------------------------------------------------- TK-170


async def test_calendar_fetch_returning_empty_list_renders_as_empty_not_unavailable() -> None:
    """A tolerant ``fetch_calendar`` (e.g. ``CalendarPoller.fetch_window`` on TK-170's missing-
    ``items``-key window) returns ``[]`` WITHOUT raising — this must produce an empty calendar
    slice with ``calendar_unavailable=False``, distinct from the raising-degrade path (AC2a)
    which sets ``calendar_unavailable=True``. This is the render-time distinction between "no
    events today" and "Calendar is unavailable right now" (``compose/brief_template.py``)."""

    def _empty_calendar() -> list[CalendarEvent]:
        return []

    stage = _make_stage(fetch_calendar=_empty_calendar)
    ctx = _make_ctx()

    result = await stage.run(ctx)  # MUST NOT raise

    assert isinstance(result, Transition)
    data = result.output.data
    assert data["calendar_events"] == []
    assert data["calendar_unavailable"] is False  # empty, NOT unavailable
    assert len(data["gmail_items"]) == 1  # gmail still collected


# ------------------------------------------------------------------------------------------ AC3


async def test_ac3_stage_never_touches_ctx_journal() -> None:
    stage = _make_stage()
    ctx = _make_ctx()

    result = await stage.run(ctx)

    assert isinstance(result, Transition)
    assert ctx.journal_accessed is False


def test_ac3_ctx_journal_spy_is_load_bearing() -> None:
    """Sanity: the spy actually raises when journal IS touched, so AC3's negative assertion
    above is proven by a live trap, not vacuously."""
    ctx = _make_ctx()
    with pytest.raises(AssertionError):
        _ = ctx.journal
    assert ctx.journal_accessed is True
