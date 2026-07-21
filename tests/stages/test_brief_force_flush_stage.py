"""TK-99 acceptance criteria — BriefForceFlushStage (Q-75).

AC1 (audit-CRITICAL): drives ``run()`` against a REAL ``Gate.select_items`` over a durable
``PendingSet`` seeded with 2 unrelated live items, and proves the live pending set/journal/ceiling
are completely untouched by the force-flush call — only the brief's own items get scored and
selected.
AC2: the sealed artifact round-trips exactly (no additions/removals) and is immutable.
AC3: an empty live pending-set + one worthy event still selects that event (force, not
accumulation-gated).
Plus: a filtered-out item (fails ``is_surfacing_worthy``) is absent from the artifact, and the
stage never touches ``ctx.journal``.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import NoReturn
from zoneinfo import ZoneInfo

import pytest
from cogworx.claims.provenance import Artifact, Provenance
from cogworx.loop.result import Transition

from tests.support.stage_context_fake import StageContextFake
from wombat.calendar.models import CalendarEvent
from wombat.domain.brief_decision_artifact import BriefDecisionArtifact
from wombat.domain.brief_payload import BriefPayload, GmailBriefItem
from wombat.gate.decay import LedgerReset
from wombat.gate.models import GateItem, ItemKind, ScoredItem
from wombat.gate.pending_set import InMemoryPendingJournal, PendingSet
from wombat.gate.pipeline import Gate
from wombat.integrations.gmail.triage import PriorityBand
from wombat.rating.params import EventClass, RatingParams
from wombat.stages.artifacts import BRIEF_DECISION, BRIEF_PAYLOAD
from wombat.stages.brief_force_flush_stage import BriefForceFlushStage

_TZ = ZoneInfo("UTC")
_NOW = datetime(2026, 7, 3, 8, 0, tzinfo=UTC)

# urgency_base=0, urgency_gain=1 -> urgency == raw_urgency exactly, so the arithmetic below is
# exact and reproducible: raw_urgency = 0.55*time_term + 0.45*sender_term.
_IDENTITY_PARAMS = RatingParams(urgency_base=0.0, urgency_gain=1.0, load_base=0.0, load_gain=0.0)


class _NoOpRollover:
    """A ``DayRolloverProtocol`` double that never fires — this stage's tests never exercise
    decay/rollover (``select_items`` doesn't call either)."""

    def check(self) -> LedgerReset | None:
        return None


@dataclass
class _FakeUserModel:
    """Fixed RatingParams regardless of the resolved event class (mirrors ``test_pipeline.py``'s
    fake) — scoring is driven entirely by each item's own payload, via the REAL scoring fns."""

    rating_params: RatingParams

    def resolve_event_class(self, item: GateItem) -> EventClass:
        return EventClass.GENERIC

    async def ratings_for(self, item: GateItem) -> RatingParams:
        return self.rating_params


@dataclass
class _SpyCeiling:
    """A ``CeilingProtocol`` spy: records every call so AC1 can assert it was NEVER touched."""

    allow_calls: list[EventClass] = field(default_factory=list)
    record_calls: list[EventClass] = field(default_factory=list)

    def allow(self, event_class: EventClass) -> bool:
        self.allow_calls.append(event_class)
        return True

    def record(self, event_class: EventClass) -> None:
        self.record_calls.append(event_class)


@dataclass
class _SpyFlushLatch:
    """A ``FlushLatchProtocol`` spy (TK-287): records every call so AC1 can assert it was NEVER
    touched — ``select_items`` reads/records nothing on the flush latch either."""

    allow_calls: int = 0
    record_calls: int = 0
    note_denied_calls: int = 0

    def allow(self) -> bool:
        self.allow_calls += 1
        return True

    def record(self) -> None:
        self.record_calls += 1

    def note_denied(self) -> None:
        self.note_denied_calls += 1


def _make_gate(
    *,
    pending_set: PendingSet,
    ceiling: _SpyCeiling,
    urgency_threshold: float,
    flush_latch: _SpyFlushLatch | None = None,
) -> Gate:
    return Gate(
        user_model=_FakeUserModel(rating_params=_IDENTITY_PARAMS),
        pending_set=pending_set,
        ceiling=ceiling,
        urgency_threshold=urgency_threshold,
        load_flush_threshold=10_000.0,
        flush_min_age_seconds=10_000.0,
        decay_ttl_seconds=float("inf"),
        day_rollover=_NoOpRollover(),
        clock=lambda: 1000.0,
        flush_latch=flush_latch or _SpyFlushLatch(),
    )


@dataclass
class _JournalSpyStageContext(StageContextFake):
    """Turns any ``ctx.journal`` access into a loud, specific failure (mirrors the TK-98 test's
    pattern) so the "never touches ctx.journal" claim is proven by an explicit trap."""

    journal_accessed: bool = False

    @property
    def journal(self) -> NoReturn:
        self.journal_accessed = True
        raise AssertionError(
            "BriefForceFlushStage touched ctx.journal — stages never journal directly"
        )


def _make_ctx(payload: BriefPayload) -> _JournalSpyStageContext:
    art = Artifact(
        kind=BRIEF_PAYLOAD,
        produced_by="brief_gather",
        provenance=Provenance(source="system", confidence=1.0, recorded_at=_NOW),
        data=payload.to_payload(),
    )
    return _JournalSpyStageContext(now_fn=lambda: _NOW, last_output_map={"brief_gather": art})


def _event(event_id: str, *, offset: timedelta, duration: timedelta) -> CalendarEvent:
    start = _NOW + offset
    return CalendarEvent(
        event_id=event_id, title=event_id, start=start, end=start + duration, all_day=False
    )


def _gmail(message_id: str, *, band: PriorityBand) -> GmailBriefItem:
    return GmailBriefItem(
        message_id=message_id,
        subject="subject",
        sender="sender@example.com",
        received_at=_NOW,
        urgency_score=0.9 if band is PriorityBand.HIGH else 0.1,
        priority_band=band,
        matched_rules=(),
    )


def _seeded_scored_item(item_id: str) -> ScoredItem:
    return ScoredItem(item_id=item_id, item_kind=ItemKind.GENERIC, urgency=0.5, load=1.0)


# ------------------------------------------------------------------------------------------ AC1


async def test_ac1_real_gate_selects_worthy_brief_items_without_touching_live_pending_state() -> (
    None
):
    # 3 events: evt-1/evt-2 near + overlapping (both worthy, and conflict with each other but the
    # DERIVED conflict item itself scores below threshold); evt-3 far (not worthy).
    events = (
        _event("evt-1", offset=timedelta(minutes=30), duration=timedelta(minutes=30)),
        _event("evt-2", offset=timedelta(minutes=45), duration=timedelta(minutes=30)),
        _event("evt-3", offset=timedelta(hours=10), duration=timedelta(minutes=30)),
    )
    # 5 gmail items: 2 HIGH (worthy), 3 NORMAL (not worthy).
    gmail_items = (
        _gmail("msg-high-1", band=PriorityBand.HIGH),
        _gmail("msg-high-2", band=PriorityBand.HIGH),
        _gmail("msg-normal-1", band=PriorityBand.NORMAL),
        _gmail("msg-normal-2", band=PriorityBand.NORMAL),
        _gmail("msg-normal-3", band=PriorityBand.NORMAL),
    )
    payload = BriefPayload(
        generated_at=_NOW,
        calendar_events=events,
        gmail_items=gmail_items,
        calendar_unavailable=False,
        gmail_unavailable=False,
    )

    journal = InMemoryPendingJournal()
    pending_set = PendingSet(journal=journal, max_pending=50)
    pending_set.add(_seeded_scored_item("unrelated-1"), added_at=1.0)
    pending_set.add(_seeded_scored_item("unrelated-2"), added_at=2.0)

    live_items_before = pending_set.list()
    cumulative_load_before = pending_set.cumulative_load()
    journal_len_before = len(journal.replay())

    ceiling = _SpyCeiling()
    flush_latch = _SpyFlushLatch()
    gate = _make_gate(
        pending_set=pending_set, ceiling=ceiling, urgency_threshold=0.2, flush_latch=flush_latch
    )
    stage = BriefForceFlushStage(select_items=gate.select_items, tz=_TZ)
    ctx = _make_ctx(payload)

    result = await stage.run(ctx)

    # --- audit-CRITICAL: the live pending set / journal / ceiling / flush latch are completely
    # untouched ---
    assert {item.item_id for item in pending_set.list()} == {"unrelated-1", "unrelated-2"}
    assert pending_set.list() == live_items_before
    assert pending_set.cumulative_load() == cumulative_load_before
    assert len(journal.replay()) == journal_len_before
    assert ceiling.allow_calls == []
    assert ceiling.record_calls == []
    assert flush_latch.allow_calls == 0  # TK-287 AC3: select_items touches no latch state
    assert flush_latch.record_calls == 0
    assert flush_latch.note_denied_calls == 0

    # --- the artifact contains exactly the brief items that pass is_surfacing_worthy ---
    assert isinstance(result, Transition)
    assert result.to == "brief_compose"
    assert result.output.kind == BRIEF_DECISION
    assert result.output.produced_by == "brief_force_flush"

    artifact = BriefDecisionArtifact.from_payload(result.output.data)
    assert artifact.item_kind == "brief"
    assert [e["event_id"] for e in artifact.to_payload()["bucket"]["prep"]] == ["evt-1", "evt-2"]
    assert artifact.to_payload()["bucket"]["conflict"] == []  # the derived conflict was filtered
    assert [g["message_id"] for g in artifact.to_payload()["bucket"]["recap"]] == [
        "msg-high-1",
        "msg-high-2",
    ]


# ------------------------------------------------------------------------------------------ AC2


async def test_ac2_sealed_artifact_round_trips_exactly_and_is_immutable() -> None:
    payload = BriefPayload(
        generated_at=_NOW,
        calendar_events=(
            _event("evt-1", offset=timedelta(minutes=5), duration=timedelta(minutes=30)),
        ),
        gmail_items=(),
        calendar_unavailable=False,
        gmail_unavailable=False,
    )
    pending_set = PendingSet(journal=InMemoryPendingJournal(), max_pending=50)
    ceiling = _SpyCeiling()
    gate = _make_gate(pending_set=pending_set, ceiling=ceiling, urgency_threshold=0.0)
    stage = BriefForceFlushStage(select_items=gate.select_items, tz=_TZ)
    ctx = _make_ctx(payload)

    result = await stage.run(ctx)

    assert isinstance(result, Transition)
    artifact = BriefDecisionArtifact.from_payload(result.output.data)
    # No additions/removals across the round-trip.
    assert artifact.to_payload() == result.output.data
    assert BriefDecisionArtifact.from_payload(artifact.to_payload()) == artifact

    with pytest.raises(dataclasses.FrozenInstanceError):
        artifact.calendar_unavailable = True  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        artifact.bucket.prep = ()  # type: ignore[misc]


# ------------------------------------------------------------------------------------------ AC3


async def test_ac3_empty_pending_set_still_force_selects_the_one_worthy_event() -> None:
    payload = BriefPayload(
        generated_at=_NOW,
        calendar_events=(
            _event("evt-1", offset=timedelta(minutes=5), duration=timedelta(minutes=30)),
        ),
        gmail_items=(),
        calendar_unavailable=False,
        gmail_unavailable=False,
    )
    pending_set = PendingSet(journal=InMemoryPendingJournal(), max_pending=50)
    assert pending_set.list() == []  # empty live pending-set

    ceiling = _SpyCeiling()
    gate = _make_gate(pending_set=pending_set, ceiling=ceiling, urgency_threshold=0.0)
    stage = BriefForceFlushStage(select_items=gate.select_items, tz=_TZ)
    ctx = _make_ctx(payload)

    result = await stage.run(ctx)

    assert isinstance(result, Transition)
    artifact = BriefDecisionArtifact.from_payload(result.output.data)
    assert [e.event_id for e in artifact.bucket.prep] == ["evt-1"]
    # The force-flush never touched the (still empty) live pending set.
    assert pending_set.list() == []


# ---------------------------------------------------------------------------------- filtered-out


async def test_filtered_out_item_that_fails_is_surfacing_worthy_is_absent_from_artifact() -> None:
    payload = BriefPayload(
        generated_at=_NOW,
        calendar_events=(
            _event("evt-far", offset=timedelta(hours=10), duration=timedelta(minutes=30)),
        ),
        gmail_items=(_gmail("msg-quiet", band=PriorityBand.NORMAL),),
        calendar_unavailable=False,
        gmail_unavailable=False,
    )
    pending_set = PendingSet(journal=InMemoryPendingJournal(), max_pending=50)
    ceiling = _SpyCeiling()
    gate = _make_gate(pending_set=pending_set, ceiling=ceiling, urgency_threshold=0.5)
    stage = BriefForceFlushStage(select_items=gate.select_items, tz=_TZ)
    ctx = _make_ctx(payload)

    result = await stage.run(ctx)

    assert isinstance(result, Transition)
    artifact = BriefDecisionArtifact.from_payload(result.output.data)
    assert artifact.bucket.prep == ()
    assert artifact.bucket.recap == ()
    assert artifact.bucket.conflict == ()


# ---------------------------------------------------------------------------------- ctx.journal


async def test_stage_never_touches_ctx_journal() -> None:
    payload = BriefPayload(
        generated_at=_NOW,
        calendar_events=(),
        gmail_items=(),
        calendar_unavailable=False,
        gmail_unavailable=False,
    )
    pending_set = PendingSet(journal=InMemoryPendingJournal(), max_pending=50)
    ceiling = _SpyCeiling()
    gate = _make_gate(pending_set=pending_set, ceiling=ceiling, urgency_threshold=0.5)
    stage = BriefForceFlushStage(select_items=gate.select_items, tz=_TZ)
    ctx = _make_ctx(payload)

    result = await stage.run(ctx)

    assert isinstance(result, Transition)
    assert ctx.journal_accessed is False
