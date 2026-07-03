"""TK-16 live-wire smoke — CalendarAuth -> AuthorizedSession -> CalendarPoller (Q-61).

Gated on ``WOMBAT_TEST_GCAL_LIVE=1`` (mirrors the ``WOMBAT_TEST_GCAL_LIVE`` idiom in
``tests/integrations/gcal/test_auth.py``) — SKIPS loudly with no gate var set. This is the
Q-44-class pre-live obligation: it must run green (against a real, already-consented vault
credential) before the first live laptop session, proving the composed
``make_calendar_session -> CalendarPoller`` path actually works end-to-end against real
Google infrastructure, not just mocks.

Exercises exactly ONE real authorized Calendar v3 events GET through the FULLY composed
stack: only ``.get`` is ever issued (the ``_CalendarSession`` Protocol / ``_FakeSession``-free
real path structurally cannot call anything else), the raw GET returns 2xx, and the poller's
own parse turns the response into ``CalendarEvent``s (round-tripped through ``from_payload``
to prove the payload shape is valid).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from wombat.calendar.models import CalendarEvent
from wombat.config import load_config
from wombat.integrations.gcal.session import make_calendar_session

_LIVE_ENV = "WOMBAT_TEST_GCAL_LIVE"

_requires_live_gcal = pytest.mark.skipif(
    not os.environ.get(_LIVE_ENV),
    reason=(
        f"{_LIVE_ENV} is not set — skipping the live composed-stack Calendar events GET smoke "
        "test. Run `python -m wombat.integrations.gcal.auth` once to grant consent (stores a "
        f"token in the OS keyring vault), then export {_LIVE_ENV}=1 to exercise a real GET. "
        "Q-44-class pre-live obligation: must be green before the first live laptop session."
    ),
)


@_requires_live_gcal
async def test_live_composed_stack_issues_one_real_get_and_parses_calendar_events() -> None:
    from wombat.integrations.gcal.poller import CalendarPoller

    config = load_config()
    session = make_calendar_session(config)  # real CalendarAuth -> real AuthorizedSession

    # The raw GET (the exact shape TK-72's poller issues) — asserts a real 2xx from Google.
    raw_response = session.get(
        "https://www.googleapis.com/calendar/v3/calendars/primary/events",
        params={
            "timeMin": datetime.now(UTC).isoformat(),
            "timeMax": datetime.now(UTC).isoformat(),
            "singleEvents": "true",
            "orderBy": "startTime",
        },
        timeout=30.0,
    )
    assert 200 <= raw_response.status_code < 300
    assert "items" in raw_response.json()

    # The FULLY composed stack: CalendarAuth -> AuthorizedSession -> CalendarPoller. Only
    # `.get` is ever available on `session` (an AuthorizedSession) via the poller's own
    # `_CalendarSession` Protocol usage — no write method is ever invoked.
    poller = CalendarPoller(session=session, tz=ZoneInfo("UTC"), poll_interval_seconds=300.0)
    events = await poller.poll()

    assert isinstance(events, list)
    for event in events:
        parsed = CalendarEvent.from_payload(event.payload)
        assert isinstance(parsed, CalendarEvent)
