"""TK-314 — DreamObserveStage acceptance criteria (EP-37, DEC-68(d)(2)).

In-memory/monkeypatched substrate, ZERO network: mirrors ``tests/behavior/test_dream_derive.py``'s
own idiom — ``observations``/``user_facts`` are REAL ``ObservationStore``/``UserFactsStore``
instances over an unreachable DSN (lazy — never actually connects) with their public methods
monkeypatched to recording/canned/raising doubles. The genuine pg round-trips for both stores live
in their own pg-gated test modules; this module is about ``DreamObserveStage``'s own
read/derive/cap/write logic — PURE CODE, no model, no network.

  AC1: a seeded ledger with one qualifying weekday arrival rhythm, one qualifying morning app
      residency, and non-qualifying scatter (a weekend segment, an under-3-days afternoon, a
      one-off early arrival) -> exactly the two templated facts land, ``source="behavior"``,
      stable keys, closed third-person text; the scatter yields nothing — and NO raw window title
      ever appears in any fact text.
  AC1b (call rhythm): mic ``in_call`` segments on the same weekday across 2 distinct ISO weeks ->
      the call-rhythm fact; a single-week weekday yields nothing.
  AC2: the same seed run a second night is idempotent — the same keys re-upsert, the store's row
      count stays unchanged, and NO fact logs a "new fact" journal line the second time.
  AC3: an empty ledger (the ordinary no-signal night — no error), a ``None`` ObservationStore
      (ONE WARNING, never an ERROR — a toggle-off boot is legitimate), a raising ``get_window``
      (loud ERROR per channel), a ``None`` user_facts store, and a raising ``upsert_fact``
      mid-batch -> zero/partial writes as specified and the stage STILL transitions onward.
  AC4: more than ``_MAX_OBSERVE_FACTS`` qualifying candidates stop at the pinned cap,
      deterministically (stable selection order), loudly logged.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from cogworx.loop.result import Transition

from tests.support.stage_context_fake import StageContextFake
from wombat.behavior.stages.dream_observe import (
    _MAX_OBSERVE_FACTS,
    DreamObserveStage,
    _derive_call_facts,
    _round_up_to_half_hour,
)
from wombat.observations import ObservationStore
from wombat.user_facts import UserFactsStore

_NOW = datetime(2026, 7, 30, 3, 0, 0, tzinfo=UTC)  # a Thursday
_TZ = ZoneInfo("UTC")
_UNREACHABLE_DSN = "postgresql://nonexistent-host-should-never-be-dialed:1/db"

# 2026-07-13 and 2026-07-20 are the Mondays of the two full ISO weeks inside the 21-day lookback.
_MONDAY_W1 = datetime(2026, 7, 13, 0, 0, tzinfo=UTC)
_MONDAY_W2 = datetime(2026, 7, 20, 0, 0, tzinfo=UTC)

_SECRET_TITLE = "SECRET window title that must never fossilize into a fact"


def _screen_row(app: str, start: datetime, end: datetime) -> dict[str, Any]:
    return {
        "id": 0,
        "channel": "screen",
        "kind": "app_segment",
        "started_at": start,
        "ended_at": end,
        "payload": {"app": app, "title": _SECRET_TITLE},
        "day_key": start.astimezone(_TZ).date(),
    }


def _mic_row(start: datetime, end: datetime) -> dict[str, Any]:
    return {
        "id": 0,
        "channel": "mic",
        "kind": "in_call",
        "started_at": start,
        "ended_at": end,
        "payload": {},
        "day_key": start.astimezone(_TZ).date(),
    }


def _fake_observations(
    monkeypatch: pytest.MonkeyPatch,
    *,
    screen_rows: list[dict[str, Any]] | None = None,
    mic_rows: list[dict[str, Any]] | None = None,
    screen_raises: BaseException | None = None,
    mic_raises: BaseException | None = None,
) -> tuple[ObservationStore, list[tuple[str, datetime, datetime]]]:
    calls: list[tuple[str, datetime, datetime]] = []

    def _get_window(
        self: ObservationStore, channel: str, start: datetime, end: datetime
    ) -> list[dict[str, Any]]:
        calls.append((channel, start, end))
        if channel == "screen":
            if screen_raises is not None:
                raise screen_raises
            return screen_rows or []
        if channel == "mic":
            if mic_raises is not None:
                raise mic_raises
            return mic_rows or []
        raise AssertionError(f"unexpected channel {channel!r}")

    monkeypatch.setattr(ObservationStore, "get_window", _get_window)
    return ObservationStore(_UNREACHABLE_DSN), calls


def _fake_user_facts(
    monkeypatch: pytest.MonkeyPatch,
    *,
    existing: dict[str, str] | None = None,
    raises_upsert_on_call: int | None = None,
) -> tuple[UserFactsStore, list[tuple[str, str, str]]]:
    """A stateful in-memory double — mirrors ``test_dream_derive.py``'s own fake exactly."""
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


def _qualifying_screen_seed() -> list[dict[str, Any]]:
    """Mon/Tue/Wed of two ISO weeks: first segment 08:47 Firefox (arrival -> "by 09:00"), then a
    two-hour Visual Studio Code block (morning residency plurality). Plus scatter: one weekend
    segment, an afternoon on only 2 distinct days (under the 3-day bar), and one early-arrival
    Thursday whose 06:30 bucket never reaches the per-week bar."""
    rows: list[dict[str, Any]] = []
    for monday in (_MONDAY_W1, _MONDAY_W2):
        for day_offset in range(3):  # Mon, Tue, Wed
            day = monday + timedelta(days=day_offset)
            first = day.replace(hour=8, minute=47)
            rows.append(_screen_row("Firefox", first, first + timedelta(minutes=5)))
            code_start = day.replace(hour=9, minute=0)
            code_end = code_start + timedelta(hours=2)
            rows.append(_screen_row("Visual Studio Code", code_start, code_end))
    # Weekend scatter (Saturday 2026-07-18) — weekends never feed weekday templates.
    saturday = _MONDAY_W1 + timedelta(days=5, hours=9)
    rows.append(_screen_row("Steam", saturday, saturday + timedelta(hours=1)))
    # Afternoon scatter on only TWO distinct days — under _MIN_DAYPART_DAYS, never a fact.
    for day_offset in range(2):
        pm = _MONDAY_W1 + timedelta(days=day_offset, hours=13)
        rows.append(_screen_row("Slack", pm, pm + timedelta(hours=1)))
    # A one-off early Thursday arrival — its 06:30 bucket has one day, never qualifies.
    thursday = _MONDAY_W2 + timedelta(days=3, hours=6, minutes=10)
    rows.append(_screen_row("Firefox", thursday, thursday + timedelta(minutes=30)))
    return rows


# ================================================================================================
# AC1: exactly the arrival + residency facts land; scatter yields nothing; titles never quoted
# ================================================================================================


async def test_ac1_qualifying_ledger_lands_exactly_arrival_and_residency_facts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observations, _calls = _fake_observations(monkeypatch, screen_rows=_qualifying_screen_seed())
    user_facts, upsert_calls = _fake_user_facts(monkeypatch)

    stage = DreamObserveStage(observations=observations, user_facts=user_facts, tz=_TZ)
    result = await stage.run(StageContextFake(now_fn=lambda: _NOW))

    assert isinstance(result, Transition)
    assert result.to == "dream_behavior_log"
    assert result.output.data == {"new_facts": 2}

    assert len(upsert_calls) == 2
    assert all(source == "behavior" for _key, _fact, source in upsert_calls)

    facts_by_key = {key: fact for key, fact, _source in upsert_calls}
    assert facts_by_key == {
        "behavior:arrival:weekday": "Usually at the computer by 09:00 on weekdays",
        "behavior:residency:morning": "Spends most weekday mornings in Visual Studio Code",
    }

    # TK-314 pinned: raw window titles NEVER fossilize into durable facts.
    assert all(_SECRET_TITLE not in fact for _key, fact, _source in upsert_calls)


async def test_ac1b_call_rhythm_fact_from_two_week_in_call_segments_single_week_yields_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tuesday_w1 = _MONDAY_W1 + timedelta(days=1, hours=14)
    tuesday_w2 = _MONDAY_W2 + timedelta(days=1, hours=14)
    friday_w1 = _MONDAY_W1 + timedelta(days=4, hours=10)  # ONE week only — never qualifies
    mic_rows = [
        _mic_row(tuesday_w1, tuesday_w1 + timedelta(minutes=30)),
        _mic_row(tuesday_w2, tuesday_w2 + timedelta(minutes=45)),
        _mic_row(friday_w1, friday_w1 + timedelta(minutes=20)),
    ]

    observations, _calls = _fake_observations(monkeypatch, mic_rows=mic_rows)
    user_facts, upsert_calls = _fake_user_facts(monkeypatch)

    stage = DreamObserveStage(observations=observations, user_facts=user_facts, tz=_TZ)
    result = await stage.run(StageContextFake(now_fn=lambda: _NOW))

    assert isinstance(result, Transition)
    assert result.output.data == {"new_facts": 1}
    assert upsert_calls == [
        ("behavior:calls:tuesday", "Regularly takes calls on Tuesdays", "behavior")
    ]


def test_arrival_rounding_is_ceiling_to_the_next_half_hour() -> None:
    assert _round_up_to_half_hour(8, 47) == (9, 0)
    assert _round_up_to_half_hour(9, 0) == (9, 0)
    assert _round_up_to_half_hour(9, 1) == (9, 30)
    assert _round_up_to_half_hour(23, 45) == (0, 0)  # wraps past midnight


# ================================================================================================
# AC2: idempotent second night — same keys re-upsert, no duplicates, no repeated "new fact" logs
# ================================================================================================


async def test_ac2_second_night_is_idempotent_no_duplicates_no_repeat_new_fact_logs(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    observations, _calls = _fake_observations(monkeypatch, screen_rows=_qualifying_screen_seed())
    user_facts, upsert_calls = _fake_user_facts(monkeypatch)
    stage = DreamObserveStage(observations=observations, user_facts=user_facts, tz=_TZ)

    with caplog.at_level(logging.INFO, logger="wombat.behavior.stages.dream_observe"):
        first = await stage.run(StageContextFake(now_fn=lambda: _NOW))
        caplog.clear()
        second = await stage.run(StageContextFake(now_fn=lambda: _NOW))

    assert isinstance(first, Transition)
    assert isinstance(second, Transition)
    assert first.output.data == {"new_facts": 2}
    assert second.output.data == {"new_facts": 0}
    # Both nights attempted the writes (idempotent re-upsert), but only TWO rows exist.
    assert len(upsert_calls) == 4
    assert len({key for key, _fact, _source in upsert_calls}) == 2

    accepted_lines = [
        r.getMessage() for r in caplog.records if "accepted new fact" in r.getMessage()
    ]
    assert accepted_lines == []  # the second run logged NO new-fact line


# ================================================================================================
# AC3: never-block — empty ledger, None store (WARNING only), raising reads/writes
# ================================================================================================


async def test_ac3_empty_ledger_is_the_ordinary_case_zero_writes_no_error(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    observations, calls = _fake_observations(monkeypatch)  # zero rows for both channels
    user_facts, upsert_calls = _fake_user_facts(monkeypatch)
    stage = DreamObserveStage(observations=observations, user_facts=user_facts, tz=_TZ)

    with caplog.at_level(logging.WARNING, logger="wombat.behavior.stages.dream_observe"):
        result = await stage.run(StageContextFake(now_fn=lambda: _NOW))

    assert isinstance(result, Transition)
    assert result.to == "dream_behavior_log"
    assert result.output.data == {"new_facts": 0}
    assert upsert_calls == []
    assert [channel for channel, _s, _e in calls] == ["screen", "mic"]  # BOTH channels read
    assert not any(r.levelno >= logging.WARNING for r in caplog.records)


async def test_ac3_none_observation_store_warns_once_never_errors_and_still_transitions(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A toggle-off boot legitimately constructs no ObservationStore (DEC-68(b)) — the stage
    logs ONE WARNING (never dream_derive's ERROR posture) and completes cleanly."""
    user_facts, upsert_calls = _fake_user_facts(monkeypatch)
    stage = DreamObserveStage(observations=None, user_facts=user_facts, tz=_TZ)

    with caplog.at_level(logging.WARNING, logger="wombat.behavior.stages.dream_observe"):
        result = await stage.run(StageContextFake(now_fn=lambda: _NOW))

    assert isinstance(result, Transition)
    assert result.to == "dream_behavior_log"
    assert result.output.data == {"new_facts": 0}
    assert upsert_calls == []
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert not any(r.levelno == logging.ERROR for r in caplog.records)


async def test_ac3_raising_get_window_for_both_channels_is_caught_loud_and_still_transitions(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    observations, _calls = _fake_observations(
        monkeypatch,
        screen_raises=RuntimeError("simulated screen get_window failure — AC3"),
        mic_raises=RuntimeError("simulated mic get_window failure — AC3"),
    )
    user_facts, upsert_calls = _fake_user_facts(monkeypatch)
    stage = DreamObserveStage(observations=observations, user_facts=user_facts, tz=_TZ)

    with caplog.at_level(logging.ERROR, logger="wombat.behavior.stages.dream_observe"):
        result = await stage.run(StageContextFake(now_fn=lambda: _NOW))

    assert isinstance(result, Transition)
    assert result.to == "dream_behavior_log"
    assert result.output.data == {"new_facts": 0}
    assert upsert_calls == []
    error_messages = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert any("'screen'" in m for m in error_messages)
    assert any("'mic'" in m for m in error_messages)


async def test_ac3_none_user_facts_store_skips_all_writes_loud_and_still_transitions(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    observations, _calls = _fake_observations(monkeypatch, screen_rows=_qualifying_screen_seed())
    stage = DreamObserveStage(observations=observations, user_facts=None, tz=_TZ)

    with caplog.at_level(logging.ERROR, logger="wombat.behavior.stages.dream_observe"):
        result = await stage.run(StageContextFake(now_fn=lambda: _NOW))

    assert isinstance(result, Transition)
    assert result.to == "dream_behavior_log"
    assert result.output.data == {"new_facts": 0}
    assert any(r.levelno == logging.ERROR for r in caplog.records)


async def test_ac3_raising_upsert_fact_mid_batch_loses_only_that_one_fact(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    observations, _calls = _fake_observations(monkeypatch, screen_rows=_qualifying_screen_seed())
    user_facts, upsert_calls = _fake_user_facts(monkeypatch, raises_upsert_on_call=1)
    stage = DreamObserveStage(observations=observations, user_facts=user_facts, tz=_TZ)

    with caplog.at_level(logging.ERROR, logger="wombat.behavior.stages.dream_observe"):
        result = await stage.run(StageContextFake(now_fn=lambda: _NOW))

    assert isinstance(result, Transition)
    assert result.to == "dream_behavior_log"
    # Two candidates total (arrival + residency); the first upsert call raised, the second still
    # landed — new_facts counts only the SUCCESSFUL upsert.
    assert result.output.data == {"new_facts": 1}
    assert len(upsert_calls) == 2
    assert any(r.levelno == logging.ERROR for r in caplog.records)


# ================================================================================================
# AC4: over-cap seed stops at _MAX_OBSERVE_FACTS deterministically, loudly logged
# ================================================================================================


async def test_ac4_over_cap_seed_stops_at_the_pinned_cap_deterministically(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # In-call segments on ALL SEVEN days of two consecutive ISO weeks — 7 qualifying call facts,
    # over the 5-per-pass cap.
    mic_rows: list[dict[str, Any]] = []
    for monday in (_MONDAY_W1, _MONDAY_W2):
        for day_offset in range(7):
            start = monday + timedelta(days=day_offset, hours=15)
            mic_rows.append(_mic_row(start, start + timedelta(minutes=15)))

    observations, _calls = _fake_observations(monkeypatch, mic_rows=mic_rows)
    user_facts, upsert_calls = _fake_user_facts(monkeypatch)
    stage = DreamObserveStage(observations=observations, user_facts=user_facts, tz=_TZ)

    with caplog.at_level(logging.WARNING, logger="wombat.behavior.stages.dream_observe"):
        result = await stage.run(StageContextFake(now_fn=lambda: _NOW))

    assert isinstance(result, Transition)
    assert result.output.data == {"new_facts": _MAX_OBSERVE_FACTS}
    assert len(upsert_calls) == _MAX_OBSERVE_FACTS

    # Deterministic: re-running derivation directly yields the SAME 5 keys, in the SAME order.
    landed_keys = [key for key, _fact, _source in upsert_calls]
    all_qualifying = _derive_call_facts(mic_rows)
    assert len(all_qualifying) == 7
    assert landed_keys == [key for key, _fact in all_qualifying[:_MAX_OBSERVE_FACTS]]

    warning_messages = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("cap" in m for m in warning_messages)
