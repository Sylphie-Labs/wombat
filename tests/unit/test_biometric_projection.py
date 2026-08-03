"""TK-347 — Tier 2 acceptance criteria: ``project_current_body_state``
(``devices.biometric_projection``), the ONE bounded ``current_body_state`` line merged into
``bootstrap.py``'s SAME shared ``asr_context_hook`` closure (R7, DEC-68(d)(1) precedent).

  AC1 fresh biometric rows -> a single fixed-shape ``"<kind>: field=value ..."`` line, capped.
  AC2 no rows, a raising store, or ``observations=None`` (the consent toggle off) -> ``None``
      (absent, never an empty string) — see ``test_no_rows_returns_none`` /
      ``test_get_window_raising_degrades_to_none_with_one_warning`` /
      ``test_no_observations_store_returns_none``.
  AC3 (structural) the SAME asr_context_hook closure this key merges into is wired ONLY into
      ASRSource.context_hook / ChatSource.context_hook — never into the brief/draft/reflection
      compose stage builders — so this key can never reach a brief, a Gmail draft, or a reflection
      by construction (mirrors tests/unit/test_bootstrap.py's own AC3 pin for the other four keys).
  Fixed-shape (no free text): the rendered line is numeric ``field=value`` pairs only — a
  ``workout`` row's ``activity`` enum is never rendered.

Unit tests here use a REAL ``ObservationStore`` over an unreachable DSN with ``get_window``
monkeypatched (the ``tests/pathways/test_dream_biometrics_stage.py`` idiom exactly) — zero network,
zero Postgres. A separate pg-gated block at the bottom proves the same contract end-to-end against
a real throwaway Postgres, gated on ``WOMBAT_TEST_PG_DSN`` (the ``tests/behavior/test_event_log.py``
idiom):

    docker run --rm -d -p 5442:5432 -e POSTGRES_PASSWORD=wombat postgres:16
    WOMBAT_TEST_PG_DSN=postgresql://postgres:wombat@localhost:5442/postgres
"""

from __future__ import annotations

import inspect
import logging
import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest

from wombat import bootstrap
from wombat.devices import biometric_projection
from wombat.devices.biometric_projection import project_current_body_state
from wombat.observations import ObservationStore, ensure_schema

_NOW = datetime(2026, 8, 3, 12, 0, 0, tzinfo=UTC)
_UNREACHABLE_DSN = "postgresql://nonexistent-host-should-never-be-dialed:1/db"

_DSN = os.environ.get("WOMBAT_TEST_PG_DSN")

_requires_pg = pytest.mark.skipif(
    not _DSN,
    reason=(
        "WOMBAT_TEST_PG_DSN is not set — skipping biometric_projection DB tests that require a "
        "real throwaway Postgres. Start one with:\n"
        "  docker run --rm -d -p 5442:5432 -e POSTGRES_PASSWORD=wombat postgres:16\n"
        "then export WOMBAT_TEST_PG_DSN=postgresql://postgres:wombat@localhost:5442/postgres"
    ),
)


def _clock(instant: datetime = _NOW) -> Callable[[], datetime]:
    return lambda: instant


def _row(kind: str, payload: dict[str, Any], started_at: datetime) -> dict[str, Any]:
    return {
        "id": 0,
        "channel": "biometric",
        "kind": kind,
        "started_at": started_at,
        "ended_at": started_at + timedelta(minutes=1),
        "payload": payload,
        "day_key": started_at.date(),
    }


def _fake_observations(
    monkeypatch: pytest.MonkeyPatch,
    *,
    rows: list[dict[str, Any]] | None = None,
    raises: BaseException | None = None,
) -> tuple[ObservationStore, list[tuple[str, datetime, datetime]]]:
    """Mirrors tests/pathways/test_dream_biometrics_stage.py's own ``_fake_observations`` idiom
    exactly: a REAL ``ObservationStore`` over an unreachable DSN (never actually connects — lazy
    construction, Q-46) with ``get_window`` monkeypatched."""
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


# ================================================================================================
# AC2: absence — None store / no rows / a raising store
# ================================================================================================


def test_no_observations_store_returns_none() -> None:
    """The consent toggle off means ``biometric_observation_store`` is never constructed
    (bootstrap.py) — ``observations=None`` here mirrors that boot state exactly: absent, no read,
    no warning."""
    assert project_current_body_state(None, clock=_clock()) is None


def test_no_rows_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    observations, _calls = _fake_observations(monkeypatch, rows=[])
    assert project_current_body_state(observations, clock=_clock()) is None


def test_get_window_raising_degrades_to_none_with_one_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    observations, _calls = _fake_observations(monkeypatch, raises=RuntimeError("pg is down"))
    caplog.set_level(logging.WARNING, logger="wombat.devices.biometric_projection")

    result = project_current_body_state(observations, clock=_clock())

    assert result is None
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1


def test_get_window_queried_with_channel_biometric_and_the_pinned_freshness_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observations, calls = _fake_observations(monkeypatch, rows=[])
    project_current_body_state(observations, clock=_clock())

    assert calls == [("biometric", _NOW - biometric_projection._FRESHNESS_WINDOW, _NOW)]


# ================================================================================================
# AC1: fresh rows -> ONE fixed-shape line
# ================================================================================================


def test_freshest_row_wins_and_renders_a_fixed_shape_numeric_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleep_payload = {"asleep_minutes": 420, "in_bed_minutes": 450, "awakenings": 2}
    older = _row("sleep_session", sleep_payload, _NOW - timedelta(hours=5))
    freshest = _row("resting_hr_daily", {"bpm": 62}, _NOW - timedelta(minutes=10))
    observations, _calls = _fake_observations(monkeypatch, rows=[older, freshest])

    result = project_current_body_state(observations, clock=_clock())

    assert result == "resting_hr_daily: bpm=62"


def test_float_fields_render_to_one_decimal_place(monkeypatch: pytest.MonkeyPatch) -> None:
    row = _row("hrv_daily", {"sdnn_ms": 45.678}, _NOW - timedelta(minutes=5))
    observations, _calls = _fake_observations(monkeypatch, rows=[row])

    result = project_current_body_state(observations, clock=_clock())

    assert result == "hrv_daily: sdnn_ms=45.7"


def test_workout_activity_enum_is_never_rendered_only_numeric_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fixed-shape sequence of numbers, not free text (R7) — ``workout``'s ``activity`` string
    field is deliberately excluded from the rendered line."""
    row = _row(
        "workout",
        {"activity": "running", "duration_seconds": 1800, "active_energy_kcal": 250.0},
        _NOW - timedelta(minutes=5),
    )
    observations, _calls = _fake_observations(monkeypatch, rows=[row])

    result = project_current_body_state(observations, clock=_clock())

    assert result is not None
    assert "running" not in result
    assert "duration_seconds=1800" in result
    assert "active_energy_kcal=250.0" in result


def test_workout_omits_absent_nullable_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    row = _row(
        "workout",
        {"activity": "running", "duration_seconds": 1800, "active_energy_kcal": 250.0},
        _NOW - timedelta(minutes=5),
    )
    observations, _calls = _fake_observations(monkeypatch, rows=[row])

    result = project_current_body_state(observations, clock=_clock())

    assert result == "workout: duration_seconds=1800 active_energy_kcal=250.0"


def test_unrecognized_kind_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    row = _row("unknown_kind", {"bpm": 62}, _NOW - timedelta(minutes=5))
    observations, _calls = _fake_observations(monkeypatch, rows=[row])

    assert project_current_body_state(observations, clock=_clock()) is None


def test_recognized_kind_with_no_numeric_fields_present_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _row("resting_hr_daily", {}, _NOW - timedelta(minutes=5))
    observations, _calls = _fake_observations(monkeypatch, rows=[row])

    assert project_current_body_state(observations, clock=_clock()) is None


def test_line_truncated_at_the_pinned_char_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    row = _row("resting_hr_daily", {"bpm": 62}, _NOW - timedelta(minutes=5))
    observations, _calls = _fake_observations(monkeypatch, rows=[row])
    monkeypatch.setattr(biometric_projection, "_MAX_BODY_STATE_CHARS", 10)

    result = project_current_body_state(observations, clock=_clock())

    assert result == "resting_hr_daily: bpm=62"[:10]
    assert result is not None
    assert len(result) == 10


def test_pinned_char_cap_default_is_generous_enough_for_the_widest_kind() -> None:
    """Defensive sanity: the worst-case line (every ``workout`` numeric field populated at its
    widest plausible value) still fits comfortably under the pinned default cap."""
    worst_case = (
        "workout: duration_seconds=86400 active_energy_kcal=20000.0 avg_hr_bpm=250 "
        "max_hr_bpm=250 distance_meters=500000.0"
    )
    assert len(worst_case) <= biometric_projection._MAX_BODY_STATE_CHARS


# ================================================================================================
# AC3 (structural): the shared closure this key merges into never reaches brief/draft/reflection
# ================================================================================================


def test_ac3_current_body_state_seam_never_wired_into_brief_draft_or_reflection() -> None:
    """The asr_context_hook closure current_body_state merges into is wired ONLY into
    ASRSource.context_hook (via build_source_registry's context_hook kwarg) and
    ChatSource.context_hook — the brief/compose/draft-composer stage builders never accept a
    context_hook kwarg at all (mirrors tests/unit/test_bootstrap.py's own
    test_ac3_context_hook_seam_is_never_wired_into_gcal_or_brief_builders pin), so
    current_body_state can never reach a brief, a Gmail draft, or a reflection by construction."""
    assert "context_hook" not in inspect.signature(bootstrap.build_brief_compose_stage).parameters
    assert "context_hook" not in inspect.signature(bootstrap.build_compose_stage).parameters
    draft_params = inspect.signature(bootstrap.build_draft_composer_stage).parameters
    assert "context_hook" not in draft_params

    # The ONLY call site of project_current_body_state in the whole composition root lives
    # textually inside asr_context_hook's own body (between its def line and the post-definition
    # wiring that assigns it onto chat_source.context_hook, a handful of lines below).
    source = inspect.getsource(bootstrap)
    assert source.count("project_current_body_state(") == 1
    hook_def_idx = source.index("def asr_context_hook()")
    wiring_idx = source.index("chat_source.context_hook = asr_context_hook")
    call_idx = source.index("project_current_body_state(")
    assert hook_def_idx < call_idx < wiring_idx


# ================================================================================================
# pg-gated: end-to-end over a real throwaway Postgres
# ================================================================================================


@pytest.fixture
def clean_table() -> None:
    assert _DSN is not None
    with psycopg.connect(_DSN) as conn:
        ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE wombat_observations")
        conn.commit()


@_requires_pg
def test_pg_fresh_biometric_row_yields_current_body_state_line(clean_table: None) -> None:
    assert _DSN is not None
    store = ObservationStore(_DSN)
    started_at = _NOW - timedelta(minutes=30)
    store.append_segment(
        "biometric",
        "resting_hr_daily",
        started_at,
        started_at + timedelta(minutes=1),
        {"bpm": 58},
        started_at.date(),
    )

    result = project_current_body_state(store, clock=_clock())

    assert result == "resting_hr_daily: bpm=58"


@_requires_pg
def test_pg_only_stale_row_outside_freshness_window_yields_absence(clean_table: None) -> None:
    assert _DSN is not None
    store = ObservationStore(_DSN)
    stale_started_at = _NOW - biometric_projection._FRESHNESS_WINDOW - timedelta(hours=1)
    store.append_segment(
        "biometric",
        "resting_hr_daily",
        stale_started_at,
        stale_started_at + timedelta(minutes=1),
        {"bpm": 58},
        stale_started_at.date(),
    )

    result = project_current_body_state(store, clock=_clock())

    assert result is None
