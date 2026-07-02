"""Tests for the TK-4 presence probe spike (RISK-3).

The classifier is exercised with INJECTED idle-millisecond values so the
active/idle/stale boundaries are pinned deterministically; one smoke test
takes a single REAL OS reading and only asserts non-negativity (the >90%
transition accuracy is live-gated to Jim's laptop, not asserted here).
"""

from __future__ import annotations

from wombat.presence.probe import (
    ACTIVE_IDLE_THRESHOLD_S,
    STALENESS_CEILING_S,
    PresenceSnapshot,
    PresenceState,
    classify,
    presence_hold,
    read_idle_ms,
)

T0 = 1_000_000.0  # arbitrary fixed epoch "taken_at" for deterministic tests


# --- classify: active / idle boundary (60s threshold) ---


def test_classify_active_just_below_threshold() -> None:
    snap = classify(idle_ms=59_999, taken_at=T0)
    assert snap.state is PresenceState.ACTIVE
    assert snap.confidence == 1.0
    assert snap.idle_ms == 59_999


def test_classify_idle_exactly_at_threshold() -> None:
    # 60_000 ms == 60s; threshold is inclusive of idle (>= => idle).
    snap = classify(idle_ms=60_000, taken_at=T0)
    assert snap.state is PresenceState.IDLE


def test_classify_idle_above_threshold() -> None:
    snap = classify(idle_ms=120_000, taken_at=T0)
    assert snap.state is PresenceState.IDLE


def test_classify_zero_idle_is_active() -> None:
    assert classify(idle_ms=0, taken_at=T0).state is PresenceState.ACTIVE


def test_threshold_constant_is_60s() -> None:
    assert ACTIVE_IDLE_THRESHOLD_S == 60.0


# --- classify: degrade path (idle signal unavailable) ---


def test_classify_none_is_unknown_zero_confidence() -> None:
    snap = classify(idle_ms=None, taken_at=T0)
    assert snap.state is PresenceState.UNKNOWN
    assert snap.confidence == 0.0
    assert snap.idle_ms is None


# --- staleness boundary (5 min) ---


def test_snapshot_fresh_within_ceiling_not_stale() -> None:
    snap = classify(idle_ms=0, taken_at=T0)
    assert snap.is_stale(now=T0 + STALENESS_CEILING_S) is False  # exactly 5 min: still fresh


def test_snapshot_just_past_ceiling_is_stale() -> None:
    snap = classify(idle_ms=0, taken_at=T0)
    assert snap.is_stale(now=T0 + STALENESS_CEILING_S + 0.001) is True


def test_age_seconds_never_negative_for_future_clock_skew() -> None:
    snap = classify(idle_ms=0, taken_at=T0)
    assert snap.age_seconds(now=T0 - 10.0) == 0.0


# --- presence_hold: the load-bearing conservative default ---


def test_hold_false_only_for_fresh_confident_active() -> None:
    snap = classify(idle_ms=1_000, taken_at=T0)  # active, fresh, confident
    assert presence_hold(snap, now=T0 + 1.0) is False


def test_hold_true_for_idle_even_when_fresh() -> None:
    snap = classify(idle_ms=90_000, taken_at=T0)  # idle
    assert presence_hold(snap, now=T0 + 1.0) is True


def test_hold_true_for_unknown() -> None:
    snap = classify(idle_ms=None, taken_at=T0)  # unknown / confidence 0.0
    assert presence_hold(snap, now=T0 + 1.0) is True


def test_hold_true_for_stale_active() -> None:
    # An ACTIVE reading that has gone stale must STILL hold (conservative-on-stale).
    snap = classify(idle_ms=1_000, taken_at=T0)
    now = T0 + STALENESS_CEILING_S + 60.0
    assert snap.state is PresenceState.ACTIVE
    assert presence_hold(snap, now=now) is True


def test_hold_true_for_low_confidence_active() -> None:
    # Hand-built ACTIVE snapshot below the confidence floor must hold.
    snap = PresenceSnapshot(
        state=PresenceState.ACTIVE, confidence=0.4, idle_ms=1_000, taken_at=T0
    )
    assert presence_hold(snap, now=T0 + 1.0, confidence_floor=0.5) is True


def test_hold_true_for_away() -> None:
    snap = PresenceSnapshot(
        state=PresenceState.AWAY, confidence=1.0, idle_ms=600_000, taken_at=T0
    )
    assert presence_hold(snap, now=T0 + 1.0) is True


def test_hold_sweep_no_unsafe_input_permits_surface() -> None:
    """Property-ish sweep: across the idle/staleness/confidence space, the ONLY
    surface-permitting input is fresh + confident + active. Mirrors the guarantee
    TK-11 will harden — any stale/unknown/idle/low-confidence input must hold."""
    surfaced_inputs = []
    for idle_ms in (0, 30_000, 59_999, 60_000, 120_000, None):
        for age in (0.0, 1.0, STALENESS_CEILING_S, STALENESS_CEILING_S + 1.0):
            snap = classify(idle_ms=idle_ms, taken_at=T0)
            if not presence_hold(snap, now=T0 + age):
                surfaced_inputs.append((idle_ms, age))
    # Every permitted surface must be a fresh, active reading.
    for idle_ms, age in surfaced_inputs:
        assert idle_ms is not None and idle_ms < 60_000
        assert age <= STALENESS_CEILING_S
    assert surfaced_inputs, "expected at least one fresh-active input to be surfaceable"


# --- smoke: ONE real OS reading is non-negative (or cleanly None on non-Windows) ---


def test_real_idle_read_is_non_negative_or_none() -> None:
    idle = read_idle_ms()
    assert idle is None or idle >= 0
