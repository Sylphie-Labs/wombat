"""observe_screen — the DEC-68(a) screen channel: ``ScreenActivityCollector`` (TK-310).

``read_foreground_window()`` is the impure edge: pure ``ctypes`` calls (``GetForegroundWindow`` +
``GetWindowThreadProcessId`` + ``QueryFullProcessImageNameW`` + ``GetWindowTextW`` — the
``sources.presence.read_idle_ms`` ctypes precedent; ZERO new dependencies) reading the current
foreground window's owning process image path and title. On ANY failure — non-Windows, no
foreground window, any syscall error — it returns ``None`` and NEVER raises (mirrors ``read_idle_
ms``'s degrade contract exactly).

``ScreenActivityCollector`` is a small stateful coalescer: consecutive beats reporting the SAME
``(app, normalized title)`` extend the currently-open segment; a beat reporting something
DIFFERENT, a failed read (``None``), or an explicit ``close()`` (process shutdown) closes it.
Titles are normalized (stripped) and truncated to ``_MAX_TITLE_CHARS`` before either comparison or
storage. A closed segment shorter than ``_MIN_SEGMENT_S`` is dropped — never appended to the store
— but the collector's open/closed state still advances normally. A ``None`` beat (syscall failure)
logs AT MOST ONE WARNING per consecutive failure streak, never one per beat, and never raises. A
store raise while appending a segment is caught, logged once loudly, and the collector's state
still resets cleanly so later segments record normally.

NO PIXELS, NO SCREENSHOTS, NO OCR — nothing raw exists anywhere in this module beyond the closed
``(app, title, started_at, ended_at)`` segment handed to ``ObservationStore.append_segment``
(DEC-68(a) structural).

``poll_once()``/``run()`` are the production polling surface (poll every ``_POLL_INTERVAL_S``
seconds); ``process_beat()`` is the pure(ish), directly-testable core the ACs drive without any
ctypes/asyncio involvement.
"""

from __future__ import annotations

import asyncio
import ctypes
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from .domain.daily_ledger import wombat_today
from .observations import CurrentActivity

logger = logging.getLogger(__name__)

# Pinned (DEC-63 no-knob precedent): NOT operator-tunable.
_POLL_INTERVAL_S = 10.0
_MAX_TITLE_CHARS = 120
_MIN_SEGMENT_S = 30.0

# The DEC-68(a)/(c) ledger vocabulary for this channel.
_CHANNEL = "screen"
_KIND = "app_segment"

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


@dataclass(frozen=True, slots=True)
class ScreenBeat:
    """One raw foreground-window reading: the owning process's image path and the window title,
    both UNNORMALIZED/untruncated — ``ScreenActivityCollector`` owns that step."""

    app: str
    title: str


def read_foreground_window() -> ScreenBeat | None:
    """IMPURE: read the current foreground window's owning process image path + title via ctypes.

    Returns ``None`` (NEVER raises) on any failure — non-Windows, no foreground window, or any
    syscall error along the way (mirrors ``sources.presence.read_idle_ms``'s degrade contract).
    """
    try:
        from ctypes import wintypes
    except (ImportError, ValueError):  # pragma: no cover - non-Windows degrade
        return None

    try:
        windll = getattr(ctypes, "windll", None)
        if windll is None:
            return None
        user32 = windll.user32
        kernel32 = windll.kernel32

        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None

        pid = wintypes.DWORD()
        if not user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid)) or pid.value == 0:
            return None

        title_buf = ctypes.create_unicode_buffer(1024)
        user32.GetWindowTextW(hwnd, title_buf, 1024)
        title = title_buf.value

        h_process = kernel32.OpenProcess(
            _PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value
        )
        if not h_process:
            return None
        try:
            name_buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(1024)
            ok = kernel32.QueryFullProcessImageNameW(h_process, 0, name_buf, ctypes.byref(size))
            if not ok:
                return None
            app = name_buf.value
        finally:
            kernel32.CloseHandle(h_process)

        if not app:
            return None
        return ScreenBeat(app=app, title=title)
    except (AttributeError, OSError, ValueError):  # pragma: no cover - syscall/platform degrade
        return None


def _normalize_title(raw: str) -> str:
    """Strip + truncate to ``_MAX_TITLE_CHARS`` — the SAME normalized string is used both as the
    coalescing comparison key and the stored payload title."""
    return raw.strip()[:_MAX_TITLE_CHARS]


class ObservationSink(Protocol):
    """The structural shape ``ScreenActivityCollector`` needs from a store — matches
    ``observations.ObservationStore.append_segment`` exactly (mirrors the ``voice.
    context_prefetch.VoiceContextStore`` Protocol convention): a test fake only needs to satisfy
    this shape, never import ``ObservationStore`` itself."""

    def append_segment(
        self,
        channel: str,
        kind: str,
        started_at: datetime,
        ended_at: datetime,
        payload: dict[str, Any],
        day_key: date,
    ) -> None: ...


class ScreenActivityCollector:
    """Coalesces consecutive foreground-window beats into closed ``(app, title)`` segments and
    writes them to ``store``, keeping ``current_activity`` live as the open segment changes
    (TK-310).

    ``read_beat`` defaults to ``read_foreground_window`` — injectable so tests never touch ctypes.
    ``clock`` returns an aware ``datetime`` (mirrors ``bootstrap._utc_now``'s own contract) used
    both for segment spans and (via ``tz``) the DEC-21 ``day_key`` a closed segment is stamped
    with.
    """

    def __init__(
        self,
        *,
        store: ObservationSink,
        current_activity: CurrentActivity,
        tz: ZoneInfo,
        clock: Callable[[], datetime],
        read_beat: Callable[[], ScreenBeat | None] = read_foreground_window,
    ) -> None:
        self._store = store
        self._current_activity = current_activity
        self._tz = tz
        self._clock = clock
        self._read_beat = read_beat
        self._open_app: str | None = None
        self._open_title: str | None = None
        self._open_started_at: datetime | None = None
        self._failure_streak_warned = False

    def process_beat(self, beat: ScreenBeat | None, *, now: datetime) -> None:
        """The pure(ish) core: fold ONE beat (already read — ``None`` means the read failed) into
        the collector's open/closed segment state. Never raises."""
        if beat is None:
            if not self._failure_streak_warned:
                logger.warning(
                    "ScreenActivityCollector: foreground-window read failed — skipping this beat"
                )
                self._failure_streak_warned = True
            self._close_open_segment(now)
            return

        self._failure_streak_warned = False
        title = _normalize_title(beat.title)
        if self._open_app == beat.app and self._open_title == title:
            return  # same segment continues — nothing to do

        self._close_open_segment(now)
        self._open_app = beat.app
        self._open_title = title
        self._open_started_at = now
        self._current_activity.app = beat.app
        self._current_activity.title = title
        self._current_activity.since = now

    def _close_open_segment(self, now: datetime) -> None:
        if self._open_app is None or self._open_started_at is None:
            return
        duration_s = (now - self._open_started_at).total_seconds()
        if duration_s >= _MIN_SEGMENT_S:
            payload = {"app": self._open_app, "title": self._open_title}
            day_key = wombat_today(self._open_started_at, self._tz)
            try:
                self._store.append_segment(
                    channel=_CHANNEL,
                    kind=_KIND,
                    started_at=self._open_started_at,
                    ended_at=now,
                    payload=payload,
                    day_key=day_key,
                )
            except Exception:
                logger.warning(
                    "ScreenActivityCollector: store raised appending a segment — dropping it",
                    exc_info=True,
                )
        self._open_app = None
        self._open_title = None
        self._open_started_at = None
        self._current_activity.app = None
        self._current_activity.title = None
        self._current_activity.since = None

    def close(self, *, now: datetime | None = None) -> None:
        """Close any open segment — the shutdown path (mirrors an idle/change beat's own close,
        just without a new segment opening after it)."""
        self._close_open_segment(now if now is not None else self._clock())

    def _safe_read(self) -> ScreenBeat | None:
        try:
            return self._read_beat()
        except Exception:  # belt-and-suspenders — read_foreground_window never raises either
            return None

    def poll_once(self) -> None:
        """Do ONE beat: read the foreground window, fold it into state. Never raises."""
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
    "ObservationSink",
    "ScreenActivityCollector",
    "ScreenBeat",
    "read_foreground_window",
]
