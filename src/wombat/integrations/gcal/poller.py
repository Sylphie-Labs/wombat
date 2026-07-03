"""wombat.integrations.gcal.poller — CalendarPoller (TK-72, EP-15, Q-59/Q-60).

The first real ``InputSource`` (``sources/base.py``): reads Google Calendar v3 events
READ-ONLY and yields them as ``SourceEvent``s for the ``SourceRegistry`` to enqueue. It never
writes to Google Calendar (DEC-16) and never constructs the ``QueueItem`` itself — the registry
owns the ``SourceEvent -> QueueItem`` mapping (Q-59 ruling 1).

Design (Q-59 BINDING rulings):
  * Conforms to the AS-BUILT ``InputSource`` Protocol exactly: ``id = "gcal"``,
    ``poll_interval_seconds``, ``async start()/stop()/poll() -> list[SourceEvent]``.
  * HTTP seam: ``_CalendarSession`` is a minimal Protocol (mirrors ``sources.registry.Enqueuer``
    — the ONE method this poller needs) over ``google.auth.transport.requests.AuthorizedSession``.
    The composition root builds the real ``AuthorizedSession`` from TK-71's
    ``CalendarAuth().get_credentials()`` and injects it here (constructor arg) — this module
    never constructs ``CalendarAuth`` and never imports ``googleapiclient`` (REJECTED,
    zero new deps). Only ``.get()`` is ever called — the no-write guarantee is structural.
  * Transient-error posture (LOAD-BEARING, ruling 4): network errors, HTTP 401/403/5xx, and a
    malformed/blip response body are ALL caught inside ``poll()`` -> logged as a WARNING naming
    the source id -> return ``[]``. ``poll()`` NEVER raises for these — the as-built
    ``SourceRegistry`` treats a raising ``poll()`` as source-degraded and stops polling that
    source, which would let one network blip permanently kill calendar ingestion until restart.
  * ``lookahead_hours: float = 48.0`` (AC-fixed default, TK-8 timeout precedent). ``clock``
    (``Callable[[], datetime]``, aware UTC) and ``tz`` (``ZoneInfo``, the configured wombat
    civil-local timezone, DEC-21) are both injected, mirroring ``DailyLedger``'s idiom.
  * No ``event_class`` is ever stamped on the payload (ruling 6) — the Q-41 total fallback
    resolves it to ``ItemKind.GENERIC`` downstream.
  * All-day events (Q-60, ruling 7): Google's date-only ``start.date``/``end.date`` resolve to
    midnight in the injected ``tz``, normalized to UTC, ``all_day=True``; Google's exclusive-end
    date is preserved as-is. Timed events parse ``start.dateTime``/``end.dateTime`` (already
    tz-offset) and normalize to UTC, ``all_day=False``. Neither shape crashes or is dropped.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import requests

from wombat.calendar.models import CalendarEvent
from wombat.sources.base import SourceEvent

logger = logging.getLogger(__name__)

# The Calendar v3 REST events-list endpoint for the single primary calendar (no multi-calendar
# fan-out in v1, non_goal). Read-only: only GET is ever issued against this URL (DEC-16).
_EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"

# A conservative fixed request timeout — not a TK-13 tunable (no ticket asked for one),
# just a guard against an authorized session hanging forever on a dead connection.
_REQUEST_TIMEOUT_S = 30.0


def _utc_now() -> datetime:
    """The real-clock default for ``CalendarPoller``'s injected ``clock``."""
    return datetime.now(UTC)


def _rfc3339(instant: datetime) -> str:
    """RFC3339 UTC form for the ``timeMin``/``timeMax`` query params."""
    return instant.astimezone(UTC).isoformat()


class _CalendarSession(Protocol):
    """The ONE HTTP method ``CalendarPoller`` needs (mirrors ``sources.registry.Enqueuer``'s
    minimal-seam pattern). Production injects a real ``AuthorizedSession``; tests inject a bare
    fake exposing only ``get`` — which makes the no-write guarantee structural (Q-59 ruling 3):
    there is no ``post``/``put``/``patch``/``delete`` for this poller's code to even call."""

    def get(self, url: str, *, params: dict[str, str], timeout: float) -> requests.Response: ...


def _parse_event(raw: dict[str, Any], *, tz: ZoneInfo) -> CalendarEvent:
    """Map one Google Calendar v3 event JSON object to a ``CalendarEvent`` (Q-60 ruling 7).

    Raises ``KeyError``/``ValueError``/``TypeError`` on a malformed/unexpected shape — ``poll()``
    catches these and degrades to ``[]`` rather than crashing (ruling 4).
    """
    event_id = raw["id"]
    title = raw.get("summary") or ""
    start_field = raw["start"]
    end_field = raw["end"]
    if "date" in start_field:
        # All-day event: date-only, resolve at midnight in the injected tz, normalize to UTC.
        # Google's exclusive-end date is preserved as-is (no -1-day adjustment here).
        start_date = date.fromisoformat(start_field["date"])
        end_date = date.fromisoformat(end_field["date"])
        start = datetime(start_date.year, start_date.month, start_date.day, tzinfo=tz).astimezone(
            UTC
        )
        end = datetime(end_date.year, end_date.month, end_date.day, tzinfo=tz).astimezone(UTC)
        all_day = True
    else:
        start = datetime.fromisoformat(start_field["dateTime"]).astimezone(UTC)
        end = datetime.fromisoformat(end_field["dateTime"]).astimezone(UTC)
        all_day = False
    return CalendarEvent(event_id=event_id, title=title, start=start, end=end, all_day=all_day)


class CalendarPoller:
    """Reads Google Calendar v3 events (read-only) and yields them as ``SourceEvent``s.

    Conforms to ``sources.base.InputSource`` (Q-59 ruling 1). Constructor-injects the
    authorized HTTP session, the clock, and the wombat civil-local ``tz`` — this class never
    constructs ``CalendarAuth`` and never reads real wall-clock time or the real timezone
    itself, matching every other injected-dependency seam in this codebase.
    """

    id: str = "gcal"

    def __init__(
        self,
        *,
        session: _CalendarSession,
        tz: ZoneInfo,
        poll_interval_seconds: float,
        lookahead_hours: float = 48.0,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.poll_interval_seconds = poll_interval_seconds
        self._session = session
        self._tz = tz
        self._lookahead_hours = lookahead_hours
        self._clock = clock

    async def start(self) -> None:
        """No lifecycle setup needed — the injected session is already authorized."""
        return None

    async def stop(self) -> None:
        """No lifecycle teardown needed."""
        return None

    def fetch_window(self, *, lookahead_hours: float | None = None) -> list[CalendarEvent]:
        """Fetch events in ``[now, now + lookahead_hours)`` — the RAISING read seam (TK-98).

        ``lookahead_hours`` defaults to the ctor's ``lookahead_hours`` when omitted (``None``).
        Unlike ``poll()``, this method does NOT catch anything: a network error, an HTTP
        401/403/5xx, or a malformed response body all propagate to the caller as-is. This lets
        a caller (e.g. ``BriefGatherStage``, TK-98) distinguish "source unavailable" from "zero
        events" — something a swallowed-to-``[]`` result cannot. ``poll()`` below is the
        transient-error-tolerant wrapper around this method (ruling 4 unchanged).
        """
        hours = self._lookahead_hours if lookahead_hours is None else lookahead_hours
        now = self._clock()
        params = {
            "timeMin": _rfc3339(now),
            "timeMax": _rfc3339(now + timedelta(hours=hours)),
            "singleEvents": "true",
            "orderBy": "startTime",
        }
        response = self._session.get(_EVENTS_URL, params=params, timeout=_REQUEST_TIMEOUT_S)
        response.raise_for_status()
        items = response.json()["items"]
        return [_parse_event(raw, tz=self._tz) for raw in items]

    async def poll(self) -> list[SourceEvent]:
        """Fetch events in ``[now, now + lookahead_hours)`` and yield them as ``SourceEvent``s.

        NEVER raises (ruling 4): a network error, an HTTP 401/403/5xx, or a malformed response
        body are all logged as a WARNING naming this source's id and degrade to ``[]`` — the
        registry keeps polling this source on the next cycle instead of marking it degraded.
        A thin wrapper around the RAISING ``fetch_window`` (TK-98) — behavior-preserving.
        """
        try:
            events = self.fetch_window()
        except requests.exceptions.RequestException:
            logger.warning(
                "gcal source %r: Calendar API request failed (network/auth/server error) — "
                "degrading this poll to no events",
                self.id,
                exc_info=True,
            )
            return []
        except (KeyError, ValueError, TypeError):
            logger.warning(
                "gcal source %r: malformed Calendar API response — degrading this poll to "
                "no events",
                self.id,
                exc_info=True,
            )
            return []

        return [
            SourceEvent(event_key=event.event_id, payload=event.to_payload()) for event in events
        ]


__all__ = ["CalendarPoller"]
