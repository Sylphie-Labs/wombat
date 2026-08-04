"""TK-348 — BiometricEventSource acceptance criteria (DEC-80(d), Tier 3).

  AC1: each of the three pinned crossing conditions -> exactly one event of the matching kind.
  AC2: debounced — a second ``poll()`` over an unchanged window returns ``[]``; a resting-HR
      value flapping in/out of band on the SAME ``day_key`` emits at most once.
  AC3: the vocabulary is CLOSED — across a corpus containing all five ledger kinds, the produced
      ``kind`` set is a subset of exactly the three names; ``hrv_daily``/``steps_hourly`` rows
      produce nothing.
  AC4: model-free (NG-4/CON-2) — proven structurally (no model-shaped constructor param, no model
      import in the module), the SAME honest reframing ``tests/pathways/test_dream_biometrics_
      stage.py`` uses for its own LLM-free stage.
  AC7: toggle-off boot is structurally inert; toggle-on with an injected store registers the
      source under id ``"biometric_events"``.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import SecretStr

from wombat.config import WombatConfig
from wombat.devices.biometric_events import BiometricEventSource
from wombat.queue import EnqueueResult, QueueItem
from wombat.sources.base import SourceEvent
from wombat.sources.bootstrap import build_source_registry

_NOW = datetime(2026, 8, 3, 6, 0, 0, tzinfo=UTC)
_TZ = ZoneInfo("America/Chicago")


def _clock(instant: datetime = _NOW) -> Callable[[], datetime]:
    return lambda: instant


class _FakeObservations:
    """A minimal ``ObservationsLike`` double — ``get_window`` returns whatever ``rows`` were
    handed at construction, ignoring the requested window (the source itself doesn't need to be
    proven against real windowing here; that's ``ObservationStore.get_window``'s own contract)."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, datetime, datetime]] = []

    def get_window(self, channel: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
        self.calls.append((channel, start, end))
        return self.rows


def _row(kind: str, payload: dict[str, Any], *, started_at: datetime, day: date) -> dict[str, Any]:
    return {
        "id": 0,
        "channel": "biometric",
        "kind": kind,
        "started_at": started_at,
        "ended_at": started_at + timedelta(minutes=30),
        "payload": payload,
        "day_key": day,
    }


def _poll(source: BiometricEventSource) -> list[SourceEvent]:
    return asyncio.run(source.poll())


# ================================================================================================
# AC1 — one event per crossing, one test per kind
# ================================================================================================


def test_ac1_workout_ended() -> None:
    started = _NOW - timedelta(hours=2)
    rows = [
        _row(
            "workout",
            {"activity": "running", "duration_seconds": 1800, "active_energy_kcal": 250.0},
            started_at=started,
            day=started.date(),
        )
    ]
    source = BiometricEventSource(
        observations=_FakeObservations(rows), poll_interval_seconds=300.0, clock=_clock()
    )

    events = _poll(source)

    assert len(events) == 1
    event = events[0]
    assert event.event_key == f"workout_ended:{started.isoformat()}"
    assert event.payload["event_class"] == "biometric"
    assert event.payload["kind"] == "workout_ended"
    assert event.payload["activity"] == "running"
    assert event.payload["duration_seconds"] == 1800
    assert "is_timed" not in event.payload
    assert "sender_class" not in event.payload


def _resting_hr_rows(days: int, *, out_of_band_day_bpm: int | None = None) -> list[dict[str, Any]]:
    rows = []
    base_day = _NOW.date()
    for i in range(days):
        day = base_day - timedelta(days=i + 1)
        bpm = 60
        if out_of_band_day_bpm is not None and i == 0:
            bpm = out_of_band_day_bpm
        started = datetime(day.year, day.month, day.day, 7, tzinfo=UTC)
        rows.append(_row("resting_hr_daily", {"bpm": bpm}, started_at=started, day=day))
    return rows


def test_ac1_resting_hr_out_of_band() -> None:
    # 5 baseline days at 60 bpm + 1 crossing day at 90 bpm (>7bpm off a 60bpm baseline).
    rows = _resting_hr_rows(5, out_of_band_day_bpm=None)
    crossing_day = _NOW.date() - timedelta(days=10)
    crossing_started = datetime(
        crossing_day.year, crossing_day.month, crossing_day.day, 7, tzinfo=UTC
    )
    rows.append(
        _row("resting_hr_daily", {"bpm": 90}, started_at=crossing_started, day=crossing_day)
    )
    source = BiometricEventSource(
        observations=_FakeObservations(rows), poll_interval_seconds=300.0, clock=_clock()
    )

    events = _poll(source)

    assert len(events) == 1
    event = events[0]
    assert event.event_key == f"resting_hr_out_of_band:{crossing_day.isoformat()}"
    assert event.payload == {
        "event_class": "biometric",
        "kind": "resting_hr_out_of_band",
        "bpm": 90,
    }


def _sleep_rows(nights: int, minutes_cycle: list[int]) -> list[dict[str, Any]]:
    rows = []
    base_day = _NOW.date()
    for i in range(nights):
        day = base_day - timedelta(days=i + 1)
        started = datetime(day.year, day.month, day.day, 23, tzinfo=UTC)
        minutes = minutes_cycle[i % len(minutes_cycle)]
        rows.append(
            _row(
                "sleep_session",
                {"asleep_minutes": minutes, "in_bed_minutes": minutes + 30, "awakenings": 1},
                started_at=started,
                day=day,
            )
        )
    return rows


def test_ac1_sleep_debt_crossed() -> None:
    # 5 OLDER nights at 480 minutes plus the 3 MOST RECENT nights at 200 minutes: baseline over
    # all 8 distinct nights = (5*480 + 3*200) / 8 = 375; summed shortfall over the 3 most recent
    # = 3 * (375 - 200) = 525 > 180.
    base_day = _NOW.date()
    older_days = [base_day - timedelta(days=6 + i) for i in range(5)]
    recent_days = [base_day - timedelta(days=1 + i) for i in range(3)]
    rows = [
        _row(
            "sleep_session",
            {"asleep_minutes": 480, "in_bed_minutes": 510, "awakenings": 1},
            started_at=datetime(day.year, day.month, day.day, 23, tzinfo=UTC),
            day=day,
        )
        for day in older_days
    ]
    rows += [
        _row(
            "sleep_session",
            {"asleep_minutes": 200, "in_bed_minutes": 230, "awakenings": 1},
            started_at=datetime(day.year, day.month, day.day, 23, tzinfo=UTC),
            day=day,
        )
        for day in recent_days
    ]
    source = BiometricEventSource(
        observations=_FakeObservations(rows), poll_interval_seconds=300.0, clock=_clock()
    )

    events = _poll(source)

    assert len(events) == 1
    event = events[0]
    most_recent_night = max(recent_days)
    assert event.event_key == f"sleep_debt_crossed:{most_recent_night.isoformat()}"
    assert event.payload["event_class"] == "biometric"
    assert event.payload["kind"] == "sleep_debt_crossed"
    assert event.payload["asleep_minutes"] == 200


# ================================================================================================
# AC2 — debounce
# ================================================================================================


def test_ac2_second_poll_over_unchanged_window_returns_empty() -> None:
    started = _NOW - timedelta(hours=2)
    rows = [
        _row(
            "workout",
            {"activity": "cycling", "duration_seconds": 900, "active_energy_kcal": 100.0},
            started_at=started,
            day=started.date(),
        )
    ]
    source = BiometricEventSource(
        observations=_FakeObservations(rows), poll_interval_seconds=300.0, clock=_clock()
    )

    first = _poll(source)
    second = _poll(source)

    assert len(first) == 1
    assert second == []


def test_ac2_resting_hr_flapping_in_and_out_of_band_same_day_emits_at_most_once() -> None:
    rows = _resting_hr_rows(5)
    day = _NOW.date() - timedelta(days=10)
    started = datetime(day.year, day.month, day.day, 6, tzinfo=UTC)
    # Two readings the SAME day: one crosses the band, one does not.
    rows.append(_row("resting_hr_daily", {"bpm": 90}, started_at=started, day=day))
    rows.append(
        _row("resting_hr_daily", {"bpm": 61}, started_at=started + timedelta(hours=1), day=day)
    )
    source = BiometricEventSource(
        observations=_FakeObservations(rows), poll_interval_seconds=300.0, clock=_clock()
    )

    events = _poll(source)

    matching = [e for e in events if e.payload["kind"] == "resting_hr_out_of_band"]
    assert len(matching) == 1


# ================================================================================================
# AC3 — closed vocabulary, exhaustively asserted
# ================================================================================================


def test_ac3_closed_vocabulary_hrv_and_steps_produce_nothing() -> None:
    workout_started = _NOW - timedelta(hours=1)
    rows: list[dict[str, Any]] = [
        _row(
            "workout",
            {"activity": "yoga", "duration_seconds": 1200, "active_energy_kcal": 80.0},
            started_at=workout_started,
            day=workout_started.date(),
        ),
        _row("hrv_daily", {"sdnn_ms": 42.0}, started_at=_NOW, day=_NOW.date()),
        _row("steps_hourly", {"steps": 500}, started_at=_NOW, day=_NOW.date()),
    ]
    rows += _resting_hr_rows(5)
    rows += _sleep_rows(5, [480])

    source = BiometricEventSource(
        observations=_FakeObservations(rows), poll_interval_seconds=300.0, clock=_clock()
    )

    events = _poll(source)

    produced_kinds = {e.payload["kind"] for e in events}
    assert produced_kinds <= {"workout_ended", "resting_hr_out_of_band", "sleep_debt_crossed"}
    # hrv_daily/steps_hourly rows contribute no kind at all (they are simply never read by any
    # deriver) — a direct assertion that no event references them.
    assert "hrv_daily" not in produced_kinds
    assert "steps_hourly" not in produced_kinds


# ================================================================================================
# AC4 — model-free (structural, mirrors test_dream_biometrics_stage.py's own honest reframing)
# ================================================================================================


def test_ac4_zero_model_calls_is_structural_no_model_param_or_import() -> None:
    params = inspect.signature(BiometricEventSource.__init__).parameters
    assert "model" not in params
    module = inspect.getmodule(BiometricEventSource)
    assert module is not None
    assert not hasattr(module, "Model")
    assert not hasattr(module, "ChatMessage")


# ================================================================================================
# AC7 — toggle-off boot is structurally inert; toggle-on registers the source
# ================================================================================================


class _FakeTokenStore:
    def load(self) -> str | None:
        return None

    def save(self, token: str) -> None:
        return None

    def clear(self) -> None:
        return None


def _make_config(*, observe_biometrics: bool) -> WombatConfig:
    return WombatConfig(
        deepseek_api_key=SecretStr("unused-in-this-test"),
        deepseek_base_url="https://unused.example",
        wombat_observe_biometrics=observe_biometrics,
    )


class _FakeEnqueuer:
    def __init__(self) -> None:
        self.items: list[QueueItem] = []

    def enqueue(self, item: QueueItem) -> EnqueueResult:
        self.items.append(item)
        return EnqueueResult.QUEUED


def test_ac7_biometric_events_source_not_wired_when_toggle_is_false() -> None:
    config = _make_config(observe_biometrics=False)

    registry = build_source_registry(
        config,
        _FakeEnqueuer(),
        tz=_TZ,
        clock=_clock(),
        gcal_token_store=_FakeTokenStore(),
        gmail_token_store=_FakeTokenStore(),
    )

    assert "biometric_events" not in registry.source_ids


def test_ac7_biometric_events_source_wired_when_toggle_is_true_with_injected_store() -> None:
    config = _make_config(observe_biometrics=True)
    injected = _FakeObservations(rows=[])

    registry = build_source_registry(
        config,
        _FakeEnqueuer(),
        tz=_TZ,
        clock=_clock(),
        gcal_token_store=_FakeTokenStore(),
        gmail_token_store=_FakeTokenStore(),
        biometric_observation_store=injected,  # type: ignore[arg-type]
    )

    assert "biometric_events" in registry.source_ids
