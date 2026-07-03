"""TK-75 acceptance criteria — GmailPoller (EP-17, Q-65).

CI tests are mocked, ZERO network — the Gmail v1 REST session is a bare fake exposing only
``.get`` (Q-65 ruling 2: the no-write guarantee is structural, there is no
``post``/``put``/``patch``/``delete`` for this poller's code to even call, so it cannot invoke
or hold ``gmail.drafts.create``).

  AC1 (3 messages, wire round-trip incl. body_text, no write): ``test_ac1_...``.
  AC2 (transport-only: no Capability registration, session .get-only, no compose/draft/send
      call site in the poller module): ``test_ac2_...``.
  AC3 (readonly-only scope guard, rejects compose): ``test_ac3_...`` (see also test_auth.py).
  AC4 (identical canonical key across polls, single admission via the registry; transient/
      malformed errors -> [] never raise): ``test_ac4_...``.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import requests

from wombat.domain.item_identity import idempotency_key as derive_key
from wombat.integrations.gmail.auth import (
    GMAIL_SCOPES,
    ScopeViolationError,
    assert_gmail_readonly_scopes,
)
from wombat.integrations.gmail.models import GmailMessageItem
from wombat.integrations.gmail.poller import GmailPoller
from wombat.queue import EnqueueResult, QueueItem
from wombat.sources.registry import SourceRegistry

_NOW = datetime(2026, 7, 2, 12, 0, tzinfo=UTC)

_POLLER_SRC = (
    Path(__file__).resolve().parents[3] / "src" / "wombat" / "integrations" / "gmail" / "poller.py"
).read_text(encoding="utf-8")


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


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
    no-write guarantee (Q-65 ruling 2) is structural, not merely asserted after the fact.

    ``list_response`` answers the ``users/me/messages`` list call; ``message_responses`` maps a
    message id to the ``.get`` response for that id's full-message fetch; ``exception`` (if set)
    is raised on every call.
    """

    list_response: _FakeResponse | None = None
    message_responses: dict[str, _FakeResponse] = field(default_factory=dict)
    exception: Exception | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)

    def get(self, url: str, *, params: dict[str, str], timeout: float) -> _FakeResponse:
        self.calls.append({"url": url, "params": params, "timeout": timeout})
        if self.exception is not None:
            raise self.exception
        if url.endswith("/messages"):
            assert self.list_response is not None
            return self.list_response
        message_id = url.rsplit("/", 1)[-1]
        return self.message_responses[message_id]


class _DedupingEnqueuer:
    """Fake Enqueuer that dedupes by ``idempotency_key`` — mirrors TK-2's proven ``ON
    CONFLICT DO NOTHING`` at the DB layer."""

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
    """Poll ``predicate`` until true or ``timeout`` elapses (event-driven, no fixed sleeps)."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() >= deadline:
            raise AssertionError(f"condition not met within {timeout}s")
        await asyncio.sleep(interval)


def _plain_message(
    message_id: str, *, subject: str, sender: str, body: str, internal_date_ms: int
) -> dict[str, Any]:
    return {
        "id": message_id,
        "internalDate": str(internal_date_ms),
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "Subject", "value": subject},
                {"name": "From", "value": sender},
            ],
            "body": {"data": _b64(body)},
        },
    }


def _multipart_message(
    message_id: str,
    *,
    subject: str,
    sender: str,
    plain_body: str,
    html_body: str,
    internal_date_ms: int,
) -> dict[str, Any]:
    return {
        "id": message_id,
        "internalDate": str(internal_date_ms),
        "payload": {
            "mimeType": "multipart/alternative",
            "headers": [
                {"name": "Subject", "value": subject},
                {"name": "From", "value": sender},
            ],
            "parts": [
                {"mimeType": "text/html", "body": {"data": _b64(html_body)}},
                {"mimeType": "text/plain", "body": {"data": _b64(plain_body)}},
            ],
        },
    }


def _html_only_message(
    message_id: str, *, subject: str, sender: str, html_body: str, internal_date_ms: int
) -> dict[str, Any]:
    return {
        "id": message_id,
        "internalDate": str(internal_date_ms),
        "payload": {
            "mimeType": "text/html",
            "headers": [
                {"name": "Subject", "value": subject},
                {"name": "From", "value": sender},
            ],
            "body": {"data": _b64(html_body)},
        },
    }


# ------------------------------------------------------------------------------------------ AC1


async def test_ac1_poll_returns_three_messages_round_tripping_through_models_no_write() -> None:
    list_body = {"messages": [{"id": "m1"}, {"id": "m2"}, {"id": "m3"}]}
    m1 = _plain_message(
        "m1", subject="Q3 budget", sender="jane@example.com", body="Hi, budget attached.",
        internal_date_ms=1751457600000,
    )
    m2 = _multipart_message(
        "m2", subject="Lunch?", sender="bob@example.com", plain_body="Lunch at noon?",
        html_body="<p>Lunch at noon?</p>", internal_date_ms=1751461200000,
    )
    m3 = _html_only_message(
        "m3", subject="", sender="newsletter@example.com", html_body="<p>only html here</p>",
        internal_date_ms=1751464800000,
    )
    session = _FakeSession(
        list_response=_FakeResponse(200, list_body),
        message_responses={
            "m1": _FakeResponse(200, m1),
            "m2": _FakeResponse(200, m2),
            "m3": _FakeResponse(200, m3),
        },
    )
    poller = GmailPoller(session=session, poll_interval_seconds=0.1, clock=lambda: _NOW)

    result = await poller.poll()

    assert len(result) == 3
    assert [e.event_key for e in result] == ["m1", "m2", "m3"]

    got = [GmailMessageItem.from_payload(e.payload) for e in result]
    expected = [
        GmailMessageItem(
            message_id="m1",
            subject="Q3 budget",
            sender="jane@example.com",
            received_at=datetime.fromtimestamp(1751457600000 / 1000, tz=UTC),
            body_text="Hi, budget attached.",
        ),
        GmailMessageItem(
            message_id="m2",
            subject="Lunch?",
            sender="bob@example.com",
            received_at=datetime.fromtimestamp(1751461200000 / 1000, tz=UTC),
            body_text="Lunch at noon?",  # text/plain preferred over text/html
        ),
        GmailMessageItem(
            message_id="m3",
            subject="",
            sender="newsletter@example.com",
            received_at=datetime.fromtimestamp(1751464800000 / 1000, tz=UTC),
            body_text="<p>only html here</p>",  # html fallback, kept as-is
        ),
    ]
    assert got == expected
    # every payload key round-trips exactly through the models.py wire helpers (field-by-field)
    for item, source_event in zip(got, result, strict=True):
        assert GmailMessageItem.from_payload(item.to_payload()) == item
        assert set(source_event.payload) == {
            "message_id",
            "subject",
            "sender",
            "received_at",
            "body_text",
        }

    # structural no-write guarantee (Q-65 ruling 2): the fake session exposes ONLY .get.
    assert not hasattr(session, "post")
    assert not hasattr(session, "put")
    assert not hasattr(session, "patch")
    assert not hasattr(session, "delete")
    assert len(session.calls) == 4  # 1 list + 3 per-message gets
    assert session.calls[0]["params"]["q"].startswith("in:inbox after:")


# ------------------------------------------------------------------------------------------ AC2


def test_ac2_poller_module_never_references_a_capability_registry_or_googleapiclient() -> None:
    """Transport-only (Q-65 ruling 2): no ``Registry.register`` call, no cog-worx capability
    import, and no ``googleapiclient`` import anywhere in the poller module's source — the
    no-write guarantee is structural (only ``.get`` exists on the session Protocol, proven by
    the other AC2 test below); this is the belt-and-suspenders import/call-site check."""
    assert "Registry.register" not in _POLLER_SRC
    assert "import cogworx" not in _POLLER_SRC
    assert "from cogworx" not in _POLLER_SRC
    assert "import googleapiclient" not in _POLLER_SRC
    assert "from googleapiclient" not in _POLLER_SRC


async def test_ac2_poller_never_issues_a_draft_or_send_call_structurally() -> None:
    """The fake session's only method is ``.get`` — there is no draft/send method for the
    poller to call even if it tried."""
    session = _FakeSession(list_response=_FakeResponse(200, {"messages": []}))
    poller = GmailPoller(session=session, poll_interval_seconds=0.1, clock=lambda: _NOW)
    result = await poller.poll()
    assert result == []
    assert not hasattr(session, "post")
    assert not hasattr(session, "put")
    assert not hasattr(session, "patch")
    assert not hasattr(session, "delete")


# ------------------------------------------------------------------------------------------ AC3


def test_ac3_scope_guard_rejects_compose_and_accepts_readonly() -> None:
    assert GMAIL_SCOPES == ("https://www.googleapis.com/auth/gmail.readonly",)
    with pytest.raises(ScopeViolationError):
        assert_gmail_readonly_scopes(
            [*GMAIL_SCOPES, "https://www.googleapis.com/auth/gmail.compose"]
        )
    assert_gmail_readonly_scopes(list(GMAIL_SCOPES))  # does not raise


# ------------------------------------------------------------------------------------------ AC4


def _connection_error_session() -> _FakeSession:
    return _FakeSession(exception=requests.exceptions.ConnectionError("boom"))


def _http_error_session(status: int) -> _FakeSession:
    return _FakeSession(list_response=_FakeResponse(status, {"messages": []}))


def _malformed_missing_payload_session() -> _FakeSession:
    return _FakeSession(
        list_response=_FakeResponse(200, {"messages": [{"id": "bad1"}]}),
        message_responses={"bad1": _FakeResponse(200, {"id": "bad1"})},  # no "payload" key
    )


def _malformed_bad_internal_date_session() -> _FakeSession:
    raw = {
        "id": "bad2",
        "internalDate": "not-a-number",
        "payload": {"mimeType": "text/plain", "headers": [], "body": {"data": _b64("x")}},
    }
    return _FakeSession(
        list_response=_FakeResponse(200, {"messages": [{"id": "bad2"}]}),
        message_responses={"bad2": _FakeResponse(200, raw)},
    )


def _per_message_get_error_session() -> _FakeSession:
    return _FakeSession(
        list_response=_FakeResponse(200, {"messages": [{"id": "m1"}]}),
        message_responses={"m1": _FakeResponse(500, {})},
    )


@pytest.mark.parametrize(
    "make_session",
    [
        _connection_error_session,
        lambda: _http_error_session(401),
        lambda: _http_error_session(403),
        lambda: _http_error_session(500),
        lambda: _http_error_session(503),
        _malformed_missing_payload_session,
        _malformed_bad_internal_date_session,
        _per_message_get_error_session,
    ],
    ids=[
        "connection_error",
        "http_401",
        "http_403",
        "http_500",
        "http_503",
        "malformed_missing_payload_key",
        "malformed_bad_internal_date",
        "per_message_get_error",
    ],
)
async def test_ac4_transient_and_malformed_errors_degrade_to_empty_never_raise(
    make_session: Callable[[], _FakeSession], caplog: pytest.LogCaptureFixture
) -> None:
    session = make_session()
    poller = GmailPoller(session=session, poll_interval_seconds=0.1, clock=lambda: _NOW)

    with caplog.at_level(logging.WARNING):
        result = await poller.poll()  # MUST NOT raise

    assert result == []
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("gmail" in r.getMessage() for r in warnings)


async def test_ac4_same_message_two_polls_identical_key_single_admission_via_registry() -> None:
    list_body = {"messages": [{"id": "msg-dup"}]}
    msg = _plain_message(
        "msg-dup", subject="Repeat", sender="jane@example.com", body="repeat body",
        internal_date_ms=1751457600000,
    )

    # Direct poll()-level proof: the SAME message on two separate polls yields the same
    # event_key, and the canonical TK-12 derivation of that key is identical both times.
    direct_session = _FakeSession(
        list_response=_FakeResponse(200, list_body),
        message_responses={"msg-dup": _FakeResponse(200, msg)},
    )
    direct_poller = GmailPoller(
        session=direct_session, poll_interval_seconds=0.01, clock=lambda: _NOW
    )
    first = await direct_poller.poll()
    second = await direct_poller.poll()
    assert len(first) == 1
    assert len(second) == 1
    assert first[0].event_key == second[0].event_key == "msg-dup"
    assert derive_key("gmail", first[0].event_key) == derive_key("gmail", second[0].event_key)

    # Registry-level proof: mapped through the REAL SourceRegistry against a fake Enqueuer that
    # dedupes by idempotency_key — only the first occurrence is admitted.
    session = _FakeSession(
        list_response=_FakeResponse(200, list_body),
        message_responses={"msg-dup": _FakeResponse(200, msg)},
    )
    poller = GmailPoller(session=session, poll_interval_seconds=0.01, clock=lambda: _NOW)
    enqueuer = _DedupingEnqueuer()
    registry = SourceRegistry(enqueuer)
    registry.register(poller)

    await registry.start()
    try:
        await _wait_until(lambda: len(session.calls) >= 4)  # at least 2 polls x (1 list + 1 get)
    finally:
        await registry.stop()

    assert len(enqueuer.admitted) == 1
    assert enqueuer.admitted[0].idempotency_key == derive_key("gmail", "msg-dup")
