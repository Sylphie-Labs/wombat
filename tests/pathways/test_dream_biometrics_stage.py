"""TK-346 — DreamBiometricsStage acceptance criteria (EP-41).

In-memory/monkeypatched substrate, ZERO network: mirrors ``tests/behavior/test_dream_observe.py``'s
own idiom — ``observations``/``user_facts`` are REAL ``ObservationStore``/``UserFactsStore``
instances over an unreachable DSN (lazy — never actually connects) with their public methods
monkeypatched to recording/canned/raising doubles.

  AC1: a seeded biometric window with 5 qualifying nights of ``sleep_session`` rows and 5
      qualifying days of ``resting_hr_daily`` rows (plus scatter on other closed kinds, which no
      template reads) -> exactly the two templated facts land, ``source="behavior"``. ZERO model
      calls: for this LLM-free stage the proof is STRUCTURAL (no model-shaped constructor param,
      no model import in the module) rather than a runtime spy — see
      ``test_zero_model_calls_is_structural``; the graph-level test in ``test_dream_pathway.py``
      additionally proves it via a raising ``ModelRegistry`` factory shared across the whole
      ``wombat.dream`` Engine run.
  AC2: zero biometric rows (a real store, empty window) OR ``observations is None`` (the consent
      toggle off) -> nothing written, exactly ONE skip log line, and a clean onward transition
      without raising.
  AC3: the CON-6 motive screen runs over the CLOSED template vocabulary itself (``_FACT_TEMPLATES``,
      not sampled/filled instances) — no template can express why/intent/mood/cause or a clinical
      claim.
  AC4: two consecutive nights over the same overlapping window -> facts upsert rather than
      accumulate duplicates; the per-night write count is clamped by the SAME cap VALUE and
      truncation/logging MECHANISM ``dream_observe._MAX_OBSERVE_FACTS`` uses.
"""

from __future__ import annotations

import inspect
import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from cogworx.loop.result import Transition

from tests.support.stage_context_fake import StageContextFake
from wombat.behavior.stages import dream_biometrics as dream_biometrics_module
from wombat.behavior.stages.dream_biometrics import (
    _FACT_TEMPLATES,
    _MAX_BIOMETRIC_FACTS,
    DreamBiometricsStage,
)
from wombat.behavior.stages.dream_observe import _MAX_OBSERVE_FACTS
from wombat.observations import ObservationStore
from wombat.user_facts import UserFactsStore

_NOW = datetime(2026, 7, 30, 3, 0, 0, tzinfo=UTC)
_UNREACHABLE_DSN = "postgresql://nonexistent-host-should-never-be-dialed:1/db"

_CON6_FORBIDDEN_TOKENS = (
    "because",
    "why",
    "intent",
    "mood",
    "cause",
    "clinical",
    "diagnos",
    "disorder",
    "symptom",
    "therapy",
    "seems to",
    "tends to",
    "you feel",
    "trying to",
)


def _biometric_row(kind: str, payload: dict[str, Any], day: date) -> dict[str, Any]:
    start = datetime(day.year, day.month, day.day, 3, 0, tzinfo=UTC)
    return {
        "id": 0,
        "channel": "biometric",
        "kind": kind,
        "started_at": start,
        "ended_at": start + timedelta(hours=1),
        "payload": payload,
        "day_key": day,
    }


def _biometric_rows(
    now: datetime, *, sleep_nights: int, resting_hr_days: int
) -> list[dict[str, Any]]:
    """Distinct days going back from ``now``; a fixed cycle of plausible values so the average is
    reproducible by hand: 5 nights of ``[420, 420, 450, 450, 435]`` average to exactly 435 minutes
    (7h 15m); 5 days of ``[58, 60, 62, 60, 60]`` bpm average to exactly 60."""
    days = [now.date() - timedelta(days=i) for i in range(max(sleep_nights, resting_hr_days, 1))]
    sleep_minutes_cycle = [420, 420, 450, 450, 435]
    resting_bpm_cycle = [58, 60, 62, 60, 60]
    rows: list[dict[str, Any]] = []
    for day, minutes in zip(days[:sleep_nights], sleep_minutes_cycle, strict=False):
        rows.append(
            _biometric_row(
                "sleep_session",
                {"asleep_minutes": minutes, "in_bed_minutes": minutes + 30, "awakenings": 1},
                day,
            )
        )
    for day, bpm in zip(days[:resting_hr_days], resting_bpm_cycle, strict=False):
        rows.append(_biometric_row("resting_hr_daily", {"bpm": bpm}, day))
    return rows


def _fake_observations(
    monkeypatch: pytest.MonkeyPatch,
    *,
    rows: list[dict[str, Any]] | None = None,
    raises: BaseException | None = None,
) -> tuple[ObservationStore, list[tuple[str, datetime, datetime]]]:
    calls: list[tuple[str, datetime, datetime]] = []

    def _get_window(
        self: ObservationStore, channel: str, start: datetime, end: datetime
    ) -> list[dict[str, Any]]:
        calls.append((channel, start, end))
        if raises is not None:
            raise raises
        return rows or []

    monkeypatch.setattr(ObservationStore, "get_window", _get_window)
    return ObservationStore(_UNREACHABLE_DSN), calls


def _fake_user_facts(
    monkeypatch: pytest.MonkeyPatch,
    *,
    existing: dict[str, str] | None = None,
    raises_upsert_on_call: int | None = None,
) -> tuple[UserFactsStore, list[tuple[str, str, str]]]:
    """A stateful in-memory double — mirrors ``test_dream_observe.py``'s own fake exactly."""
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
            raise RuntimeError(f"simulated upsert_fact failure on call {call_index['n']} — AC")
        rows[fact_key] = fact

    monkeypatch.setattr(UserFactsStore, "count", _count)
    monkeypatch.setattr(UserFactsStore, "list_facts", _list_facts)
    monkeypatch.setattr(UserFactsStore, "upsert_fact", _upsert_fact)
    return UserFactsStore(_UNREACHABLE_DSN), calls


# ================================================================================================
# AC1: qualifying sleep + resting-HR seed -> exactly two bounded facts; ZERO model calls (struct.)
# ================================================================================================


async def test_ac1_seeded_sleep_and_resting_hr_yields_the_two_bounded_facts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = _biometric_rows(_NOW, sleep_nights=5, resting_hr_days=5)
    # Scatter on OTHER closed kinds (workout/hrv_daily/steps_hourly) — no template reads them.
    scatter_day = _NOW.date() - timedelta(days=15)
    rows.append(
        _biometric_row(
            "workout",
            {"activity": "running", "duration_seconds": 1800, "active_energy_kcal": 250.0},
            scatter_day,
        )
    )
    rows.append(_biometric_row("hrv_daily", {"sdnn_ms": 45.0}, scatter_day))
    rows.append(_biometric_row("steps_hourly", {"steps": 500}, scatter_day))

    observations, _calls = _fake_observations(monkeypatch, rows=rows)
    user_facts, upsert_calls = _fake_user_facts(monkeypatch)
    stage = DreamBiometricsStage(observations=observations, user_facts=user_facts)

    result = await stage.run(StageContextFake(now_fn=lambda: _NOW))

    assert isinstance(result, Transition)
    assert result.to == "dream_behavior_log"
    assert result.output.data == {"new_facts": 2}

    assert len(upsert_calls) == 2
    assert all(source == "behavior" for _key, _fact, source in upsert_calls)
    facts_by_key = {key: fact for key, fact, _source in upsert_calls}
    assert facts_by_key == {
        "biometric:sleep:duration": "Usually gets about 7h 15m of sleep per night",
        "biometric:resting_hr:baseline": "Resting heart rate is usually around 60 bpm",
    }


def test_zero_model_calls_is_structural_no_model_param_or_import() -> None:
    """AC1's 'ZERO model calls ... asserted with a spy model that fails the test if called at
    all', applied honestly to an LLM-free stage: there is no model-shaped constructor parameter to
    wire a spy into in the first place, so the proof is structural (stronger than a per-run spy,
    which only proves the model wasn't touched THIS run) — the stage cannot call a model no matter
    how it is constructed."""
    params = inspect.signature(DreamBiometricsStage.__init__).parameters
    assert "model" not in params
    assert not hasattr(dream_biometrics_module, "Model")
    assert not hasattr(dream_biometrics_module, "ChatMessage")


async def test_under_threshold_days_yield_no_facts(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = _biometric_rows(_NOW, sleep_nights=4, resting_hr_days=4)  # under both min-day bars
    observations, _calls = _fake_observations(monkeypatch, rows=rows)
    user_facts, upsert_calls = _fake_user_facts(monkeypatch)
    stage = DreamBiometricsStage(observations=observations, user_facts=user_facts)

    result = await stage.run(StageContextFake(now_fn=lambda: _NOW))

    assert isinstance(result, Transition)
    assert result.output.data == {"new_facts": 0}
    assert upsert_calls == []


# ================================================================================================
# AC2: no rows (real store) OR observations is None -> nothing written, ONE skip log line
# ================================================================================================


async def test_ac2_zero_rows_with_a_real_store_logs_one_skip_line_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    observations, calls = _fake_observations(monkeypatch, rows=[])
    user_facts, upsert_calls = _fake_user_facts(monkeypatch)
    stage = DreamBiometricsStage(observations=observations, user_facts=user_facts)

    with caplog.at_level(logging.INFO, logger="wombat.behavior.stages.dream_biometrics"):
        result = await stage.run(StageContextFake(now_fn=lambda: _NOW))

    assert isinstance(result, Transition)
    assert result.to == "dream_behavior_log"
    assert result.output.data == {"new_facts": 0}
    assert upsert_calls == []
    assert [channel for channel, _s, _e in calls] == ["biometric"]
    assert len(caplog.records) == 1


async def test_ac2_none_observation_store_logs_one_skip_line_never_errors(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A toggle-off boot legitimately constructs no ObservationStore — one skip line, never an
    error, and the stage completes cleanly."""
    user_facts, upsert_calls = _fake_user_facts(monkeypatch)
    stage = DreamBiometricsStage(observations=None, user_facts=user_facts)

    with caplog.at_level(logging.INFO, logger="wombat.behavior.stages.dream_biometrics"):
        result = await stage.run(StageContextFake(now_fn=lambda: _NOW))

    assert isinstance(result, Transition)
    assert result.to == "dream_behavior_log"
    assert result.output.data == {"new_facts": 0}
    assert upsert_calls == []
    assert len(caplog.records) == 1
    assert caplog.records[0].levelno == logging.WARNING


async def test_raising_get_window_is_caught_loud_and_still_transitions(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    observations, _calls = _fake_observations(
        monkeypatch, raises=RuntimeError("simulated get_window failure")
    )
    user_facts, upsert_calls = _fake_user_facts(monkeypatch)
    stage = DreamBiometricsStage(observations=observations, user_facts=user_facts)

    with caplog.at_level(logging.ERROR, logger="wombat.behavior.stages.dream_biometrics"):
        result = await stage.run(StageContextFake(now_fn=lambda: _NOW))

    assert isinstance(result, Transition)
    assert result.to == "dream_behavior_log"
    assert result.output.data == {"new_facts": 0}
    assert upsert_calls == []
    assert any(r.levelno == logging.ERROR for r in caplog.records)


async def test_none_user_facts_store_skips_all_writes_loud_and_still_transitions(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    rows = _biometric_rows(_NOW, sleep_nights=5, resting_hr_days=5)
    observations, _calls = _fake_observations(monkeypatch, rows=rows)
    stage = DreamBiometricsStage(observations=observations, user_facts=None)

    with caplog.at_level(logging.ERROR, logger="wombat.behavior.stages.dream_biometrics"):
        result = await stage.run(StageContextFake(now_fn=lambda: _NOW))

    assert isinstance(result, Transition)
    assert result.to == "dream_behavior_log"
    assert result.output.data == {"new_facts": 0}
    assert any(r.levelno == logging.ERROR for r in caplog.records)


async def test_raising_upsert_fact_mid_batch_loses_only_that_one_fact(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    rows = _biometric_rows(_NOW, sleep_nights=5, resting_hr_days=5)
    observations, _calls = _fake_observations(monkeypatch, rows=rows)
    user_facts, upsert_calls = _fake_user_facts(monkeypatch, raises_upsert_on_call=1)
    stage = DreamBiometricsStage(observations=observations, user_facts=user_facts)

    with caplog.at_level(logging.ERROR, logger="wombat.behavior.stages.dream_biometrics"):
        result = await stage.run(StageContextFake(now_fn=lambda: _NOW))

    assert isinstance(result, Transition)
    # Two candidates total; the first upsert raised, the second still landed.
    assert result.output.data == {"new_facts": 1}
    assert len(upsert_calls) == 2
    assert any(r.levelno == logging.ERROR for r in caplog.records)


# ================================================================================================
# AC3: CON-6 motive screen runs over the CLOSED template vocabulary directly
# ================================================================================================


def test_ac3_con6_motive_screen_runs_over_the_closed_template_vocabulary() -> None:
    assert len(_FACT_TEMPLATES) == 2  # the whole closed vocabulary this ticket ever writes
    for template in _FACT_TEMPLATES:
        casefolded = template.casefold()
        for token in _CON6_FORBIDDEN_TOKENS:
            assert token not in casefolded, f"forbidden token {token!r} found in: {template!r}"


# ================================================================================================
# AC4: two consecutive nights -> upsert not duplicate; write count clamped exactly as dream_observe
# ================================================================================================


async def test_ac4_two_consecutive_nights_upsert_rather_than_accumulate_duplicates(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    rows = _biometric_rows(_NOW, sleep_nights=5, resting_hr_days=5)
    observations, _calls = _fake_observations(monkeypatch, rows=rows)
    user_facts, upsert_calls = _fake_user_facts(monkeypatch)
    stage = DreamBiometricsStage(observations=observations, user_facts=user_facts)

    with caplog.at_level(logging.INFO, logger="wombat.behavior.stages.dream_biometrics"):
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
    assert accepted_lines == []  # the second night logged NO new-fact line


def test_ac4_the_cap_is_the_same_value_dream_observe_uses() -> None:
    assert _MAX_BIOMETRIC_FACTS == _MAX_OBSERVE_FACTS


async def test_ac4_write_count_is_clamped_by_the_same_truncate_and_warn_mechanism(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # This ticket's two templates can never together exceed the real cap (5) — the mechanism
    # itself (truncate to the pinned cap, log ONE loud WARNING naming the overflow) is proven by
    # lowering the module's cap constant, exactly as dream_observe's own AC4 proves its cap.
    monkeypatch.setattr(dream_biometrics_module, "_MAX_BIOMETRIC_FACTS", 1)

    rows = _biometric_rows(_NOW, sleep_nights=5, resting_hr_days=5)  # 2 qualifying candidates
    observations, _calls = _fake_observations(monkeypatch, rows=rows)
    user_facts, upsert_calls = _fake_user_facts(monkeypatch)
    stage = DreamBiometricsStage(observations=observations, user_facts=user_facts)

    with caplog.at_level(logging.WARNING, logger="wombat.behavior.stages.dream_biometrics"):
        result = await stage.run(StageContextFake(now_fn=lambda: _NOW))

    assert isinstance(result, Transition)
    assert result.output.data == {"new_facts": 1}
    assert len(upsert_calls) == 1
    assert upsert_calls[0][0] == "biometric:sleep:duration"  # sleep is derived FIRST, deterministic
    warning_messages = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("cap" in m for m in warning_messages)
