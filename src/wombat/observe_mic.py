"""observe_mic — the DEC-68(a)/(e) mic channel: zero-capture WASAPI in-call presence probe
(TK-313).

``probe_in_call()`` is the impure edge: enumerates audio CAPTURE sessions on the default capture
endpoint (the microphone device) via pycaw's session manager, looking for an ACTIVE session owned
by a process other than this one — the signal that the user currently appears to be on a call. NO
audio device is EVER opened here — this reads only the per-session state WASAPI's audio session
manager already exposes (DEC-68a structural: a test asserts this module's source names no
stream-opening API — see ``tests/unit/test_observations.py`` for the exact forbidden-token list).

pycaw (pulls comtypes) is imported LAZILY inside ``probe_in_call()`` (the DEC-68a dependency
ruling): non-Windows, ``ImportError``, or any COM raise degrades to ``False`` plus ONE WARNING —
reduce-only must never become silence-forever.

``MicInCallProbe`` mirrors ``observe_screen.ScreenActivityCollector``'s coalescing shape: a
true beat opens (or extends) a segment and flips ``CurrentActivity.in_call`` in place; a false
beat (or ``close()``) closes it and appends ONE ``channel='mic', kind='in_call', payload={}``
segment spanning the probe-true interval to ``ObservationStore`` (the DEC-68 ledger vocab,
RULED) — ``day_key`` is the tz-local civil date (DEC-21 ``wombat_today``) the segment opened on.
A store raise while appending is caught, logged once loudly, and the probe's state still resets
cleanly so later segments record normally.

``poll_once()``/``run()`` are the production polling surface (poll every ``_POLL_INTERVAL_S``
seconds); ``process_beat()`` is the pure(ish), directly-testable core the ACs drive without any
pycaw/asyncio involvement.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from datetime import date, datetime
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from .domain.daily_ledger import wombat_today
from .observations import CurrentActivity

logger = logging.getLogger(__name__)

# Pinned (DEC-63 no-knob precedent): NOT operator-tunable.
_POLL_INTERVAL_S = 10.0

# The DEC-68(a)/(c) ledger vocabulary for this channel.
_CHANNEL = "mic"
_KIND = "in_call"

# TK-378 (DEC-88, ISS-55): capture-session owners that NEVER mean "on a call". Matched on the
# executable basename, lowercased. These are always-on peripheral/vendor helpers that hold an
# ACTIVE session on the capture endpoint for their own reasons (hardware monitoring, sidetone,
# game audio) — before this list, one of them holding the microphone read as an in-call hold and
# suppressed EVERY surfacing arm indefinitely, so wombat went silent while a service it has
# nothing to do with happened to be running. Pinned (DEC-63 no-knob precedent), NOT operator-
# tunable: an ignored owner is skipped exactly the way an Inactive session already is, and an
# ACTIVE session owned by anything NOT listed here still reads as in-call — this narrows the
# probe, it never weakens it. When a new offender turns up, the in-call log line below names the
# owning process precisely so it can be added here without a debugger.
_IGNORED_CAPTURE_OWNERS: frozenset[str] = frozenset(
    {
        "steelseriescvgamesense.exe",
        "steelseriesengine.exe",
        "steelseriesgg.exe",
        "steelseriesggclient.exe",
    }
)

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

# TK-378 (ISS-56): the owner behind the CURRENT in-call reading, so the log line below fires once
# per transition (and once more if the owner changes) rather than on every 10s poll. Diagnostic
# state only — nothing reads it for a decision.
_last_in_call_owner: str | None = None


def _note_in_call_owner(owner: str | None) -> None:
    """Log ONE line naming the process behind a new in-call reading (ISS-56 — before this, an
    operator could see wombat go quiet with no way to tell WHICH process pinned the probe true,
    which is exactly how ISS-55 stayed invisible). Fires on a transition into in-call and on an
    owner change; silent while the same owner keeps holding the endpoint."""
    global _last_in_call_owner
    if owner == _last_in_call_owner:
        return
    if owner is not None:
        logger.info(
            "observe_mic: in-call reading TRUE — active capture session owned by %r "
            "(add it to _IGNORED_CAPTURE_OWNERS if it is a background service, never a call)",
            owner,
        )
    _last_in_call_owner = owner


def _capture_owner_name(pid: int) -> str | None:
    """The executable basename owning ``pid``, lowercased — or ``None`` if it can't be read.

    Uses the SAME ctypes pair ``observe_screen.read_foreground_window`` already relies on
    (``OpenProcess`` + ``QueryFullProcessImageNameW``, the ``sources.presence.read_idle_ms``
    precedent) — ZERO new dependencies. Never raises: an unreadable owner returns ``None``, and
    the caller then treats the session as NOT ignorable, which is the safe direction (an
    unidentifiable Active session still counts as in-call).
    """
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None
        try:
            name_buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(1024)
            ok = kernel32.QueryFullProcessImageNameW(handle, 0, name_buf, ctypes.byref(size))
            if not ok or not name_buf.value:
                return None
            return name_buf.value.rsplit("\\", 1)[-1].lower()
        finally:
            kernel32.CloseHandle(handle)
    except (AttributeError, OSError, ValueError):  # pragma: no cover - syscall/platform degrade
        return None


def probe_in_call() -> bool:
    """IMPURE: query the default capture endpoint's audio-session manager for an ACTIVE session
    owned by a process other than this one, EXCLUDING the ``_IGNORED_CAPTURE_OWNERS`` always-on
    background helpers (TK-378, ISS-55).

    Returns ``False`` (NEVER raises) on any failure — pycaw missing/non-Windows, no capture
    device, or any COM error along the way — logging exactly ONE WARNING per failure.
    """
    try:
        from pycaw.api.audiopolicy import IAudioSessionControl2
        from pycaw.constants import AudioSessionState
        from pycaw.utils import AudioUtilities
    except ImportError:
        logger.warning("observe_mic: pycaw unavailable — treating the in-call probe as False")
        return False

    try:
        device = AudioUtilities.CreateDevice(AudioUtilities.GetMicrophone())
        if device is None:
            return False
        manager = device.AudioSessionManager
        if manager is None:
            return False
        enumerator = manager.GetSessionEnumerator()
        own_pid = os.getpid()
        for i in range(enumerator.GetCount()):
            ctl = enumerator.GetSession(i)
            if ctl is None:
                continue
            ctl2 = ctl.QueryInterface(IAudioSessionControl2)
            if ctl2 is None:
                continue
            if int(ctl2.GetState()) != AudioSessionState.Active.value:
                continue
            pid = ctl2.GetProcessId()
            if pid == own_pid:
                continue
            # TK-378 (ISS-55): an always-on background helper holding the capture endpoint is not
            # a call. An owner we cannot identify is deliberately NOT ignored — an unreadable
            # Active session still counts, which is the conservative direction.
            owner = _capture_owner_name(pid)
            if owner is not None and owner in _IGNORED_CAPTURE_OWNERS:
                continue
            _note_in_call_owner(owner or "<unreadable>")
            return True
        _note_in_call_owner(None)
        return False
    except Exception:  # pragma: no cover - COM/platform degrade
        logger.warning(
            "observe_mic: capture-session probe raised — treating as False", exc_info=True
        )
        return False


class ObservationSink(Protocol):
    """The structural shape ``MicInCallProbe`` needs from a store — matches ``observations.
    ObservationStore.append_segment`` exactly (mirrors ``observe_screen.ObservationSink``): a
    test fake only needs to satisfy this shape, never import ``ObservationStore`` itself."""

    def append_segment(
        self,
        channel: str,
        kind: str,
        started_at: datetime,
        ended_at: datetime,
        payload: dict[str, Any],
        day_key: date,
    ) -> None: ...


class MicInCallProbe:
    """Coalesces consecutive in-call beats into ONE closed ``channel='mic', kind='in_call'``
    segment and writes it to ``store``, keeping ``current_activity.in_call`` live as the probe's
    own state (TK-313, DEC-68a/e).

    ``read_beat`` defaults to ``probe_in_call`` — injectable so tests never touch pycaw. ``clock``
    returns an aware ``datetime`` (mirrors ``bootstrap._utc_now``'s own contract), used both for
    the segment span and (via ``tz``) the DEC-21 ``day_key`` a closed segment is stamped with.
    """

    def __init__(
        self,
        *,
        store: ObservationSink,
        current_activity: CurrentActivity,
        tz: ZoneInfo,
        clock: Callable[[], datetime],
        read_beat: Callable[[], bool] = probe_in_call,
    ) -> None:
        self._store = store
        self._current_activity = current_activity
        self._tz = tz
        self._clock = clock
        self._read_beat = read_beat
        self._open_started_at: datetime | None = None

    def process_beat(self, in_call: bool, *, now: datetime) -> None:
        """The pure(ish) core: fold ONE beat (already read) into the probe's open/closed segment
        state. Never raises.

        Opus-verify repair, CHECKED AND RULED OUT: this probe deliberately does NOT stamp
        ``CurrentActivity.refreshed_at`` — it writes only the ``in_call`` flag, and only on
        transitions, never per-beat. ``refreshed_at`` is the SCREEN poller's liveness clock
        (``app``/``title`` are what the staleness gate protects, and the renderer never emits
        ``(in a call)`` without a live app/title anyway); a mic-side stamp would let a healthy mic
        poller present a dead screen poller's frozen window as live."""
        if in_call:
            if self._open_started_at is None:
                self._open_started_at = now
                self._current_activity.in_call = True
            return
        self._close_open_segment(now)

    def _close_open_segment(self, now: datetime) -> None:
        if self._open_started_at is None:
            return
        day_key = wombat_today(self._open_started_at, self._tz)
        try:
            self._store.append_segment(
                channel=_CHANNEL,
                kind=_KIND,
                started_at=self._open_started_at,
                ended_at=now,
                payload={},
                day_key=day_key,
            )
        except Exception:
            logger.warning(
                "MicInCallProbe: store raised appending a segment — dropping it", exc_info=True
            )
        self._open_started_at = None
        self._current_activity.in_call = False

    def close(self, *, now: datetime | None = None) -> None:
        """Close any open segment — the shutdown path."""
        self._close_open_segment(now if now is not None else self._clock())

    def _safe_read(self) -> bool:
        try:
            return self._read_beat()
        except Exception:  # belt-and-suspenders — probe_in_call never raises either
            return False

    def poll_once(self) -> None:
        """Do ONE beat: read the probe, fold it into state. Never raises."""
        now = self._clock()
        beat = self._safe_read()
        self.process_beat(beat, now=now)

    async def run(self) -> None:
        """The live polling loop: sleeps ``_POLL_INTERVAL_S`` between beats, forever, until
        cancelled — cancellation closes any open segment (graceful shutdown) before propagating."""
        try:
            while True:
                await asyncio.sleep(_POLL_INTERVAL_S)
                self.poll_once()
        except asyncio.CancelledError:
            self.close()
            raise


__all__ = [
    "MicInCallProbe",
    "ObservationSink",
    "probe_in_call",
]
