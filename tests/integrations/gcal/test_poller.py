"""TK-72 acceptance criteria — CalendarPoller (EP-15, Q-59/Q-60).

CI tests are mocked, ZERO network — the Google Calendar v3 REST session is a bare fake
exposing only ``.get`` (Q-59 ruling 3: the no-write guarantee is structural, there is no
``post``/``put``/``patch``/``delete`` for this poller's code to even call).

  AC1 (3 events, timed + all-day, no write): ``test_ac1_...``.
  AC2 (transient/auth/malformed error -> [] never raise): ``test_ac2_...`` (parametrized).
  AC3 (identical canonical key across polls, single admission via the registry):
      ``test_ac3_...``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import requests

from wombat.calendar.models import CalendarEvent
from wombat.domain.item_identity import idempotency_key as derive_key
from wombat.integrations.gcal.poller import CalendarPoller
from wombat.queue import EnqueueResult, QueueItem
from wombat.sources.registry import SourceRegistry

_TZ = ZoneInfo("America/Chicago")
_NOW = datetime(2026, 7, 2, 12, 0, tzinfo=UTC)


class _FakeResponse:
    """Mimics the ``requests.Response`` surface ``poll()`` touches: ``.raise_for_status()``
    and ``.json()``."""

    def __init__(self, status_code: int, body: dict[str, Any]) -> None:
        self.status_code = status_code
        self._body = body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"{self.status_code} error")

    def json(self) -> dict[str, Any]:
        return self._body


@dataclass
class _FakeSession:
    """Exposes ONLY ``.get`` — no ``post``/``put``/``patch``/``delete`` exist at all, so the
    no-write guarantee (Q-59 ruling 3) is structural, not merely asserted after the fact."""

    response: _FakeResponse | None = None
    exception: Exception | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)

    def get(self, url: str, *, params: dict[str, str], timeout: float) -> _FakeResponse:
        self.calls.append({"url": url, "params": params, "timeout": timeout})
        if self.exception is not None:
            raise self.exception
        assert self.response is not None
        return self.response


class _DedupingEnqueuer:
    """Fake Enqueuer that dedupes by ``idempotency_key`` — mirrors TK-2's proven ``ON
    CONFLICT DO NOTHING`` at the DB layer (Q-59 AC3: the DB half is proven elsewhere; this
    test only proves the KEY the poller + registry produce is stable/identical across polls
    and that a second occurrence is not admitted)."""

    def __init__(self) -> None:
        self.admitted: list[QueueItem] = []
        self._seen: set[str] = set()

    def enqueue(self, item: QueueItem) -> EnqueueResult:
        if item.idempotency_key in self._seen:
            return EnqueueResult.ALREADY_QUEUED
        self._seen.add(item.idempotency_key)
        self.admitted.append(item)
        return EnqueueResult.QUEUED


async def _wait_until(
    predicate: Callable[[], bool], *, timeout: float = 2.0, interval: float = 0.005
) -> None:
    """Poll ``predicate`` until true or ``timeout`` elapses (event-driven, no fixed sleeps) —
    matches ``tests/unit/test_source_registry.py``'s idiom."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() >= deadline:
            raise AssertionError(f"condition not met within {timeout}s")
        await asyncio.sleep(interval)


# ------------------------------------------------------------------------------------------ AC1


async def test_ac1_poll_returns_three_events_including_timed_and_allday_no_write() -> None:
    body = {
        "items": [
            {
                "id": "evt1",
                "summary": "Standup",
                "start": {"dateTime": "2026-07-02T09:00:00-05:00"},
                "end": {"dateTime": "2026-07-02T09:30:00-05:00"},
            },
            {
                # no "summary" key at all -> title must default to ""
                "id": "evt2",
                "start": {"dateTime": "2026-07-02T14:00:00Z"},
                "end": {"dateTime": "2026-07-02T15:00:00Z"},
            },
            {
                "id": "evt3",
                "summary": "Company Holiday",
                "start": {"date": "2026-07-04"},
                "end": {"date": "2026-07-05"},  # Google's exclusive end, preserved as-is
            },
        ]
    }
    session = _FakeSession(response=_FakeResponse(200, body))
    poller = CalendarPoller(
        session=session, tz=_TZ, poll_interval_seconds=0.1, clock=lambda: _NOW
    )

    result = await poller.poll()

    assert len(result) == 3
    assert [e.event_key for e in result] == ["evt1", "evt2", "evt3"]

    got = [CalendarEvent.from_payload(e.payload) for e in result]
    expected = [
        CalendarEvent(
            event_id="evt1",
            title="Standup",
            start=datetime(2026, 7, 2, 14, 0, tzinfo=UTC),
            end=datetime(2026, 7, 2, 14, 30, tzinfo=UTC),
            all_day=False,
        ),
        CalendarEvent(
            event_id="evt2",
            title="",
            start=datetime(2026, 7, 2, 14, 0, tzinfo=UTC),
            end=datetime(2026, 7, 2, 15, 0, tzinfo=UTC),
            all_day=False,
        ),
        CalendarEvent(
            event_id="evt3",
            title="Company Holiday",
            start=datetime(2026, 7, 4, tzinfo=_TZ).astimezone(UTC),
            end=datetime(2026, 7, 5, tzinfo=_TZ).astimezone(UTC),
            all_day=True,
        ),
    ]
    assert got == expected
    assert any(e.all_day for e in got)  # at least one all-day event among the 3
    assert any(not e.all_day for e in got)  # at least one timed event among the 3
    # every payload key round-trips exactly (Q-49 JSON-native rule)
    for event, source_event in zip(got, result, strict=True):
        assert CalendarEvent.from_payload(event.to_payload()) == event
        assert "event_class" not in source_event.payload  # ruling 6: never stamped here

    # structural no-write guarantee (Q-59 ruling 3): the fake session exposes ONLY .get.
    assert not hasattr(session, "post")
    assert not hasattr(session, "put")
    assert not hasattr(session, "patch")
    assert not hasattr(session, "delete")
    assert len(session.calls) == 1
    assert session.calls[0]["params"]["singleEvents"] == "true"
    assert session.calls[0]["params"]["orderBy"] == "startTime"


# ------------------------------------------------------------------------------------------ AC2


def _connection_error_session() -> _FakeSession:
    return _FakeSession(exception=requests.exceptions.ConnectionError("boom"))


def _http_error_session(status: int) -> _FakeSession:
    return _FakeSession(response=_FakeResponse(status, {"items": []}))


def _malformed_missing_items_session() -> _FakeSession:
    return _FakeSession(response=_FakeResponse(200, {"not_items": []}))


def _malformed_bad_interval_session() -> _FakeSession:
    body = {
        "items": [
            {
                "id": "backwards",
                "summary": "oops",
                "start": {"dateTime": "2026-07-02T10:00:00Z"},
                "end": {"dateTime": "2026-07-02T09:00:00Z"},  # end before start
            }
        ]
    }
    return _FakeSession(response=_FakeResponse(200, body))


@pytest.mark.parametrize(
    "make_session",
    [
        _connection_error_session,
        lambda: _http_error_session(401),
        lambda: _http_error_session(403),
        lambda: _http_error_session(500),
        lambda: _http_error_session(503),
        _malformed_missing_items_session,
        _malformed_bad_interval_session,
    ],
    ids=[
        "connection_error",
        "http_401",
        "http_403",
        "http_500",
        "http_503",
        "malformed_missing_items_key",
        "malformed_bad_interval",
    ],
)
async def test_ac2_transient_and_malformed_errors_degrade_to_empty_never_raise(
    make_session: Callable[[], _FakeSession], caplog: pytest.LogCaptureFixture
) -> None:
    session = make_session()
    poller = CalendarPoller(
        session=session, tz=_TZ, poll_interval_seconds=0.1, clock=lambda: _NOW
    )

    with caplog.at_level(logging.WARNING):
        result = await poller.poll()  # MUST NOT raise

    assert result == []
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("gcal" in r.getMessage() for r in warnings)


# ------------------------------------------------------------------------------------------ AC3


async def test_ac3_same_event_two_polls_identical_key_single_admission_via_registry() -> None:
    body = {
        "items": [
            {
                "id": "evt-dup",
                "summary": "Repeat",
                "start": {"dateTime": "2026-07-02T09:00:00Z"},
                "end": {"dateTime": "2026-07-02T09:30:00Z"},
            }
        ]
    }
    # Direct poll()-level proof: the SAME event on two separate polls yields the same
    # event_key, and the canonical TK-12 derivation of that key is identical both times.
    # (Its own session/poller — kept separate from the registry-driven poller below so the
    # two polls issued here don't get counted as part of THAT poller's call count.)
    direct_session = _FakeSession(response=_FakeResponse(200, body))
    direct_poller = CalendarPoller(
        session=direct_session, tz=_TZ, poll_interval_seconds=0.01, clock=lambda: _NOW
    )
    first = await direct_poller.poll()
    second = await direct_poller.poll()
    assert len(first) == 1
    assert len(second) == 1
    assert first[0].event_key == second[0].event_key == "evt-dup"
    assert derive_key("gcal", first[0].event_key) == derive_key("gcal", second[0].event_key)

    # Registry-level proof: mapped through the REAL SourceRegistry against a fake Enqueuer
    # that dedupes by idempotency_key (mirroring TK-2's ON CONFLICT DO NOTHING) — only the
    # first occurrence is admitted.
    session = _FakeSession(response=_FakeResponse(200, body))
    poller = CalendarPoller(
        session=session, tz=_TZ, poll_interval_seconds=0.01, clock=lambda: _NOW
    )
    enqueuer = _DedupingEnqueuer()
    registry = SourceRegistry(enqueuer)
    registry.register(poller)

    await registry.start()
    try:
        await _wait_until(lambda: len(session.calls) >= 2)
    finally:
        await registry.stop()

    assert len(enqueuer.admitted) == 1
    assert enqueuer.admitted[0].idempotency_key == derive_key("gcal", "evt-dup")
