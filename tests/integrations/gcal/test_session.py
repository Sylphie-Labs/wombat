"""TK-16 acceptance criteria — make_calendar_session (Q-61, AC1).

CI test is mocked at the auth/credentials boundary, ZERO network: ``CalendarAuth`` and
``AuthorizedSession`` are both faked inside ``wombat.integrations.gcal.session`` so this test
proves the WIRING (the session is constructed from exactly the credentials CalendarAuth
provides) without touching real Google endpoints — mirrors the ``tests/integrations/gcal/
test_auth.py`` fake-token-store idiom.
"""

from __future__ import annotations

from typing import Any

from pydantic import SecretStr

from wombat.config import WombatConfig
from wombat.integrations.gcal import session as session_module
from wombat.integrations.gcal.session import make_calendar_session


def _make_config(
    *, client_id: str | None = "test-client-id", client_secret: str | None = "test-client-secret"
) -> WombatConfig:
    return WombatConfig(
        deepseek_api_key=SecretStr("unused-in-this-test"),
        deepseek_base_url="https://unused.example",
        google_oauth_client_id=client_id,
        google_oauth_client_secret=SecretStr(client_secret) if client_secret is not None else None,
    )


class _FakeTokenStore:
    def __init__(self, *, initial: str | None = None) -> None:
        self._value = initial

    def load(self) -> str | None:
        return self._value

    def save(self, token: str) -> None:
        self._value = token

    def clear(self) -> None:
        self._value = None


class _SentinelCredentials:
    """Stands in for a real ``google.oauth2.credentials.Credentials`` instance — identity is
    all this test needs, not shape."""


class _FakeAuthorizedSession:
    """Stands in for ``google.auth.transport.requests.AuthorizedSession`` — records exactly
    what it was constructed with, ZERO network/HTTP behavior."""

    def __init__(self, credentials: Any) -> None:
        self.credentials = credentials


def test_make_calendar_session_binds_authorizedsession_to_calendarauth_credentials(
    monkeypatch: Any,
) -> None:
    sentinel_creds = _SentinelCredentials()
    captured: dict[str, Any] = {}

    class _FakeCalendarAuth:
        def __init__(self, *, config: WombatConfig, token_store: Any = None) -> None:
            captured["config"] = config
            captured["token_store"] = token_store

        def get_credentials(self) -> _SentinelCredentials:
            captured["get_credentials_called"] = True
            return sentinel_creds

    monkeypatch.setattr(session_module, "CalendarAuth", _FakeCalendarAuth)
    monkeypatch.setattr(session_module, "AuthorizedSession", _FakeAuthorizedSession)

    config = _make_config()
    token_store = _FakeTokenStore(initial="irrelevant-serialized-token")

    result = make_calendar_session(config, token_store=token_store)

    # the ONE seam: AuthorizedSession is bound to EXACTLY the credentials CalendarAuth provided
    assert isinstance(result, _FakeAuthorizedSession)
    assert result.credentials is sentinel_creds
    assert captured["get_credentials_called"] is True
    # CalendarAuth was constructed with the same config/token_store this function received
    assert captured["config"] is config
    assert captured["token_store"] is token_store
