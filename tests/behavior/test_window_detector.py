"""TK-112 — detect_productivity_windows acceptance criteria (EP-21, Q-99c).

PURE, no I/O, no DSN anywhere in this module — ``detect_productivity_windows`` is a plain function
over a ``Sequence[BehaviorEventRow]`` fixture.

  AC1 (partition + summary shape): a 14-day-spanning fixture covering a focused block, a context
      switch, and cross-day gaps -> the returned ``WindowSummary`` list never overlaps, every
      input event lands in EXACTLY ONE window (the partition invariant — the covered-time sum of
      member ``duration_seconds``, ``None`` treated as ``0.0``, equals the same sum over the full
      corpus), and each summary's ``event_count``/``switch_rate``/``outcome_mix`` match a
      hand-computed expectation. No motive field exists on ``WindowSummary`` (dataclass field
      check).
  AC3 (empty input): ``detect_productivity_windows(())`` returns ``[]``, never an error.
  AC4 (NG-3, structural): an AST identifier scan over ``window_detector.py`` finds no
      render/surface/dashboard-implying identifier anywhere.
"""

from __future__ import annotations

import ast
import inspect
from dataclasses import fields
from datetime import UTC, datetime, timedelta
from itertools import pairwise

from wombat.behavior.event_log import BehaviorEventRow
from wombat.behavior.window_detector import (
    WINDOW_GAP_SECONDS,
    WindowSummary,
    detect_productivity_windows,
    window_summary_to_dict,
)

# Tokens that would imply a surfacing/visualization concern has crept into this pure detector
# (NG-3). Checked against every identifier this module defines/uses, not raw text — so the
# module's own docstrings can document the exclusion (which necessarily names the forbidden
# concepts) without tripping the guard on their own explanatory prose.
_SURFACE_TOKENS = ("render", "surface", "dashboard")


def _row(
    *,
    key: str,
    event_type: str,
    timestamp_utc: datetime,
    outcome_label: str,
    duration_seconds: float | None = None,
) -> BehaviorEventRow:
    return BehaviorEventRow(
        idempotency_key=key,
        event_type=event_type,
        source_id="test-source",
        timestamp_utc=timestamp_utc,
        outcome_label=outcome_label,
        duration_seconds=duration_seconds,
    )


def _partition_by_window(
    events: list[BehaviorEventRow], windows: list[WindowSummary]
) -> list[list[BehaviorEventRow]]:
    """Black-box slice of ``events`` into per-window groups using each summary's inclusive
    ``[start_utc, end_utc]`` range — valid because non-overlapping windows make the slice
    unambiguous (Q-99c)."""
    return [
        [event for event in events if window.start_utc <= event.timestamp_utc <= window.end_utc]
        for window in windows
    ]


# --------------------------------------------------------------------------------------- AC1


def test_ac1_partition_covers_every_event_with_no_overlap_and_correct_summaries() -> None:
    day1 = datetime(2026, 6, 25, 9, 0, 0, tzinfo=UTC)

    # Window 1: a focused block with one context switch (three events, gaps <= 30 min).
    e1 = _row(
        key="e1",
        event_type="draft_reply",
        timestamp_utc=day1,
        outcome_label="outcome_load_bearing",
        duration_seconds=300.0,
    )
    e2 = _row(
        key="e2",
        event_type="draft_reply",
        timestamp_utc=day1 + timedelta(minutes=15),
        outcome_label="outcome_load_bearing",
        duration_seconds=200.0,
    )
    e3 = _row(
        key="e3",
        event_type="calendar_conflict",
        timestamp_utc=day1 + timedelta(minutes=40),
        outcome_label="outcome_ignored",
        duration_seconds=None,
    )

    # Window 2: a single-event window, > 30 min after e3.
    e4 = _row(
        key="e4",
        event_type="morning_brief",
        timestamp_utc=day1 + timedelta(hours=3),
        outcome_label="outcome_regretted",
        duration_seconds=50.0,
    )

    # Window 3: a two-event, no-switch window on a different day (>= 7 days later).
    day2 = day1 + timedelta(days=13)
    e5 = _row(
        key="e5",
        event_type="draft_reply",
        timestamp_utc=day2,
        outcome_label="outcome_load_bearing",
        duration_seconds=120.0,
    )
    e6 = _row(
        key="e6",
        event_type="draft_reply",
        timestamp_utc=day2 + timedelta(minutes=10),
        outcome_label="outcome_ignored",
        duration_seconds=None,
    )

    events = [e6, e1, e4, e3, e5, e2]  # deliberately out of chronological order

    windows = detect_productivity_windows(events)

    assert len(windows) == 3

    # No motive field — the closed WindowSummary field set.
    field_names = {f.name for f in fields(WindowSummary)}
    assert field_names == {"start_utc", "end_utc", "event_count", "switch_rate", "outcome_mix"}

    # Non-overlapping, chronologically ordered.
    for previous, current in pairwise(windows):
        assert previous.end_utc < current.start_utc

    # Partition invariant: every event lands in exactly one window.
    groups = _partition_by_window(events, windows)
    assert sum(len(group) for group in groups) == len(events)
    covered_keys = {event.idempotency_key for group in groups for event in group}
    assert covered_keys == {event.idempotency_key for event in events}

    # Covered-time invariant (AC1): sum of member durations (None -> 0.0) == corpus total.
    corpus_total = sum(event.duration_seconds or 0.0 for event in events)
    windows_total = sum(
        sum(event.duration_seconds or 0.0 for event in group) for group in groups
    )
    assert windows_total == corpus_total

    window_1, window_2, window_3 = windows
    assert window_1.event_count == 3
    assert window_1.switch_rate == 0.5  # one switch (e2 -> e3) out of two consecutive pairs
    assert window_1.outcome_mix == {"outcome_load_bearing": 2, "outcome_ignored": 1}
    assert window_1.start_utc == e1.timestamp_utc
    assert window_1.end_utc == e3.timestamp_utc

    assert window_2.event_count == 1
    assert window_2.switch_rate == 0.0  # single-event window
    assert window_2.outcome_mix == {"outcome_regretted": 1}

    assert window_3.event_count == 2
    assert window_3.switch_rate == 0.0  # same event_type both times
    assert window_3.outcome_mix == {"outcome_load_bearing": 1, "outcome_ignored": 1}


def test_ac1_gap_exactly_at_threshold_stays_in_the_same_window() -> None:
    """The split rule is a STRICT '>' — a gap exactly equal to WINDOW_GAP_SECONDS never splits."""
    start = datetime(2026, 6, 25, 9, 0, 0, tzinfo=UTC)
    e1 = _row(key="a", event_type="x", timestamp_utc=start, outcome_label="outcome_ignored")
    e2 = _row(
        key="b",
        event_type="x",
        timestamp_utc=start + timedelta(seconds=WINDOW_GAP_SECONDS),
        outcome_label="outcome_ignored",
    )

    windows = detect_productivity_windows([e1, e2])

    assert len(windows) == 1
    assert windows[0].event_count == 2


def test_ac1_gap_one_second_over_threshold_splits() -> None:
    start = datetime(2026, 6, 25, 9, 0, 0, tzinfo=UTC)
    e1 = _row(key="a", event_type="x", timestamp_utc=start, outcome_label="outcome_ignored")
    e2 = _row(
        key="b",
        event_type="x",
        timestamp_utc=start + timedelta(seconds=WINDOW_GAP_SECONDS + 1),
        outcome_label="outcome_ignored",
    )

    windows = detect_productivity_windows([e1, e2])

    assert len(windows) == 2
    assert windows[0].event_count == 1
    assert windows[1].event_count == 1


def test_window_summary_to_dict_is_json_native() -> None:
    start = datetime(2026, 6, 25, 9, 0, 0, tzinfo=UTC)
    e1 = _row(
        key="a", event_type="x", timestamp_utc=start, outcome_label="outcome_ignored"
    )
    windows = detect_productivity_windows([e1])
    payload = window_summary_to_dict(windows[0])

    assert payload == {
        "start_utc": start.isoformat(),
        "end_utc": start.isoformat(),
        "event_count": 1,
        "switch_rate": 0.0,
        "outcome_mix": {"outcome_ignored": 1},
    }


# --------------------------------------------------------------------------------------- AC3


def test_ac3_empty_input_returns_empty_list() -> None:
    assert detect_productivity_windows([]) == []
    assert detect_productivity_windows(()) == []


# --------------------------------------------------------------------------------------- AC4


def test_ac4_no_dashboard_surface_render_identifier() -> None:
    """Structural (NG-3): scan every identifier this module defines/uses for a
    render/surface/dashboard-implying token."""
    import wombat.behavior.window_detector as detector_module

    tree = ast.parse(inspect.getsource(detector_module))

    identifiers: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            identifiers.add(node.name)
        elif isinstance(node, ast.Name):
            identifiers.add(node.id)
        elif isinstance(node, ast.arg):
            identifiers.add(node.arg)
        elif isinstance(node, ast.Attribute):
            identifiers.add(node.attr)

    for identifier in identifiers:
        for token in _SURFACE_TOKENS:
            assert token not in identifier.lower(), (
                f"identifier {identifier!r} contains surface-implying token {token!r}"
            )
