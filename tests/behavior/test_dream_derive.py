"""TK-299 — DreamDeriveStage acceptance criteria (EP-37, DEC-66).

In-memory/monkeypatched substrate, ZERO network: mirrors ``tests/behavior/test_dream_facts.py``'s
own idiom — ``external_items``/``user_facts`` are REAL ``ExternalItemStore``/``UserFactsStore``
instances over an unreachable DSN (lazy — never actually connects) with their public methods
monkeypatched to recording/canned/raising doubles. The genuine pg round-trips for both stores live
in their own pg-gated test modules; this module is about ``DreamDeriveStage``'s own
read/derive/cap/write logic — PURE CODE, no model, no network.

  AC1: a seeded ``ExternalItemStore`` with a weekly recurring meeting (same normalized
      title/weekday/near-same start) across 3 distinct weeks, a non-recurring/scatter meeting, an
      all-day event sharing the recurring meeting's title/weekday, and gmail rows with one sender
      over the frequency threshold plus scatter senders under it -> exactly the recurring-meeting
      fact and the frequent-correspondent fact land, ``source="derived"``, stable keys, templated
      third-person text; the non-qualifying rows produce nothing.
  AC2: the same seed run a second night is idempotent — the same keys re-upsert, the store's row
      count stays unchanged, and NEITHER fact logs a "new fact" journal line the second time.
  AC3: an empty/sparse store (the ordinary no-signal-yet night), a ``None`` store, and a raising
      store (each separately, for both the read side and the write side) -> zero writes, a loud
      ERROR log for the ``None``/raising cases (never for the merely-empty case), and the stage
      STILL transitions onward.
  AC4: a seed deriving more than ``_MAX_DERIVED_FACTS`` stops at the pinned cap, deterministically
      (stable ordering), loudly logged.
  AC5 (tolerance): the 30-minute-tolerance reading of "same start time" is exercised directly via
      ``_derive_meeting_facts``.
"""

from __future__ import annotations

import calendar
import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from cogworx.loop.result import Transition

from tests.support.stage_context_fake import StageContextFake
from wombat.behavior.stages.dream_derive import (
    _MAX_DERIVED_FACTS,
    _MIN_RECURRENCE_WEEKS,
    DreamDeriveStage,
    _derive_meeting_facts,
)
from wombat.external_store import ExternalItemStore
from wombat.user_facts import UserFactsStore

_NOW = datetime(2026, 7, 30, 3, 0, 0, tzinfo=UTC)
_TZ = ZoneInfo("UTC")
_UNREACHABLE_DSN = "postgresql://nonexistent-host-should-never-be-dialed:1/db"


def _gcal_row(
    item_key: str, title: str, start: datetime, *, all_day: bool = False
) -> dict[str, Any]:
    return {
        "item_key": item_key,
        "payload": {
            "event_id": item_key,
            "title": title,
            "start": start.isoformat(),
            "end": start.isoformat(),
            "all_day": all_day,
        },
        "occurs_at": start,
        "fetched_at": _NOW,
        "first_seen_at": _NOW,
    }


def _gmail_row(item_key: str, sender: str, received: datetime) -> dict[str, Any]:
    return {
        "item_key": item_key,
        "payload": {
            "message_id": item_key,
            "subject": "subject",
            "sender": sender,
            "received_at": received.isoformat(),
            "priority_band": "normal",
        },
        "occurs_at": received,
        "fetched_at": _NOW,
        "first_seen_at": _NOW,
    }


def _fake_external_items(
    monkeypatch: pytest.MonkeyPatch,
    *,
    gcal_rows: list[dict[str, Any]] | None = None,
    gmail_rows: list[dict[str, Any]] | None = None,
    gcal_raises: BaseException | None = None,
    gmail_raises: BaseException | None = None,
) -> tuple[ExternalItemStore, list[tuple[str, datetime, datetime]]]:
    calls: list[tuple[str, datetime, datetime]] = []

    def _get_window(
        self: ExternalItemStore, source: str, start: datetime, end: datetime
    ) -> list[dict[str, Any]]:
        calls.append((source, start, end))
        if source == "gcal":
            if gcal_raises is not None:
                raise gcal_raises
            return gcal_rows or []
        if source == "gmail":
            if gmail_raises is not None:
                raise gmail_raises
            return gmail_rows or []
        raise AssertionError(f"unexpected source {source!r}")

    monkeypatch.setattr(ExternalItemStore, "get_window", _get_window)
    return ExternalItemStore(_UNREACHABLE_DSN), calls


def _fake_user_facts(
    monkeypatch: pytest.MonkeyPatch,
    *,
    existing: dict[str, str] | None = None,
    raises_upsert_on_call: int | None = None,
) -> tuple[UserFactsStore, list[tuple[str, str, str]]]:
    """A stateful in-memory double: ``existing`` seeds pre-known ``{fact_key: fact_text}`` rows;
    ``raises_upsert_on_call`` (1-indexed) makes exactly that ``upsert_fact`` call raise — mirrors
    ``test_dream_facts.py``'s own fake exactly."""
    rows: dict[str, str] = dict(existing or {})
    calls: list[tuple[str, str, str]] = []
    call_index = {"n": 0}

    def _count(self: UserFactsStore) -> int:
        return len(rows)

    def _list_facts(self: UserFactsStore, limit: int) -> list[dict[str, Any]]:
        return [{"fact_key": key, "fact": text} for key, text in list(rows.items())[:limit]]

    def _upsert_fact(self: UserFactsStore, fact_key: str, fact: str, source: str) -> None:
        call_index["n"] += 1
        calls.append((fact_key, fact, source))
        if raises_upsert_on_call is not None and call_index["n"] == raises_upsert_on_call:
            raise RuntimeError(f"simulated upsert_fact failure on call {call_index['n']} — AC3")
        rows[fact_key] = fact

    monkeypatch.setattr(UserFactsStore, "count", _count)
    monkeypatch.setattr(UserFactsStore, "list_facts", _list_facts)
    monkeypatch.setattr(UserFactsStore, "upsert_fact", _upsert_fact)
    return UserFactsStore(_UNREACHABLE_DSN), calls


# ================================================================================================
# AC1: exactly the recurring-meeting fact + frequent-correspondent fact land; scatter yields
# nothing
# ================================================================================================


async def test_ac1_qualifying_rows_land_exactly_two_facts_scatter_yields_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = (_NOW - timedelta(days=21)).replace(hour=9, minute=0, second=0, microsecond=0)
    weekday_name = calendar.day_name[base.weekday()]

    gcal_rows = [
        _gcal_row("meet-1", "Team Standup", base),
        _gcal_row("meet-2", "Team Standup", base + timedelta(weeks=1)),
        _gcal_row("meet-3", "Team Standup", base + timedelta(weeks=2)),
        # Scatter: only ONE occurrence, never recurs — must not qualify.
        _gcal_row("meet-scatter", "One-off Sync", base + timedelta(days=1)),
        # Same title/weekday/time as the qualifying meeting but all-day — must not count toward
        # (or pollute) the recurring group.
        _gcal_row("meet-allday", "Team Standup", base + timedelta(weeks=3), all_day=True),
    ]
    gmail_rows = [
        _gmail_row(f"msg-freq-{i}", "alex@example.com", base + timedelta(days=i))
        for i in range(5)
    ] + [
        # Scatter senders, each under the threshold.
        _gmail_row("msg-scatter-1", "sam@example.com", base),
        _gmail_row("msg-scatter-2", "sam@example.com", base + timedelta(days=1)),
        _gmail_row("msg-scatter-3", "jo@example.com", base),
    ]

    external_items, _calls = _fake_external_items(
        monkeypatch, gcal_rows=gcal_rows, gmail_rows=gmail_rows
    )
    user_facts, upsert_calls = _fake_user_facts(monkeypatch)

    stage = DreamDeriveStage(external_items=external_items, user_facts=user_facts, tz=_TZ)
    result = await stage.run(StageContextFake(now_fn=lambda: _NOW))

    assert isinstance(result, Transition)
    assert result.to == "dream_observe"
    assert result.output.data == {"new_facts": 2}

    assert len(upsert_calls) == 2
    assert all(source == "derived" for _key, _fact, source in upsert_calls)

    keys = {key for key, _fact, _source in upsert_calls}
    expected_meeting_key = f"derived:meeting:team-standup:{weekday_name.lower()}"
    expected_correspondent_key = "derived:correspondent:alex-example-com"
    assert keys == {expected_meeting_key, expected_correspondent_key}

    facts_by_key = {key: fact for key, fact, _source in upsert_calls}
    assert facts_by_key[expected_meeting_key] == f"Has Team Standup on {weekday_name}s around 09:00"
    assert facts_by_key[expected_correspondent_key] == "Corresponds often with alex@example.com"


# ================================================================================================
# AC2: idempotent second night — same keys re-upsert, no duplicates, no repeated "new fact" logs
# ================================================================================================


async def test_ac2_second_night_is_idempotent_no_duplicates_no_repeat_new_fact_logs(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    base = (_NOW - timedelta(days=21)).replace(hour=9, minute=0, second=0, microsecond=0)
    gcal_rows = [
        _gcal_row(f"meet-{i}", "Team Standup", base + timedelta(weeks=i)) for i in range(3)
    ]

    external_items, _calls = _fake_external_items(monkeypatch, gcal_rows=gcal_rows)
    user_facts, upsert_calls = _fake_user_facts(monkeypatch)
    stage = DreamDeriveStage(external_items=external_items, user_facts=user_facts, tz=_TZ)

    with caplog.at_level(logging.INFO, logger="wombat.behavior.stages.dream_derive"):
        first = await stage.run(StageContextFake(now_fn=lambda: _NOW))
        caplog.clear()
        second = await stage.run(StageContextFake(now_fn=lambda: _NOW))

    assert isinstance(first, Transition)
    assert isinstance(second, Transition)
    assert first.output.data == {"new_facts": 1}
    assert second.output.data == {"new_facts": 0}
    # Both nights attempted the write (idempotent re-upsert), but only ONE row exists in the store.
    assert len(upsert_calls) == 2
    assert len({key for key, _fact, _source in upsert_calls}) == 1

    accepted_lines = [
        r.getMessage() for r in caplog.records if "accepted new fact" in r.getMessage()
    ]
    assert accepted_lines == []  # the second run logged NO new-fact line


# ================================================================================================
# AC3: never-block — empty/None/raising stores (read side and write side)
# ================================================================================================


async def test_ac3_empty_store_is_the_ordinary_case_zero_writes_no_error(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    external_items, _calls = _fake_external_items(monkeypatch)  # zero rows for both sources
    user_facts, upsert_calls = _fake_user_facts(monkeypatch)
    stage = DreamDeriveStage(external_items=external_items, user_facts=user_facts, tz=_TZ)

    with caplog.at_level(logging.ERROR, logger="wombat.behavior.stages.dream_derive"):
        result = await stage.run(StageContextFake(now_fn=lambda: _NOW))

    assert isinstance(result, Transition)
    assert result.to == "dream_observe"
    assert result.output.data == {"new_facts": 0}
    assert upsert_calls == []
    assert not any(r.levelno == logging.ERROR for r in caplog.records)


async def test_ac3_none_external_items_store_is_caught_loud_and_still_transitions(
    caplog: pytest.LogCaptureFixture,
) -> None:
    stage = DreamDeriveStage(
        external_items=None, user_facts=UserFactsStore(_UNREACHABLE_DSN), tz=_TZ
    )

    with caplog.at_level(logging.ERROR, logger="wombat.behavior.stages.dream_derive"):
        result = await stage.run(StageContextFake(now_fn=lambda: _NOW))

    assert isinstance(result, Transition)
    assert result.to == "dream_observe"
    assert result.output.data == {"new_facts": 0}
    assert any(r.levelno == logging.ERROR for r in caplog.records)


async def test_ac3_raising_get_window_for_both_sources_is_caught_loud_and_still_transitions(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    external_items, _calls = _fake_external_items(
        monkeypatch,
        gcal_raises=RuntimeError("simulated gcal get_window failure — AC3"),
        gmail_raises=RuntimeError("simulated gmail get_window failure — AC3"),
    )
    user_facts, upsert_calls = _fake_user_facts(monkeypatch)
    stage = DreamDeriveStage(external_items=external_items, user_facts=user_facts, tz=_TZ)

    with caplog.at_level(logging.ERROR, logger="wombat.behavior.stages.dream_derive"):
        result = await stage.run(StageContextFake(now_fn=lambda: _NOW))

    assert isinstance(result, Transition)
    assert result.to == "dream_observe"
    assert result.output.data == {"new_facts": 0}
    assert upsert_calls == []
    error_messages = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert any("gcal" in m for m in error_messages)
    assert any("gmail" in m for m in error_messages)


async def test_ac3_none_user_facts_store_skips_all_writes_loud_and_still_transitions(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    base = (_NOW - timedelta(days=21)).replace(hour=9, minute=0, second=0, microsecond=0)
    gcal_rows = [
        _gcal_row(f"meet-{i}", "Team Standup", base + timedelta(weeks=i)) for i in range(3)
    ]
    external_items, _calls = _fake_external_items(monkeypatch, gcal_rows=gcal_rows)
    stage = DreamDeriveStage(external_items=external_items, user_facts=None, tz=_TZ)

    with caplog.at_level(logging.ERROR, logger="wombat.behavior.stages.dream_derive"):
        result = await stage.run(StageContextFake(now_fn=lambda: _NOW))

    assert isinstance(result, Transition)
    assert result.to == "dream_observe"
    assert result.output.data == {"new_facts": 0}
    assert any(r.levelno == logging.ERROR for r in caplog.records)


async def test_ac3_raising_upsert_fact_mid_batch_loses_only_that_one_fact(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    base = (_NOW - timedelta(days=21)).replace(hour=9, minute=0, second=0, microsecond=0)
    gcal_rows = [
        _gcal_row(f"meet-{i}", "Team Standup", base + timedelta(weeks=i)) for i in range(3)
    ]
    gmail_rows = [
        _gmail_row(f"msg-{i}", "alex@example.com", base + timedelta(days=i)) for i in range(5)
    ]
    external_items, _calls = _fake_external_items(
        monkeypatch, gcal_rows=gcal_rows, gmail_rows=gmail_rows
    )
    user_facts, upsert_calls = _fake_user_facts(monkeypatch, raises_upsert_on_call=1)
    stage = DreamDeriveStage(external_items=external_items, user_facts=user_facts, tz=_TZ)

    with caplog.at_level(logging.ERROR, logger="wombat.behavior.stages.dream_derive"):
        result = await stage.run(StageContextFake(now_fn=lambda: _NOW))

    assert isinstance(result, Transition)
    assert result.to == "dream_observe"
    # Two candidates total (one meeting, one correspondent); the first upsert call raised, the
    # second still landed — new_facts counts only the SUCCESSFUL upsert.
    assert result.output.data == {"new_facts": 1}
    assert len(upsert_calls) == 2
    assert any(r.levelno == logging.ERROR for r in caplog.records)


# ================================================================================================
# AC4: over-cap seed stops at _MAX_DERIVED_FACTS deterministically, loudly logged
# ================================================================================================


async def test_ac4_over_cap_seed_stops_at_the_pinned_cap_deterministically(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    base = (_NOW - timedelta(days=21)).replace(hour=9, minute=0, second=0, microsecond=0)
    # 7 distinct qualifying recurring-meeting groups (well over _MAX_DERIVED_FACTS=5), each its
    # own title so each is its own group.
    gcal_rows: list[dict[str, Any]] = []
    for group in range(7):
        for week in range(_MIN_RECURRENCE_WEEKS):
            gcal_rows.append(
                _gcal_row(
                    f"meet-{group}-{week}",
                    f"Meeting {group}",
                    base + timedelta(weeks=week, hours=group),
                )
            )

    external_items, _calls = _fake_external_items(monkeypatch, gcal_rows=gcal_rows)
    user_facts, upsert_calls = _fake_user_facts(monkeypatch)
    stage = DreamDeriveStage(external_items=external_items, user_facts=user_facts, tz=_TZ)

    with caplog.at_level(logging.WARNING, logger="wombat.behavior.stages.dream_derive"):
        result = await stage.run(StageContextFake(now_fn=lambda: _NOW))

    assert isinstance(result, Transition)
    assert result.output.data == {"new_facts": _MAX_DERIVED_FACTS}
    assert len(upsert_calls) == _MAX_DERIVED_FACTS

    # Deterministic: re-running derivation directly yields the SAME 5 keys, in the SAME order.
    landed_keys = [key for key, _fact, _source in upsert_calls]
    all_qualifying = _derive_meeting_facts(gcal_rows, _TZ)
    assert landed_keys == [key for key, _fact in all_qualifying[:_MAX_DERIVED_FACTS]]

    warning_messages = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("cap" in m for m in warning_messages)


# ================================================================================================
# AC5 (tolerance): the pinned 30-minute-tolerance reading, exercised directly on the pure function
# ================================================================================================


def test_ac5_start_times_within_thirty_minutes_group_together_outside_do_not() -> None:
    base = (_NOW - timedelta(days=21)).replace(hour=9, minute=0, second=0, microsecond=0)
    # Three occurrences, each nudged a few minutes so none share an exact start time — all round
    # to the SAME half-hour bucket (09:00).
    rows = [
        _gcal_row("meet-0", "Team Standup", base),
        _gcal_row("meet-1", "Team Standup", base + timedelta(weeks=1, minutes=10)),
        _gcal_row("meet-2", "Team Standup", base + timedelta(weeks=2, minutes=-10)),
    ]
    facts = _derive_meeting_facts(rows, _TZ)
    assert len(facts) == 1
    _key, text = facts[0]
    assert "around 09:00" in text

    # A fourth occurrence 45 minutes later (rounds to a DIFFERENT half-hour bucket) never joins
    # the group, so it alone stays below the 3-week bar.
    rows_with_outlier = [
        *rows,
        _gcal_row("meet-3", "Team Standup", base + timedelta(weeks=3, minutes=45)),
    ]
    facts_with_outlier = _derive_meeting_facts(rows_with_outlier, _TZ)
    assert facts_with_outlier == facts  # the outlier's own bucket never reaches 3 distinct weeks


# ================================================================================================
# AC6 (repair, batch review finding): weekday/time are derived in the injected tz, not the UTC the
# gcal poller stores ``start`` in — a Monday 18:00 America/Los_Angeles standup must never be
# bucketed onto "Tuesday ~01:00".
# ================================================================================================


def test_ac6_weekday_and_time_are_derived_in_the_injected_tz_not_stored_utc() -> None:
    la_tz = ZoneInfo("America/Los_Angeles")
    # A Monday 18:00 America/Los_Angeles standup is Tuesday 01:00 UTC — payload["start"] is
    # stored UTC (mirrors integrations/gcal/poller.py's own normalization).
    monday_18_local = datetime(2026, 6, 1, 18, 0, tzinfo=la_tz)
    assert monday_18_local.weekday() == 0  # sanity: 2026-06-01 is a Monday in LA
    rows = [
        _gcal_row(f"meet-{i}", "Standup", monday_18_local.astimezone(UTC) + timedelta(weeks=i))
        for i in range(_MIN_RECURRENCE_WEEKS)
    ]

    utc_facts = _derive_meeting_facts(rows, _TZ)
    la_facts = _derive_meeting_facts(rows, la_tz)

    assert len(la_facts) == 1
    _la_key, la_text = la_facts[0]
    assert "Mondays around 18:00" in la_text
    assert "tuesday" not in _la_key
    assert _la_key.endswith(":monday")

    # The UTC read of the SAME rows lands on the wrong day/time — proving the derivation is
    # actually tz-sensitive rather than coincidentally matching.
    assert len(utc_facts) == 1
    _utc_key, utc_text = utc_facts[0]
    assert "Tuesdays around 01:00" in utc_text
    assert utc_facts != la_facts
