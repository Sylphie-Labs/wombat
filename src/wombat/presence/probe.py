"""SPIKE (TK-4, RISK-3) — single-signal laptop presence probe.

THROWAWAY prototype. It answers one POC question: can a single OS idle-time API
(Windows ``GetLastInputInfo``) distinguish active vs idle for *conservative* gate
conditioning, and does a stale snapshot reliably force the gate to HOLD?

Design (deterministic, model-free — NG-4):
  * ``read_idle_ms()`` does the only impure thing: it asks user32 how long since
    the last keyboard/mouse input. On any failure (non-Windows, missing API,
    syscall error) it returns ``None`` instead of raising — the degrade path.
  * ``classify(idle_ms, taken_at, now, ...)`` is a PURE function: given an idle
    reading and timestamps it returns a ``PresenceSnapshot``. All boundaries
    (active/idle threshold, staleness ceiling) are injected and unit-tested.
  * ``presence_hold(snapshot, now, ...)`` is a PURE predicate: the load-bearing
    conservative default. It returns ``True`` (HOLD / do-not-interrupt) for
    unknown OR stale snapshots, and only permits a surface for a fresh, ACTIVE
    reading. TK-11 later hardens this same contract to production.

The "> 90% of observed transitions on a real machine" half of the hypothesis is
LIVE-GATED: it can only be confirmed by Jim observing real active/idle/away
transitions on his own laptop during real use. The code here makes that
observation possible and proves the classifier/HOLD logic deterministically.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

# Spike defaults — the thresholds the POC names. Injectable so tests pin boundaries.
ACTIVE_IDLE_THRESHOLD_S: float = 60.0  # < 60s idle => active; >= 60s => idle (hypothesis)
STALENESS_CEILING_S: float = 300.0  # snapshot older than 5 min => STALE => gate HOLDs


class PresenceState(Enum):
    """The single closed presence vocabulary for this spike.

    ``away`` is reserved for the multi-signal future (DEC-6, a non_goal here); a
    single idle signal only ever distinguishes ``active`` / ``idle`` / ``unknown``.
    """

    ACTIVE = "active"
    IDLE = "idle"
    AWAY = "away"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class PresenceSnapshot:
    """A single presence reading. ``taken_at`` is epoch seconds (the clock the gate compares)."""

    state: PresenceState
    confidence: float  # 0.0 when the signal is unavailable; ~1.0 for a clean idle read
    idle_ms: int | None  # raw OS idle milliseconds; None when the read failed
    taken_at: float

    def age_seconds(self, now: float) -> float:
        """How stale this snapshot is relative to ``now`` (never negative)."""
        return max(0.0, now - self.taken_at)

    def is_stale(self, now: float, staleness_ceiling_s: float = STALENESS_CEILING_S) -> bool:
        """A snapshot older than the staleness ceiling can no longer be trusted (=> HOLD)."""
        return self.age_seconds(now) > staleness_ceiling_s


def classify(
    idle_ms: int | None,
    taken_at: float,
    *,
    active_threshold_s: float = ACTIVE_IDLE_THRESHOLD_S,
) -> PresenceSnapshot:
    """PURE: turn a raw idle reading into a ``PresenceSnapshot``.

    ``idle_ms is None`` => the OS signal was unavailable => ``unknown`` /
    confidence 0.0 (the degrade path; AC2). Otherwise sub-threshold idle is
    ``active``, at-or-above is ``idle``. Staleness is NOT decided here — it is a
    function of ``now`` at gate-check time, so ``presence_hold`` owns it.
    """
    if idle_ms is None:
        return PresenceSnapshot(
            state=PresenceState.UNKNOWN, confidence=0.0, idle_ms=None, taken_at=taken_at
        )
    idle_s = idle_ms / 1000.0
    state = PresenceState.ACTIVE if idle_s < active_threshold_s else PresenceState.IDLE
    return PresenceSnapshot(state=state, confidence=1.0, idle_ms=idle_ms, taken_at=taken_at)


def presence_hold(
    snapshot: PresenceSnapshot,
    now: float,
    *,
    staleness_ceiling_s: float = STALENESS_CEILING_S,
    confidence_floor: float = 0.5,
) -> bool:
    """PURE, load-bearing: return ``True`` (HOLD / do-not-interrupt) unless it is safe to surface.

    Conservative-on-everything-but-a-clean-active-read. The ONLY input that does
    NOT hold is a fresh (within the staleness ceiling), confident, ACTIVE
    snapshot. Unknown, stale, low-confidence, idle, or away all HOLD. This is the
    guarantee TK-11 hardens to production; the prototype already enforces it.
    """
    if snapshot.state is PresenceState.UNKNOWN:
        return True
    if snapshot.confidence < confidence_floor:
        return True
    if snapshot.is_stale(now, staleness_ceiling_s):
        return True
    # Only an actively-present user may be (potentially) surfaced to. idle/away => HOLD.
    return snapshot.state is not PresenceState.ACTIVE


def read_idle_ms() -> int | None:
    """IMPURE smoke read: milliseconds since last keyboard/mouse input, or ``None`` on failure.

    Uses Windows ``user32.GetLastInputInfo`` + ``kernel32.GetTickCount`` via ctypes.
    Returns ``None`` (never raises) if not on Windows or the syscall path fails —
    that ``None`` flows through ``classify`` into an ``unknown`` snapshot (AC2).
    """
    try:
        import ctypes
        from ctypes import wintypes
    except (ImportError, ValueError):  # pragma: no cover - non-Windows degrade
        return None

    class _LastInputInfo(ctypes.Structure):
        _fields_ = (("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD))

    try:
        # ``windll`` only exists on Windows; getattr keeps this typed/importable
        # everywhere, with a clean None degrade off-Windows.
        windll = getattr(ctypes, "windll", None)
        if windll is None:
            return None
        user32 = windll.user32
        kernel32 = windll.kernel32
        info = _LastInputInfo()
        info.cbSize = ctypes.sizeof(_LastInputInfo)
        if not user32.GetLastInputInfo(ctypes.byref(info)):
            return None
        # GetTickCount and dwTime are both 32-bit ms tick counts; the difference is
        # idle time. Mask to 32 bits so wraparound stays non-negative.
        now_ticks = kernel32.GetTickCount() & 0xFFFFFFFF
        idle = (now_ticks - info.dwTime) & 0xFFFFFFFF
        return int(idle)
    except (AttributeError, OSError):  # pragma: no cover - syscall/platform failure degrade
        return None
