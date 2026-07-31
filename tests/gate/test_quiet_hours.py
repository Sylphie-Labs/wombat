"""TK-304 (DEC-67g) — the canonical, load-bearing ``in_quiet_hours`` predicate.

PURE tests (AC1): a midnight-spanning window (22:00-07:00) checked at the four pinned
timestamps, plus the "start==end or either empty never holds" guard.
"""

from __future__ import annotations

from datetime import time

import pytest

from wombat.gate.quiet_hours import in_quiet_hours

_START = "22:00"
_END = "07:00"


@pytest.mark.parametrize(
    ("now", "expected_hold"),
    [
        (time(23, 30), True),
        (time(3, 0), True),
        (time(8, 0), False),
        (time(21, 59), False),
    ],
)
def test_ac1_midnight_spanning_window(now: time, expected_hold: bool) -> None:
    assert in_quiet_hours(now, _START, _END) is expected_hold


def test_ac1_start_equals_end_never_holds() -> None:
    assert in_quiet_hours(time(23, 0), "10:00", "10:00") is False


def test_ac1_blank_start_never_holds() -> None:
    assert in_quiet_hours(time(23, 30), "", _END) is False


def test_ac1_blank_end_never_holds() -> None:
    assert in_quiet_hours(time(23, 30), _START, "") is False


def test_ac1_both_blank_never_holds() -> None:
    assert in_quiet_hours(time(23, 30), "", "") is False


# --- a non-wrapping (same-day) window is also supported --------------------------------------


def test_non_wrapping_window_inside_holds() -> None:
    assert in_quiet_hours(time(13, 0), "12:00", "14:00") is True


def test_non_wrapping_window_start_inclusive() -> None:
    assert in_quiet_hours(time(12, 0), "12:00", "14:00") is True


def test_non_wrapping_window_end_exclusive() -> None:
    assert in_quiet_hours(time(14, 0), "12:00", "14:00") is False


def test_non_wrapping_window_outside_never_holds() -> None:
    assert in_quiet_hours(time(15, 0), "12:00", "14:00") is False


def test_midnight_spanning_start_inclusive() -> None:
    assert in_quiet_hours(time(22, 0), _START, _END) is True


def test_midnight_spanning_end_exclusive() -> None:
    assert in_quiet_hours(time(7, 0), _START, _END) is False
