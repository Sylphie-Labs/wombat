"""Production laptop-presence domain types + source (TK-11, hardened from the TK-4 spike).

Q-54 HOMES: this module owns the presence VOCABULARY (``PresenceState``,
``PresenceSnapshot``), the impure OS idle reader (``read_idle_ms``), the pure
classifier (``classify``), and the Q-49 PROVIDER factory (``make_presence_provider``).
The pure canonical HOLD predicate that TK-6/TK-27 call does NOT live here — it lives in
``wombat.gate.presence_hold`` (a gate concern, not a source concern) and imports these
types.

Design (deterministic, model-free — NG-4), carried over from the TK-4 spike and hardened:
  * ``read_idle_ms()`` does the only impure thing: it asks user32 how long since the last
    keyboard/mouse input. On any failure (non-Windows, missing API, syscall error) it
    returns ``None`` instead of raising — the degrade path (AC3).
  * ``classify(idle_ms, taken_at, ...)`` is a PURE function: given an idle reading and a
    timestamp it returns a ``PresenceSnapshot``. The active/idle boundary is INJECTED
    (``idle_threshold_s``, no baked default — TK-13 owns the value); the idle/away boundary
    is the module-level ``AWAY_THRESHOLD_S`` constant, which is DESCRIPTIVE ONLY (journal/UX)
    and deliberately NOT a TK-13 param, since IDLE and AWAY hold identically in
    ``presence_hold``.
  * ``make_presence_provider(...)`` is the Q-49 PROVIDER: each call reads a fresh idle
    signal, stamps ``taken_at`` from the injected clock, classifies it, and applies the
    staleness ceiling AT PROVISION time — a stale snapshot is degraded to
    ``(UNKNOWN, confidence=0.0)`` before it ever reaches the gate (Layer 1 defense). The
    canonical ``presence_hold`` predicate keeps its own staleness check as Layer 2,
    defense-in-depth, for the case a provider bug lets a stale snapshot through.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

# AWAY_THRESHOLD_S: a MODULE CONSTANT, DESCRIPTIVE ONLY (journal/UX labeling of "how long since
# the user was last seen"). It is NOT behavior-bearing — IDLE and AWAY hold IDENTICALLY in
# presence_hold — so it is deliberately NOT a TK-13 injected param (Q-54).
AWAY_THRESHOLD_S: float = 1800.0


class PresenceState(Enum):
    """The closed presence vocabulary. A single idle signal distinguishes all four values:
    ``unknown`` (signal unavailable), ``active`` (recent input), ``idle`` (no recent input,
    below the away threshold), ``away`` (idle for a very long time — descriptive only)."""

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

    def is_stale(self, now: float, staleness_ceiling_s: float) -> bool:
        """A snapshot older than the staleness ceiling can no longer be trusted (=> HOLD)."""
        return self.age_seconds(now) > staleness_ceiling_s


def read_idle_ms() -> int | None:
    """IMPURE: milliseconds since last keyboard/mouse input, or ``None`` on failure.

    Uses Windows ``user32.GetLastInputInfo`` + ``kernel32.GetTickCount`` via ctypes.
    Returns ``None`` (NEVER raises) if not on Windows or the syscall path fails — that
    ``None`` flows through ``classify`` into an ``unknown`` snapshot (AC3).
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


def classify(
    idle_ms: int | None,
    taken_at: float,
    *,
    idle_threshold_s: float,
) -> PresenceSnapshot:
    """PURE: turn a raw idle reading into a ``PresenceSnapshot``.

    ``idle_ms is None`` => the OS signal was unavailable => ``UNKNOWN`` / confidence 0.0
    (the degrade path). Otherwise confidence is 1.0 and:
      * ``idle_s < idle_threshold_s`` => ``ACTIVE``
      * ``idle_s >= AWAY_THRESHOLD_S`` => ``AWAY``
      * otherwise => ``IDLE``

    Staleness is NOT decided here — it is a function of ``now`` at gate-check time, owned
    by ``wombat.gate.presence_hold``.
    """
    if idle_ms is None:
        return PresenceSnapshot(
            state=PresenceState.UNKNOWN, confidence=0.0, idle_ms=None, taken_at=taken_at
        )
    idle_s = idle_ms / 1000.0
    if idle_s < idle_threshold_s:
        state = PresenceState.ACTIVE
    elif idle_s >= AWAY_THRESHOLD_S:
        state = PresenceState.AWAY
    else:
        state = PresenceState.IDLE
    return PresenceSnapshot(state=state, confidence=1.0, idle_ms=idle_ms, taken_at=taken_at)


def make_presence_provider(
    *,
    clock: Callable[[], float],
    staleness_ceiling_s: float,
    idle_threshold_s: float,
) -> Callable[[], PresenceSnapshot]:
    """Build the Q-49 PROVIDER: an impure callable the gate composes as ``presence_provider``.

    Each call reads ``read_idle_ms()``, stamps ``taken_at = clock()``, classifies the
    reading, and applies the staleness ceiling AT PROVISION (Layer 1 defense): if the
    resulting snapshot is already stale relative to ``clock()``, it is degraded to
    ``PresenceSnapshot(UNKNOWN, confidence=0.0, idle_ms=idle_ms, taken_at=taken_at)`` before
    it is ever handed to the gate. For a fresh read ``taken_at`` is (by construction)
    approximately ``now``, so this degrade is belt-and-suspenders here — it matters when a
    provider is composed with a clock that can lag or when future providers cache readings.

    ``read_idle_ms()`` is documented to never raise, but the provider degrades to
    ``UNKNOWN``/confidence 0.0 even if it somehow did (belt-and-suspenders, mirroring the
    idle-read's own None-on-failure contract) — the provider itself must never raise to the
    gate (AC3).
    """

    def provider() -> PresenceSnapshot:
        taken_at = clock()
        try:
            idle_ms = read_idle_ms()
        except Exception:  # the provider must never raise to the gate (AC3)
            idle_ms = None
        snapshot = classify(idle_ms, taken_at, idle_threshold_s=idle_threshold_s)
        if snapshot.is_stale(taken_at, staleness_ceiling_s):
            return PresenceSnapshot(
                state=PresenceState.UNKNOWN,
                confidence=0.0,
                idle_ms=snapshot.idle_ms,
                taken_at=taken_at,
            )
        return snapshot

    return provider


__all__ = [
    "AWAY_THRESHOLD_S",
    "PresenceSnapshot",
    "PresenceState",
    "classify",
    "make_presence_provider",
    "read_idle_ms",
]
