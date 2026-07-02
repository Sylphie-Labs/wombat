"""TK-11 — the production presence source (types, classify, read_idle_ms, provider; Q-54).

Ported/hardened from the deleted TK-4 spike (``tests/presence/test_probe.py``): the
classifier's active/idle/away boundaries, the None-degrade path, staleness/age helpers on
``PresenceSnapshot``, and (AC3) the provider's degrade-on-unavailable/erroring idle read.
"""

from __future__ import annotations

import pytest

from wombat.sources.presence import (
    AWAY_THRESHOLD_S,
    PresenceSnapshot,
    PresenceState,
    classify,
    make_presence_provider,
    read_idle_ms,
)

T0 = 1_000_000.0  # arbitrary fixed epoch "taken_at" for deterministic tests
_IDLE_THRESHOLD_S = 60.0
_STALENESS_CEILING_S = 300.0


# --- classify: active / idle boundary ---------------------------------------------------


def test_classify_active_just_below_threshold() -> None:
    snap = classify(idle_ms=59_999, taken_at=T0, idle_threshold_s=_IDLE_THRESHOLD_S)
    assert snap.state is PresenceState.ACTIVE
    assert snap.confidence == 1.0
    assert snap.idle_ms == 59_999


def test_classify_idle_exactly_at_threshold() -> None:
    # 60_000 ms == 60s; threshold is inclusive of idle (>= => idle).
    snap = classify(idle_ms=60_000, taken_at=T0, idle_threshold_s=_IDLE_THRESHOLD_S)
    assert snap.state is PresenceState.IDLE


def test_classify_idle_above_threshold() -> None:
    snap = classify(idle_ms=120_000, taken_at=T0, idle_threshold_s=_IDLE_THRESHOLD_S)
    assert snap.state is PresenceState.IDLE


def test_classify_zero_idle_is_active() -> None:
    snap = classify(idle_ms=0, taken_at=T0, idle_threshold_s=_IDLE_THRESHOLD_S)
    assert snap.state is PresenceState.ACTIVE


# --- classify: away boundary (AWAY_THRESHOLD_S, descriptive-only per Q-54) --------------


def test_away_threshold_constant_is_1800s() -> None:
    assert AWAY_THRESHOLD_S == 1800.0


def test_classify_just_below_away_threshold_is_idle() -> None:
    idle_ms = int((AWAY_THRESHOLD_S - 0.001) * 1000)
    snap = classify(idle_ms=idle_ms, taken_at=T0, idle_threshold_s=_IDLE_THRESHOLD_S)
    assert snap.state is PresenceState.IDLE


def test_classify_at_away_threshold_is_away() -> None:
    idle_ms = int(AWAY_THRESHOLD_S * 1000)
    snap = classify(idle_ms=idle_ms, taken_at=T0, idle_threshold_s=_IDLE_THRESHOLD_S)
    assert snap.state is PresenceState.AWAY


def test_classify_well_above_away_threshold_is_away() -> None:
    snap = classify(idle_ms=6_000_000, taken_at=T0, idle_threshold_s=_IDLE_THRESHOLD_S)
    assert snap.state is PresenceState.AWAY


# --- classify: degrade path (idle signal unavailable) -----------------------------------


def test_classify_none_is_unknown_zero_confidence() -> None:
    snap = classify(idle_ms=None, taken_at=T0, idle_threshold_s=_IDLE_THRESHOLD_S)
    assert snap.state is PresenceState.UNKNOWN
    assert snap.confidence == 0.0
    assert snap.idle_ms is None


# --- PresenceSnapshot.age_seconds / is_stale ---------------------------------------------


def test_snapshot_fresh_within_ceiling_not_stale() -> None:
    snap = classify(idle_ms=0, taken_at=T0, idle_threshold_s=_IDLE_THRESHOLD_S)
    assert (
        snap.is_stale(now=T0 + _STALENESS_CEILING_S, staleness_ceiling_s=_STALENESS_CEILING_S)
        is False
    )


def test_snapshot_just_past_ceiling_is_stale() -> None:
    snap = classify(idle_ms=0, taken_at=T0, idle_threshold_s=_IDLE_THRESHOLD_S)
    now = T0 + _STALENESS_CEILING_S + 0.001
    assert snap.is_stale(now=now, staleness_ceiling_s=_STALENESS_CEILING_S) is True


def test_age_seconds_never_negative_for_future_clock_skew() -> None:
    snap = classify(idle_ms=0, taken_at=T0, idle_threshold_s=_IDLE_THRESHOLD_S)
    assert snap.age_seconds(now=T0 - 10.0) == 0.0


# --- read_idle_ms: smoke — one real OS reading is non-negative or cleanly None ----------


def test_real_idle_read_is_non_negative_or_none() -> None:
    idle = read_idle_ms()
    assert idle is None or idle >= 0


# --- AC3: make_presence_provider degrades None/raising idle reads to UNKNOWN ------------


def test_provider_degrades_none_idle_read_to_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    """A read_idle_ms() that returns None yields UNKNOWN/confidence 0.0, never raises (AC3)."""
    monkeypatch.setattr("wombat.sources.presence.read_idle_ms", lambda: None)
    provider = make_presence_provider(
        clock=lambda: T0,
        staleness_ceiling_s=_STALENESS_CEILING_S,
        idle_threshold_s=_IDLE_THRESHOLD_S,
    )

    snap = provider()

    assert snap.state is PresenceState.UNKNOWN
    assert snap.confidence == 0.0
    assert snap.taken_at == T0


def test_provider_degrades_raising_idle_read_without_propagating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If ``read_idle_ms`` somehow raises (it is documented never to, but the provider treats
    that as belt-and-suspenders defense, AC3), the provider degrades to UNKNOWN/confidence 0.0
    and does NOT propagate the exception to the gate."""

    def _raising_read() -> int | None:
        raise OSError("simulated syscall failure")

    monkeypatch.setattr("wombat.sources.presence.read_idle_ms", _raising_read)
    provider = make_presence_provider(
        clock=lambda: T0,
        staleness_ceiling_s=_STALENESS_CEILING_S,
        idle_threshold_s=_IDLE_THRESHOLD_S,
    )

    snap = provider()  # must not raise

    assert snap.state is PresenceState.UNKNOWN
    assert snap.confidence == 0.0


def test_provider_fresh_active_read_passes_through(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("wombat.sources.presence.read_idle_ms", lambda: 1_000)
    provider = make_presence_provider(
        clock=lambda: T0,
        staleness_ceiling_s=_STALENESS_CEILING_S,
        idle_threshold_s=_IDLE_THRESHOLD_S,
    )

    snap = provider()

    assert snap.state is PresenceState.ACTIVE
    assert snap.confidence == 1.0
    assert snap.idle_ms == 1_000
    assert snap.taken_at == T0


def test_provider_stamps_taken_at_from_injected_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("wombat.sources.presence.read_idle_ms", lambda: 0)
    calls = {"n": 0}

    def clock() -> float:
        calls["n"] += 1
        return T0 + calls["n"]

    provider = make_presence_provider(
        clock=clock, staleness_ceiling_s=_STALENESS_CEILING_S, idle_threshold_s=_IDLE_THRESHOLD_S
    )

    first = provider()
    second = provider()

    assert first.taken_at == T0 + 1
    assert second.taken_at == T0 + 2


def test_provider_boundary_ceiling_zero_age_still_passes_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh read's age relative to its own ``clock()`` stamp is exactly 0.0, which is NOT
    ``> 0.0`` — so even a zero-second staleness ceiling still lets a same-instant read through
    un-degraded (``is_stale`` is a strict ``>``)."""
    monkeypatch.setattr("wombat.sources.presence.read_idle_ms", lambda: 1_000)
    provider = make_presence_provider(
        clock=lambda: T0, staleness_ceiling_s=0.0, idle_threshold_s=_IDLE_THRESHOLD_S
    )

    snap = provider()

    assert snap.state is PresenceState.ACTIVE


def test_provider_degrade_at_provision_branch_fires(monkeypatch: pytest.MonkeyPatch) -> None:
    """Layer 1 defense (Q-49): the provider's own staleness-at-provision degrade branch fires
    and yields UNKNOWN/confidence 0.0 — proven by an unreachable-in-practice negative ceiling
    (age >= 0.0 is always > a negative ceiling), which exercises the exact same code path a
    real provider-clock-lag bug would trip."""
    monkeypatch.setattr("wombat.sources.presence.read_idle_ms", lambda: 1_000)
    provider_forced_stale = make_presence_provider(
        clock=lambda: T0, staleness_ceiling_s=-1.0, idle_threshold_s=_IDLE_THRESHOLD_S
    )

    degraded = provider_forced_stale()

    assert degraded.state is PresenceState.UNKNOWN
    assert degraded.confidence == 0.0


def test_presence_hold_holds_for_provider_degraded_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC3 end-to-end: a provider that degrades to UNKNOWN feeds presence_hold and it holds."""
    from wombat.gate.presence_hold import presence_hold

    monkeypatch.setattr("wombat.sources.presence.read_idle_ms", lambda: None)
    provider = make_presence_provider(
        clock=lambda: T0,
        staleness_ceiling_s=_STALENESS_CEILING_S,
        idle_threshold_s=_IDLE_THRESHOLD_S,
    )

    snap = provider()

    assert (
        presence_hold(snap, T0, staleness_ceiling_s=_STALENESS_CEILING_S, confidence_floor=0.5)
        is True
    )


def test_presence_snapshot_frozen_and_slotted() -> None:
    snap = PresenceSnapshot(state=PresenceState.ACTIVE, confidence=1.0, idle_ms=0, taken_at=T0)
    with pytest.raises(AttributeError):
        snap.state = PresenceState.IDLE  # type: ignore[misc]
