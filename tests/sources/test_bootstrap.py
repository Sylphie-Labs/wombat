"""TK-16 acceptance criteria — build_source_registry (Q-61/Q-67).

CI tests are mocked, ZERO network: ``CalendarAuth``/``GmailAuth`` and ``AuthorizedSession`` are
faked at the module boundary inside ``wombat.integrations.{gcal,gmail}.session`` (mirrors
``tests/integrations/{gcal,gmail}/test_session.py``), and ``google_auth_oauthlib.flow.
InstalledAppFlow.from_client_config`` is monkeypatched to fail the test if EVER invoked — the
runnable proof that ``build_source_registry`` never triggers interactive OAuth consent at boot.

  AC (empty boot): zero configured -> empty, working registry, no raise, no consent.
  AC (independent presence): each source registers independently on its OWN
      client-creds-present AND stored-token-present check (gcal/gmail SHARE one client id/
      secret per Q-65 ruling 2 — so "one configured" is expressed via token presence, not
      client creds).
  AC (absent creds/token -> loud skip): a loud log names the missing piece; no exception; the
      registry returned is still usable.
  AC (polls end-to-end): a registry built with both sources present polls the fake-sessioned
      CalendarPoller/GmailPoller and both sources' events reach the injected enqueuer.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from google_auth_oauthlib.flow import InstalledAppFlow
from pydantic import SecretStr

import wombat.integrations.gcal.session as gcal_session_module
import wombat.integrations.gmail.session as gmail_session_module
from wombat.config import ConfigurationError, WombatConfig
from wombat.queue import EnqueueResult, QueueItem
from wombat.sources.base import SourceEvent
from wombat.sources.bootstrap import build_brief_fetches, build_source_registry
from wombat.sources.registry import SourceRegistry

_TZ = ZoneInfo("America/Chicago")
_NOW = datetime(2026, 7, 2, 12, 0, tzinfo=UTC)


def _utc_now() -> datetime:
    return _NOW


# --------------------------------------------------------------------------------------- config


def _make_config(
    *, client_id: str | None = None, client_secret: str | None = None
) -> WombatConfig:
    return WombatConfig(
        deepseek_api_key=SecretStr("unused-in-this-test"),
        deepseek_base_url="https://unused.example",
        google_oauth_client_id=client_id,
        google_oauth_client_secret=SecretStr(client_secret) if client_secret is not None else None,
    )


_CONFIGURED = {"client_id": "test-client-id", "client_secret": "test-client-secret"}


# ------------------------------------------------------------------------------------- fakes


class _FakeTokenStore:
    def __init__(self, *, initial: str | None = None) -> None:
        self._value = initial

    def load(self) -> str | None:
        return self._value

    def save(self, token: str) -> None:
        self._value = token

    def clear(self) -> None:
        self._value = None


class _FakeEnqueuer:
    def __init__(self) -> None:
        self.items: list[QueueItem] = []

    def enqueue(self, item: QueueItem) -> EnqueueResult:
        self.items.append(item)
        return EnqueueResult.QUEUED


@dataclass
class _StubSource:
    """Minimal InputSource stub used ONLY to probe whether an id is already taken —
    ``SourceRegistry.register`` raises ``ValueError`` on a duplicate id (registry.py), so a
    successful/failed probe-register is a runnable proof of presence/absence."""

    id: str
    poll_interval_seconds: float = 999.0

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def poll(self) -> list[SourceEvent]:
        return []


def _is_registered(registry: SourceRegistry, source_id: str) -> bool:
    try:
        registry.register(_StubSource(id=source_id))
    except ValueError:
        return True
    return False


class _FakeCredentials:
    """A sentinel standing in for a real ``Credentials`` — identity is all these tests need."""


class _FakeAuthorizedSession:
    """Captures what it was constructed with; ``.get`` is overridden per-test to serve canned
    JSON, matching the ``_CalendarSession``/``_GmailSession`` Protocol exactly (GET-only)."""

    def __init__(self, credentials: Any) -> None:
        self.credentials = credentials
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, *, params: dict[str, str], timeout: float) -> _FakeApiResponse:
        raise NotImplementedError  # overridden per-test via subclassing/monkeypatch


class _FakeApiResponse:
    def __init__(self, body: dict[str, Any]) -> None:
        self._body = body

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._body


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def _one_calendar_event_session(credentials: Any) -> _FakeAuthorizedSession:
    session = _FakeAuthorizedSession(credentials)

    def _get(url: str, *, params: dict[str, str], timeout: float) -> _FakeApiResponse:
        session.calls.append({"url": url, "params": params, "timeout": timeout})
        return _FakeApiResponse(
            {
                "items": [
                    {
                        "id": "evt1",
                        "summary": "Standup",
                        "start": {"dateTime": "2026-07-02T09:00:00-05:00"},
                        "end": {"dateTime": "2026-07-02T09:30:00-05:00"},
                    }
                ]
            }
        )

    session.get = _get  # type: ignore[method-assign]
    return session


def _one_gmail_message_session(credentials: Any) -> _FakeAuthorizedSession:
    session = _FakeAuthorizedSession(credentials)
    message = {
        "id": "m1",
        "internalDate": "1751457600000",
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "Subject", "value": "hi"},
                {"name": "From", "value": "a@example.com"},
            ],
            "body": {"data": _b64("body text")},
        },
    }

    def _get(url: str, *, params: dict[str, str], timeout: float) -> _FakeApiResponse:
        session.calls.append({"url": url, "params": params, "timeout": timeout})
        if url.endswith("/messages"):
            return _FakeApiResponse({"messages": [{"id": "m1"}]})
        return _FakeApiResponse(message)

    session.get = _get  # type: ignore[method-assign]
    return session


def _assert_never_triggers_consent(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Monkeypatches BOTH auth modules' ``InstalledAppFlow.from_client_config`` to record any
    invocation — callers assert the returned list stays empty (the Q-61 no-boot-time-consent
    guarantee, proven directly rather than merely inferred from the token-presence gate)."""
    calls: list[object] = []

    def _fake_from_client_config(*args: object, **kwargs: object) -> object:
        calls.append((args, kwargs))
        raise AssertionError("interactive OAuth consent must never be triggered at boot")

    monkeypatch.setattr(InstalledAppFlow, "from_client_config", _fake_from_client_config)
    return calls


async def _wait_until(
    predicate: Callable[[], bool], *, timeout: float = 2.0, interval: float = 0.005
) -> None:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() >= deadline:
            raise AssertionError(f"condition not met within {timeout}s")
        await asyncio.sleep(interval)


# --------------------------------------------------------------------------- zero-configured AC


def test_zero_configured_sources_boots_empty_registry_no_raise_no_consent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    consent_calls = _assert_never_triggers_consent(monkeypatch)
    config = _make_config()  # no client id/secret at all

    registry = build_source_registry(
        config,
        _FakeEnqueuer(),
        tz=_TZ,
        clock=_utc_now,
        gcal_token_store=_FakeTokenStore(initial=None),
        gmail_token_store=_FakeTokenStore(initial=None),
    )

    assert isinstance(registry, SourceRegistry)
    assert not _is_registered(registry, "gcal")
    assert not _is_registered(registry, "gmail")
    assert consent_calls == []


def test_zero_configured_sources_even_with_a_stored_token_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Config absent entirely still skips BOTH sources even if a stray token exists in the
    (fake) vault — client-creds absence alone is decisive, never overridden by a token."""
    consent_calls = _assert_never_triggers_consent(monkeypatch)
    config = _make_config()

    registry = build_source_registry(
        config,
        _FakeEnqueuer(),
        tz=_TZ,
        clock=_utc_now,
        gcal_token_store=_FakeTokenStore(initial="stray-token"),
        gmail_token_store=_FakeTokenStore(initial="stray-token"),
    )

    assert not _is_registered(registry, "gcal")
    assert not _is_registered(registry, "gmail")
    assert consent_calls == []


# ------------------------------------------------------------------------ independent presence


def test_one_configured_source_with_stored_token_registers_only_that_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    consent_calls = _assert_never_triggers_consent(monkeypatch)

    class _FakeCalendarAuth:
        def __init__(self, *, config: WombatConfig, token_store: Any = None) -> None:
            pass

        def get_credentials(self) -> _FakeCredentials:
            return _FakeCredentials()

    monkeypatch.setattr(gcal_session_module, "CalendarAuth", _FakeCalendarAuth)
    monkeypatch.setattr(
        gcal_session_module,
        "AuthorizedSession",
        lambda creds: _one_calendar_event_session(creds),
    )

    config = _make_config(**_CONFIGURED)
    registry = build_source_registry(
        config,
        _FakeEnqueuer(),
        tz=_TZ,
        clock=_utc_now,
        gcal_token_store=_FakeTokenStore(initial="a-real-looking-token"),
        gmail_token_store=_FakeTokenStore(initial=None),  # gmail: creds present, NO token
    )

    assert _is_registered(registry, "gcal")
    assert not _is_registered(registry, "gmail")
    assert consent_calls == []


def test_configured_creds_but_no_token_skips_loudly_no_exception_no_consent(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    consent_calls = _assert_never_triggers_consent(monkeypatch)
    config = _make_config(**_CONFIGURED)

    with caplog.at_level(logging.WARNING):
        registry = build_source_registry(
            config,
            _FakeEnqueuer(),
            tz=_TZ,
            clock=_utc_now,
            gcal_token_store=_FakeTokenStore(initial=None),
            gmail_token_store=_FakeTokenStore(initial=None),
        )

    assert not _is_registered(registry, "gcal")
    assert not _is_registered(registry, "gmail")
    assert consent_calls == []
    assert "no stored credential" in caplog.text
    assert "python -m wombat.integrations.gcal.auth" in caplog.text
    assert "python -m wombat.integrations.gmail.auth" in caplog.text


def test_no_client_creds_skips_loudly_naming_the_missing_config(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    consent_calls = _assert_never_triggers_consent(monkeypatch)
    config = _make_config()

    with caplog.at_level(logging.WARNING):
        build_source_registry(
            config,
            _FakeEnqueuer(),
            tz=_TZ,
            clock=_utc_now,
        )

    assert consent_calls == []
    assert "GOOGLE_OAUTH_CLIENT_ID" in caplog.text or "not configured" in caplog.text


# ------------------------------------------------------------------------------ both + polling


async def test_both_configured_and_tokened_registers_both_and_polls_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    consent_calls = _assert_never_triggers_consent(monkeypatch)

    class _FakeCalendarAuth:
        def __init__(self, *, config: WombatConfig, token_store: Any = None) -> None:
            pass

        def get_credentials(self) -> _FakeCredentials:
            return _FakeCredentials()

    class _FakeGmailAuth:
        def __init__(self, *, config: WombatConfig, token_store: Any = None) -> None:
            pass

        def get_credentials(self) -> _FakeCredentials:
            return _FakeCredentials()

    monkeypatch.setattr(gcal_session_module, "CalendarAuth", _FakeCalendarAuth)
    monkeypatch.setattr(
        gcal_session_module, "AuthorizedSession", lambda creds: _one_calendar_event_session(creds)
    )
    monkeypatch.setattr(gmail_session_module, "GmailAuth", _FakeGmailAuth)
    monkeypatch.setattr(
        gmail_session_module, "AuthorizedSession", lambda creds: _one_gmail_message_session(creds)
    )

    config = _make_config(**_CONFIGURED)
    enqueuer = _FakeEnqueuer()
    registry = build_source_registry(
        config,
        enqueuer,
        tz=_TZ,
        clock=_utc_now,
        gcal_poll_interval_seconds=0.01,
        gmail_poll_interval_seconds=0.01,
        gcal_token_store=_FakeTokenStore(initial="gcal-token"),
        gmail_token_store=_FakeTokenStore(initial="gmail-token"),
    )

    assert _is_registered(registry, "gcal")
    assert _is_registered(registry, "gmail")

    await registry.start()
    try:
        await _wait_until(lambda: len(enqueuer.items) >= 2)
    finally:
        await registry.stop()

    keys = {item.idempotency_key for item in enqueuer.items}
    assert any("evt1" in k for k in keys)
    assert any("m1" in k for k in keys)
    assert registry.degraded_sources == frozenset()
    assert consent_calls == []


# ------------------------------------------------------------------ TK-96: build_brief_fetches


def test_build_brief_fetches_unwired_sources_raise_configuration_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Zero configured Google creds -> BOTH fetch callables are RAISING placeholders (TK-96) —
    never a network call, never an exception at build time (the raise is lazy, on first call)."""
    consent_calls = _assert_never_triggers_consent(monkeypatch)
    config = _make_config()  # no client id/secret at all

    fetches = build_brief_fetches(
        config,
        tz=_TZ,
        clock=_utc_now,
        gcal_token_store=_FakeTokenStore(initial=None),
        gmail_token_store=_FakeTokenStore(initial=None),
    )

    with pytest.raises(ConfigurationError):
        fetches.fetch_calendar()
    with pytest.raises(ConfigurationError):
        fetches.fetch_gmail()
    assert consent_calls == []


def test_build_brief_fetches_wired_sources_bind_the_real_poller_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both sources configured + tokened -> BOTH fetch callables are bound to the REAL poller's
    ``fetch_window``/``fetch_recent`` (TK-96) — the same wired/unwired decision
    ``build_source_registry`` makes, proven by a genuine (fake-sessioned) read succeeding."""
    consent_calls = _assert_never_triggers_consent(monkeypatch)

    class _FakeCalendarAuth:
        def __init__(self, *, config: WombatConfig, token_store: Any = None) -> None:
            pass

        def get_credentials(self) -> _FakeCredentials:
            return _FakeCredentials()

    class _FakeGmailAuth:
        def __init__(self, *, config: WombatConfig, token_store: Any = None) -> None:
            pass

        def get_credentials(self) -> _FakeCredentials:
            return _FakeCredentials()

    monkeypatch.setattr(gcal_session_module, "CalendarAuth", _FakeCalendarAuth)
    monkeypatch.setattr(
        gcal_session_module, "AuthorizedSession", lambda creds: _one_calendar_event_session(creds)
    )
    monkeypatch.setattr(gmail_session_module, "GmailAuth", _FakeGmailAuth)
    monkeypatch.setattr(
        gmail_session_module, "AuthorizedSession", lambda creds: _one_gmail_message_session(creds)
    )

    config = _make_config(**_CONFIGURED)
    fetches = build_brief_fetches(
        config,
        tz=_TZ,
        clock=_utc_now,
        gcal_token_store=_FakeTokenStore(initial="gcal-token"),
        gmail_token_store=_FakeTokenStore(initial="gmail-token"),
    )

    events = fetches.fetch_calendar()
    messages = fetches.fetch_gmail()

    assert [e.event_id for e in events] == ["evt1"]
    assert [m.message_id for m in messages] == ["m1"]
    assert consent_calls == []
