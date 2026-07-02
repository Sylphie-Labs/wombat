"""TK-11 — the canonical, load-bearing ``presence_hold`` predicate (production, Q-54).

PURE property tests (AC1/AC2): sweep the (state x confidence x staleness) space and assert
the ONLY input that returns ``False`` (surface permitted) is fresh + confident + ACTIVE.
Every unknown / stale / low-confidence / non-active input must return ``True`` (hold). These
tests are ported/hardened from the TK-4 spike's ``tests/presence/test_probe.py`` (now
deleted, Q-54) plus new sweeps the production hardening adds.
"""

from __future__ import annotations

import pytest

from wombat.gate.presence_hold import presence_hold
from wombat.sources.presence import PresenceSnapshot, PresenceState

T0 = 1_000_000.0  # arbitrary fixed epoch "taken_at" for deterministic tests
_STALENESS_CEILING_S = 300.0
_CONFIDENCE_FLOOR = 0.5


def _snapshot(
    state: PresenceState, *, confidence: float = 1.0, idle_ms: int | None = 0, taken_at: float = T0
) -> PresenceSnapshot:
    return PresenceSnapshot(state=state, confidence=confidence, idle_ms=idle_ms, taken_at=taken_at)


# --- snapshot is None => hold ------------------------------------------------------------


def test_none_snapshot_holds() -> None:
    assert (
        presence_hold(
            None, T0, staleness_ceiling_s=_STALENESS_CEILING_S, confidence_floor=_CONFIDENCE_FLOOR
        )
        is True
    )


# --- the ONE permitted path: fresh + confident + ACTIVE => surface (False) --------------


def test_fresh_confident_active_permits_surface() -> None:
    snap = _snapshot(PresenceState.ACTIVE, confidence=1.0, taken_at=T0)
    assert (
        presence_hold(
            snap,
            T0 + 1.0,
            staleness_ceiling_s=_STALENESS_CEILING_S,
            confidence_floor=_CONFIDENCE_FLOOR,
        )
        is False
    )


def test_fresh_exactly_at_confidence_floor_permits_surface() -> None:
    """Confidence == floor is still permitted (only strictly BELOW the floor holds)."""
    snap = _snapshot(PresenceState.ACTIVE, confidence=_CONFIDENCE_FLOOR, taken_at=T0)
    assert (
        presence_hold(
            snap,
            T0 + 1.0,
            staleness_ceiling_s=_STALENESS_CEILING_S,
            confidence_floor=_CONFIDENCE_FLOOR,
        )
        is False
    )


def test_fresh_exactly_at_staleness_ceiling_permits_surface() -> None:
    """age == ceiling is still fresh (only strictly GREATER than the ceiling is stale)."""
    snap = _snapshot(PresenceState.ACTIVE, confidence=1.0, taken_at=T0)
    assert (
        presence_hold(
            snap,
            T0 + _STALENESS_CEILING_S,
            staleness_ceiling_s=_STALENESS_CEILING_S,
            confidence_floor=_CONFIDENCE_FLOOR,
        )
        is False
    )


# --- AC1: parametrized sweep over state x confidence x staleness -----------------------

_STATES = (PresenceState.ACTIVE, PresenceState.IDLE, PresenceState.AWAY, PresenceState.UNKNOWN)
_CONFIDENCES = (0.0, 0.2, 0.49, 0.5, 0.75, 1.0)
_AGES = (
    0.0,
    1.0,
    _STALENESS_CEILING_S,
    _STALENESS_CEILING_S + 0.001,
    _STALENESS_CEILING_S + 3600.0,
)


@pytest.mark.parametrize("state", _STATES)
@pytest.mark.parametrize("confidence", _CONFIDENCES)
@pytest.mark.parametrize("age", _AGES)
def test_hold_sweep_only_fresh_confident_active_surfaces(
    state: PresenceState, confidence: float, age: float
) -> None:
    """The load-bearing property (AC1): across the full sweep, ``presence_hold`` returns
    ``False`` (surface permitted) if and only if state is ACTIVE, confidence is at least the
    floor, and age is at most the staleness ceiling. Every other combination MUST hold —
    this test fails immediately if any stale/unknown/low-confidence/non-active input ever
    permits a surface."""
    snap = _snapshot(state, confidence=confidence, taken_at=T0)
    now = T0 + age

    result = presence_hold(
        snap, now, staleness_ceiling_s=_STALENESS_CEILING_S, confidence_floor=_CONFIDENCE_FLOOR
    )

    expected_surface = (
        state is PresenceState.ACTIVE
        and confidence >= _CONFIDENCE_FLOOR
        and age <= _STALENESS_CEILING_S
    )
    assert result is (not expected_surface), (
        f"presence_hold(state={state}, confidence={confidence}, age={age}) returned "
        f"{result}, expected hold={not expected_surface}"
    )


def test_hold_sweep_idle_and_away_hold_identically() -> None:
    """IDLE and AWAY must produce IDENTICAL hold outcomes across the sweep — AWAY_THRESHOLD_S
    is descriptive-only (journal/UX), never behavior-bearing (Q-54)."""
    for confidence in _CONFIDENCES:
        for age in _AGES:
            idle_snap = _snapshot(PresenceState.IDLE, confidence=confidence, taken_at=T0)
            away_snap = _snapshot(PresenceState.AWAY, confidence=confidence, taken_at=T0)
            now = T0 + age
            idle_result = presence_hold(
                idle_snap,
                now,
                staleness_ceiling_s=_STALENESS_CEILING_S,
                confidence_floor=_CONFIDENCE_FLOOR,
            )
            away_result = presence_hold(
                away_snap,
                now,
                staleness_ceiling_s=_STALENESS_CEILING_S,
                confidence_floor=_CONFIDENCE_FLOOR,
            )
            assert idle_result is True
            assert away_result is True
            assert idle_result == away_result


# --- AC2: a snapshot whose taken_at predates a multi-hour sleep gap must hold on wake ---


def test_ac2_multi_hour_sleep_gap_holds_on_wake() -> None:
    """A PresenceSnapshot taken before the host slept, evaluated with now = taken_at + a
    multi-hour gap (far beyond the staleness ceiling), must HOLD — the conservative-on-stale
    guarantee survives the sleep boundary (TK-28 resets the ledger; this predicate does not
    need sleep/wake DETECTION, staleness alone subsumes it)."""
    pre_sleep_snapshot = _snapshot(PresenceState.ACTIVE, confidence=1.0, taken_at=T0)
    multi_hour_gap_s = 8 * 3600.0  # host slept for 8 hours
    now_on_wake = T0 + multi_hour_gap_s

    assert multi_hour_gap_s > _STALENESS_CEILING_S
    assert (
        presence_hold(
            pre_sleep_snapshot,
            now_on_wake,
            staleness_ceiling_s=_STALENESS_CEILING_S,
            confidence_floor=_CONFIDENCE_FLOOR,
        )
        is True
    )


# --- explicit non-active states hold even when fresh + fully confident -----------------


def test_idle_fresh_confident_holds() -> None:
    snap = _snapshot(PresenceState.IDLE, confidence=1.0, taken_at=T0)
    assert (
        presence_hold(
            snap, T0, staleness_ceiling_s=_STALENESS_CEILING_S, confidence_floor=_CONFIDENCE_FLOOR
        )
        is True
    )


def test_away_fresh_confident_holds() -> None:
    snap = _snapshot(PresenceState.AWAY, confidence=1.0, taken_at=T0)
    assert (
        presence_hold(
            snap, T0, staleness_ceiling_s=_STALENESS_CEILING_S, confidence_floor=_CONFIDENCE_FLOOR
        )
        is True
    )


def test_unknown_fresh_confident_holds() -> None:
    """UNKNOWN holds even with a (nonsensical) confidence=1.0 — state check is independent."""
    snap = _snapshot(PresenceState.UNKNOWN, confidence=1.0, taken_at=T0)
    assert (
        presence_hold(
            snap, T0, staleness_ceiling_s=_STALENESS_CEILING_S, confidence_floor=_CONFIDENCE_FLOOR
        )
        is True
    )


def test_low_confidence_active_holds() -> None:
    snap = _snapshot(PresenceState.ACTIVE, confidence=0.4, taken_at=T0)
    assert (
        presence_hold(
            snap, T0, staleness_ceiling_s=_STALENESS_CEILING_S, confidence_floor=_CONFIDENCE_FLOOR
        )
        is True
    )


def test_stale_active_holds() -> None:
    """An ACTIVE reading that has gone stale must STILL hold (Layer-2 defense-in-depth)."""
    snap = _snapshot(PresenceState.ACTIVE, confidence=1.0, taken_at=T0)
    now = T0 + _STALENESS_CEILING_S + 60.0
    assert (
        presence_hold(
            snap, now, staleness_ceiling_s=_STALENESS_CEILING_S, confidence_floor=_CONFIDENCE_FLOOR
        )
        is True
    )
