"""wombat.behavior.window_detector — the PURE, off-path productivity-window detector (TK-112,
EP-21, Q-99c).

PARTITION, NOT CLUSTERING (Q-99c ruling): ``detect_productivity_windows`` sorts the input corpus
by ``timestamp_utc`` ascending, then splits it into maximal runs wherever the gap between two
chronologically adjacent events STRICTLY exceeds the module constant ``WINDOW_GAP_SECONDS`` (30
minutes) — a fixed structural definition of "one productivity window", never a tunable
``OperatingParams`` knob. Every input event belongs to EXACTLY ONE returned window, so the windows
never overlap and the sum, across every window, of its member events' ``duration_seconds``
(``None`` treated as ``0.0``) equals the same sum over the full input corpus.

PURE: no I/O, no clock read, no store read — the nightly ``WriteWindowSummariesStage``
(``wombat.behavior.stages.write_window_summaries``, Q-99e) is the sole caller, supplying the
corpus it reads off ``wombat.behavior.event_log.BehaviorEventLog``.

MOTIVE-FREE (CON-6/NG-1): ``WindowSummary`` carries only behavior aggregates — timestamps, an
event count, how often ``event_type`` changed between consecutive events (``switch_rate``), and a
per-``outcome_label`` count (``outcome_mix``). There is no motive/why field, and this module never
infers one.

NO DASHBOARD/SURFACE (NG-3): this module computes summaries only; it has no render/surface/
dashboard call anywhere (enforced by ``tests/behavior/test_window_detector.py``'s structural
scan).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from itertools import pairwise
from typing import Any

from wombat.behavior.event_log import BehaviorEventRow

# The maximal-run split threshold (Q-99c ruling): a module constant, not a tunable — whenever the
# gap between two chronologically adjacent events strictly exceeds 30 minutes, a new window
# begins.
WINDOW_GAP_SECONDS = 1800.0


@dataclass(frozen=True, slots=True)
class WindowSummary:
    """One detected productivity window — a PARTITION member over the input event corpus.

    NO motive field (CON-6/NG-1) — a pure behavior aggregate: ``start_utc``/``end_utc`` (the
    member events' earliest/latest ``timestamp_utc``), ``event_count``, ``switch_rate`` (the
    fraction of consecutive event pairs whose ``event_type`` differs; ``0.0`` for a single-event
    window), and ``outcome_mix`` (a count per ``outcome_label`` among the window's members).
    """

    start_utc: datetime
    end_utc: datetime
    event_count: int
    switch_rate: float
    outcome_mix: dict[str, int]


def detect_productivity_windows(events: Sequence[BehaviorEventRow]) -> list[WindowSummary]:
    """Partition ``events`` into maximal productivity windows and summarize each (Q-99c).

    Sorts by ``timestamp_utc`` ascending, then splits into a new window wherever the gap to the
    PREVIOUS (chronologically) event strictly exceeds ``WINDOW_GAP_SECONDS``. Every event lands in
    exactly one window (the partition invariant) — window time ranges never overlap.

    Empty input returns ``[]``, never an error.
    """
    if not events:
        return []

    ordered = sorted(events, key=lambda event: event.timestamp_utc)

    windows: list[list[BehaviorEventRow]] = [[ordered[0]]]
    for previous, current in pairwise(ordered):
        gap_seconds = (current.timestamp_utc - previous.timestamp_utc).total_seconds()
        if gap_seconds > WINDOW_GAP_SECONDS:
            windows.append([])
        windows[-1].append(current)

    return [_summarize(window) for window in windows]


def _summarize(window: Sequence[BehaviorEventRow]) -> WindowSummary:
    """Fold one maximal, chronologically-sorted run of events into its ``WindowSummary``."""
    event_count = len(window)

    switches = sum(
        1 for previous, current in pairwise(window) if previous.event_type != current.event_type
    )
    switch_rate = switches / max(event_count - 1, 1)

    outcome_mix: dict[str, int] = {}
    for event in window:
        outcome_mix[event.outcome_label] = outcome_mix.get(event.outcome_label, 0) + 1

    return WindowSummary(
        start_utc=window[0].timestamp_utc,
        end_utc=window[-1].timestamp_utc,
        event_count=event_count,
        switch_rate=switch_rate,
        outcome_mix=outcome_mix,
    )


def window_summary_to_dict(summary: WindowSummary) -> dict[str, Any]:
    """The JSON-native wire shape one ``WindowSummary`` serializes to (Q-49 convention) —
    ``WriteWindowSummariesStage`` writes a list of these as one claim's JSON-native value payload
    (TK-113 owns the read side)."""
    return {
        "start_utc": summary.start_utc.isoformat(),
        "end_utc": summary.end_utc.isoformat(),
        "event_count": summary.event_count,
        "switch_rate": summary.switch_rate,
        "outcome_mix": dict(summary.outcome_mix),
    }


__all__ = [
    "WINDOW_GAP_SECONDS",
    "WindowSummary",
    "detect_productivity_windows",
    "window_summary_to_dict",
]
