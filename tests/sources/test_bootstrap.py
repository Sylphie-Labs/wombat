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
import os
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.abc import MetaPathFinder
from importlib.machinery import ModuleSpec
from pathlib import Path
from types import ModuleType
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
import pytest
from google.auth.exceptions import RefreshError
from google_auth_oauthlib.flow import InstalledAppFlow
from pydantic import SecretStr

import wombat.integrations.gcal.session as gcal_session_module
import wombat.integrations.gmail.session as gmail_session_module
import wombat.sources.bootstrap as sources_bootstrap_module
from wombat.calendar.models import CalendarEvent
from wombat.chat_turns import ChatTurnStore
from wombat.chat_turns import ensure_schema as ensure_chat_turns_schema
from wombat.config import ConfigurationError, WombatConfig
from wombat.external_store import ExternalItemStore
from wombat.external_store import ensure_schema as ensure_external_items_schema
from wombat.integrations.gmail.models import GmailMessageItem
from wombat.integrations.gmail.triage import load_triage_rules
from wombat.persona.live import LivePersona
from wombat.persona.matrix import DEFAULT_MATRIX
from wombat.queue import EnqueueResult, QueueItem
from wombat.sources.base import SourceEvent
from wombat.sources.bootstrap import (
    DEFAULT_ASR_POLL_INTERVAL_SECONDS,
    DEFAULT_FEEDBACK_POLL_INTERVAL_SECONDS,
    DEFAULT_GCAL_POLL_INTERVAL_SECONDS,
    DEFAULT_GMAIL_POLL_INTERVAL_SECONDS,
    build_brief_fetches,
    build_chat_turn_sink,
    build_external_item_sink,
    build_source_registry,
)
from wombat.sources.registry import SourceRegistry

_TZ = ZoneInfo("America/Chicago")
_NOW = datetime(2026, 7, 2, 12, 0, tzinfo=UTC)

_DSN = os.environ.get("WOMBAT_TEST_PG_DSN")

_requires_pg = pytest.mark.skipif(
    not _DSN,
    reason=(
        "WOMBAT_TEST_PG_DSN is not set — skipping the TK-245 real-ExternalItemStore sink test. "
        "Start one with:\n"
        "  docker run --rm -d -p 5433:5432 -e POSTGRES_PASSWORD=wombat postgres:16\n"
        "then export WOMBAT_TEST_PG_DSN=postgresql://postgres:wombat@localhost:5433/postgres"
    ),
)


def _utc_now() -> datetime:
    return _NOW


# --------------------------------------------------------------------------------------- config


def _make_config(
    *,
    client_id: str | None = None,
    client_secret: str | None = None,
    asr_drop_dir: str | None = None,
) -> WombatConfig:
    return WombatConfig(
        deepseek_api_key=SecretStr("unused-in-this-test"),
        deepseek_base_url="https://unused.example",
        google_oauth_client_id=client_id,
        google_oauth_client_secret=SecretStr(client_secret) if client_secret is not None else None,
        wombat_asr_drop_dir=asr_drop_dir,
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


class _BlockedFinder(MetaPathFinder):
    """A meta-path finder that fails the import of one named module (and its submodules)."""

    def __init__(self, blocked: str) -> None:
        self._blocked = blocked

    def find_spec(
        self, fullname: str, path: Sequence[str] | None, target: ModuleType | None = None
    ) -> ModuleSpec | None:
        if fullname == self._blocked or fullname.startswith(f"{self._blocked}."):
            raise ModuleNotFoundError(f"No module named {fullname!r} (simulated absence, TK-202)")
        return None


def _simulate_absent(monkeypatch: pytest.MonkeyPatch, module_name: str) -> None:
    """Simulate ``module_name`` being genuinely not installed, regardless of whether it actually
    is on this machine (TK-202/Q-103): evict any cached import AND install a meta-path finder
    ahead of the real one so any subsequent import raises ``ModuleNotFoundError``."""
    monkeypatch.delitem(sys.modules, module_name, raising=False)
    monkeypatch.setattr(sys, "meta_path", [_BlockedFinder(module_name), *sys.meta_path])


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


# --------------------------------------------------------- TK-253 (DEC-49): expired stored token


def test_gcal_source_not_wired_when_stored_credential_fails_to_refresh(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """AC2: client creds configured AND a stored gcal token present, but the token is expired/
    revoked (session factory's ``get_credentials`` raises) — degrades exactly like the no-stored-
    token branch (source skipped, no exception), WARNING names the gcal re-consent command."""
    consent_calls = _assert_never_triggers_consent(monkeypatch)

    class _FailingCalendarAuth:
        def __init__(self, *, config: WombatConfig, token_store: Any = None) -> None:
            pass

        def get_credentials(self) -> _FakeCredentials:
            raise RefreshError("stored gcal token is expired/revoked")  # type: ignore[no-untyped-call]

    monkeypatch.setattr(gcal_session_module, "CalendarAuth", _FailingCalendarAuth)
    config = _make_config(**_CONFIGURED)

    with caplog.at_level(logging.WARNING):
        registry = build_source_registry(
            config,
            _FakeEnqueuer(),
            tz=_TZ,
            clock=_utc_now,
            gcal_token_store=_FakeTokenStore(initial="expired-token"),
            gmail_token_store=_FakeTokenStore(initial=None),
        )

    assert not _is_registered(registry, "gcal")
    assert "gcal source not wired" in caplog.text
    assert "python -m wombat.integrations.gcal.auth" in caplog.text
    assert consent_calls == []


def test_gmail_source_not_wired_when_stored_credential_fails_to_refresh(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """AC2: same as above for the gmail source builder."""
    consent_calls = _assert_never_triggers_consent(monkeypatch)

    class _FailingGmailAuth:
        def __init__(self, *, config: WombatConfig, token_store: Any = None) -> None:
            pass

        def get_credentials(self) -> _FakeCredentials:
            raise RefreshError("stored gmail token is expired/revoked")  # type: ignore[no-untyped-call]

    monkeypatch.setattr(gmail_session_module, "GmailAuth", _FailingGmailAuth)
    config = _make_config(**_CONFIGURED)

    with caplog.at_level(logging.WARNING):
        registry = build_source_registry(
            config,
            _FakeEnqueuer(),
            tz=_TZ,
            clock=_utc_now,
            gcal_token_store=_FakeTokenStore(initial=None),
            gmail_token_store=_FakeTokenStore(initial="expired-token"),
        )

    assert not _is_registered(registry, "gmail")
    assert "gmail source not wired" in caplog.text
    assert "python -m wombat.integrations.gmail.auth" in caplog.text
    assert consent_calls == []


def test_build_brief_fetches_shares_the_gmail_expired_token_skip_decision(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """AC2: ``build_brief_fetches`` reuses the SAME ``_build_gmail_poller`` extraction (TK-96) —
    an expired stored gmail token degrades it to the same raising placeholder as no-token-at-all,
    boot continues Google-less rather than propagating the RefreshError."""
    consent_calls = _assert_never_triggers_consent(monkeypatch)

    class _FailingGmailAuth:
        def __init__(self, *, config: WombatConfig, token_store: Any = None) -> None:
            pass

        def get_credentials(self) -> _FakeCredentials:
            raise RefreshError("stored gmail token is expired/revoked")  # type: ignore[no-untyped-call]

    monkeypatch.setattr(gmail_session_module, "GmailAuth", _FailingGmailAuth)
    config = _make_config(**_CONFIGURED)

    with caplog.at_level(logging.WARNING):
        fetches = build_brief_fetches(
            config,
            tz=_TZ,
            clock=_utc_now,
            gcal_token_store=_FakeTokenStore(initial=None),
            gmail_token_store=_FakeTokenStore(initial="expired-token"),
        )

    with pytest.raises(ConfigurationError):
        fetches.fetch_gmail()
    assert "python -m wombat.integrations.gmail.auth" in caplog.text
    assert consent_calls == []


# ---------------------------------------------------------------------- TK-162/Q-97: asr lesion


def test_asr_source_not_wired_when_drop_dir_unset(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """AC2 (lesion): WOMBAT_ASR_DROP_DIR unset -> one loud skip log, no 'asr' source
    registered, and the gcal/gmail paths are entirely unaffected (both still skip loudly on
    their own missing config, exactly as the pre-existing suite already proves)."""
    consent_calls = _assert_never_triggers_consent(monkeypatch)
    config = _make_config()  # no client id/secret, no asr drop dir at all

    with caplog.at_level(logging.WARNING):
        registry = build_source_registry(
            config,
            _FakeEnqueuer(),
            tz=_TZ,
            clock=_utc_now,
            gcal_token_store=_FakeTokenStore(initial=None),
            gmail_token_store=_FakeTokenStore(initial=None),
        )

    assert not _is_registered(registry, "asr")
    assert not _is_registered(registry, "gcal")
    assert not _is_registered(registry, "gmail")
    assert "WOMBAT_ASR_DROP_DIR" in caplog.text
    assert consent_calls == []


def test_asr_source_not_wired_when_faster_whisper_not_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A configured drop dir alone is not enough: faster-whisper is simulated absent (TK-202/
    Q-103 — the optional ``[voice]`` extra, never a core dep, but MAY be installed on a dev/
    operator checkout), so ``_maybe_register_asr`` catches the real ``ImportError`` from
    constructing ``FasterWhisperTranscriber`` and skips loudly — never raises, never registers,
    no import error escapes anywhere in this call."""
    consent_calls = _assert_never_triggers_consent(monkeypatch)
    _simulate_absent(monkeypatch, "faster_whisper")
    config = _make_config(asr_drop_dir=str(tmp_path))

    with caplog.at_level(logging.WARNING):
        registry = build_source_registry(
            config,
            _FakeEnqueuer(),
            tz=_TZ,
            clock=_utc_now,
            gcal_token_store=_FakeTokenStore(initial=None),
            gmail_token_store=_FakeTokenStore(initial=None),
        )

    assert not _is_registered(registry, "asr")
    assert "faster-whisper" in caplog.text.lower()
    assert consent_calls == []


# --------------------------------------------------------------------- TK-212: persona hook wiring


def _wire_spy_asr_source(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Monkeypatch ``sources.bootstrap``'s ``ASRSource``/``build_transcriber`` names so
    ``_maybe_register_asr`` constructs a spy instead of a real ``ASRSource`` (no faster-whisper
    needed) — returns the dict the spy populates with its constructor kwargs (mutated in place,
    read after the call)."""
    captured_kwargs: dict[str, Any] = {}

    class _SpyASRSource:
        id: str = "asr"

        def __init__(self, **kwargs: Any) -> None:
            captured_kwargs.update(kwargs)
            self.poll_interval_seconds = kwargs.get("poll_interval_seconds", 1.0)

        async def start(self) -> None:
            return None

        async def stop(self) -> None:
            return None

        async def poll(self) -> list[SourceEvent]:
            return []

    monkeypatch.setattr(sources_bootstrap_module, "ASRSource", _SpyASRSource)
    monkeypatch.setattr(sources_bootstrap_module, "build_transcriber", lambda config: object())
    return captured_kwargs


def test_build_source_registry_threads_live_persona_and_speak_into_asr_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC4: a supplied ``live_persona`` (plus ``speak``) threads through to a non-``None``
    ``command_hook`` on the constructed ``ASRSource``."""
    consent_calls = _assert_never_triggers_consent(monkeypatch)
    captured_kwargs = _wire_spy_asr_source(monkeypatch)
    config = _make_config(asr_drop_dir=str(tmp_path))
    live_persona = LivePersona(DEFAULT_MATRIX, "Steward")  # store-less (TK-243), fully in-memory
    speak_calls: list[str] = []

    registry = build_source_registry(
        config,
        _FakeEnqueuer(),
        tz=_TZ,
        clock=_utc_now,
        gcal_token_store=_FakeTokenStore(initial=None),
        gmail_token_store=_FakeTokenStore(initial=None),
        live_persona=live_persona,
        speak=speak_calls.append,
    )

    assert _is_registered(registry, "asr")
    assert captured_kwargs["command_hook"] is not None
    assert consent_calls == []


def test_build_source_registry_defaults_construct_asr_source_with_no_command_hook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC4: the ``live_persona``/``speak`` defaults (``None``) construct today's ``ASRSource``
    exactly — ``command_hook`` stays ``None``, no interception wired at all."""
    consent_calls = _assert_never_triggers_consent(monkeypatch)
    captured_kwargs = _wire_spy_asr_source(monkeypatch)
    config = _make_config(asr_drop_dir=str(tmp_path))

    registry = build_source_registry(
        config,
        _FakeEnqueuer(),
        tz=_TZ,
        clock=_utc_now,
        gcal_token_store=_FakeTokenStore(initial=None),
        gmail_token_store=_FakeTokenStore(initial=None),
    )

    assert _is_registered(registry, "asr")
    assert captured_kwargs["command_hook"] is None
    assert consent_calls == []


# --------------------------------------------------------------------- TK-280: turn_hook wiring


def test_build_source_registry_threads_turn_hook_straight_through_to_asr_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TK-280 (DEC-60c server half): a supplied ``turn_hook`` reaches the constructed
    ``ASRSource`` unchanged -- ``build_source_registry`` does no branching of its own on it."""
    consent_calls = _assert_never_triggers_consent(monkeypatch)
    captured_kwargs = _wire_spy_asr_source(monkeypatch)
    config = _make_config(asr_drop_dir=str(tmp_path))

    def turn_hook(event_key: str, transcript: str, captured_at: str) -> None:
        raise AssertionError("never called by this test -- identity-through-wiring only")

    registry = build_source_registry(
        config,
        _FakeEnqueuer(),
        tz=_TZ,
        clock=_utc_now,
        gcal_token_store=_FakeTokenStore(initial=None),
        gmail_token_store=_FakeTokenStore(initial=None),
        turn_hook=turn_hook,
    )

    assert _is_registered(registry, "asr")
    assert captured_kwargs["turn_hook"] is turn_hook
    assert consent_calls == []


def test_build_source_registry_defaults_construct_asr_source_with_no_turn_hook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TK-280: the ``turn_hook`` default (``None``) constructs today's ``ASRSource`` exactly --
    no ledger side effect wired at all."""
    consent_calls = _assert_never_triggers_consent(monkeypatch)
    captured_kwargs = _wire_spy_asr_source(monkeypatch)
    config = _make_config(asr_drop_dir=str(tmp_path))

    registry = build_source_registry(
        config,
        _FakeEnqueuer(),
        tz=_TZ,
        clock=_utc_now,
        gcal_token_store=_FakeTokenStore(initial=None),
        gmail_token_store=_FakeTokenStore(initial=None),
    )

    assert _is_registered(registry, "asr")
    assert captured_kwargs["turn_hook"] is None
    assert consent_calls == []


# ----------------------------------------------------------------- TK-289: context_hook wiring


def test_build_source_registry_threads_context_hook_straight_through_to_asr_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TK-289 (DEC-64 gap A, half 2): a supplied ``context_hook`` reaches the constructed
    ``ASRSource`` unchanged -- ``build_source_registry`` does no branching of its own on it."""
    consent_calls = _assert_never_triggers_consent(monkeypatch)
    captured_kwargs = _wire_spy_asr_source(monkeypatch)
    config = _make_config(asr_drop_dir=str(tmp_path))

    def context_hook() -> dict[str, str]:
        raise AssertionError("never called by this test -- identity-through-wiring only")

    registry = build_source_registry(
        config,
        _FakeEnqueuer(),
        tz=_TZ,
        clock=_utc_now,
        gcal_token_store=_FakeTokenStore(initial=None),
        gmail_token_store=_FakeTokenStore(initial=None),
        context_hook=context_hook,
    )

    assert _is_registered(registry, "asr")
    assert captured_kwargs["context_hook"] is context_hook
    assert consent_calls == []


def test_build_source_registry_defaults_construct_asr_source_with_no_context_hook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TK-289: the ``context_hook`` default (``None``) constructs today's ``ASRSource`` exactly --
    no payload stamping wired at all."""
    consent_calls = _assert_never_triggers_consent(monkeypatch)
    captured_kwargs = _wire_spy_asr_source(monkeypatch)
    config = _make_config(asr_drop_dir=str(tmp_path))

    registry = build_source_registry(
        config,
        _FakeEnqueuer(),
        tz=_TZ,
        clock=_utc_now,
        gcal_token_store=_FakeTokenStore(initial=None),
        gmail_token_store=_FakeTokenStore(initial=None),
    )

    assert _is_registered(registry, "asr")
    assert captured_kwargs["context_hook"] is None
    assert consent_calls == []


def test_build_source_registry_wires_asr_at_2s_while_others_stay_at_300s(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TK-282 (DEC-60d): with every source configured and using ITS OWN default poll interval
    (no explicit override), asr is constructed at the new 2.0s cadence while gcal/gmail/feedback
    stay at their pre-existing 300.0s cadence — asserted on the actual constructed source
    instances, not just the module-level constants."""
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
    _wire_spy_asr_source(monkeypatch)

    config = WombatConfig(
        deepseek_api_key=SecretStr("unused-in-this-test"),
        deepseek_base_url="https://unused.example",
        google_oauth_client_id=_CONFIGURED["client_id"],
        google_oauth_client_secret=SecretStr(_CONFIGURED["client_secret"]),
        wombat_asr_drop_dir=str(tmp_path),
        wombat_feedback_file=str(tmp_path / "feedback.jsonl"),
    )

    registry = build_source_registry(
        config,
        _FakeEnqueuer(),
        tz=_TZ,
        clock=_utc_now,
        gcal_token_store=_FakeTokenStore(initial="gcal-token"),
        gmail_token_store=_FakeTokenStore(initial="gmail-token"),
    )

    assert registry._sources["gcal"].poll_interval_seconds == DEFAULT_GCAL_POLL_INTERVAL_SECONDS
    assert registry._sources["gmail"].poll_interval_seconds == DEFAULT_GMAIL_POLL_INTERVAL_SECONDS
    assert (
        registry._sources["feedback"].poll_interval_seconds
        == DEFAULT_FEEDBACK_POLL_INTERVAL_SECONDS
    )
    assert registry._sources["asr"].poll_interval_seconds == DEFAULT_ASR_POLL_INTERVAL_SECONDS
    assert DEFAULT_ASR_POLL_INTERVAL_SECONDS == 2.0
    assert DEFAULT_GCAL_POLL_INTERVAL_SECONDS == 300.0
    assert DEFAULT_GMAIL_POLL_INTERVAL_SECONDS == 300.0
    assert DEFAULT_FEEDBACK_POLL_INTERVAL_SECONDS == 300.0
    assert consent_calls == []


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


# --------------------------------------------------------------- TK-245: the store sink ---------


def test_build_source_registry_sink_is_none_without_an_external_item_store() -> None:
    """AC3 (DSN-less half): no ``external_item_store`` given -> the registry carries no sink at
    all — poll behavior is byte-unchanged."""
    config = _make_config()
    registry = build_source_registry(
        config,
        _FakeEnqueuer(),
        tz=_TZ,
        clock=_utc_now,
        gcal_token_store=_FakeTokenStore(initial=None),
        gmail_token_store=_FakeTokenStore(initial=None),
    )
    assert registry._sink is None


def test_build_source_registry_threads_a_sink_when_an_external_item_store_is_given() -> None:
    """AC3 (DSN-full half): an ``external_item_store`` given -> the registry carries a sink."""
    config = _make_config()
    store = ExternalItemStore("postgresql://fake-host/fake-db")  # lazy — never connects here
    registry = build_source_registry(
        config,
        _FakeEnqueuer(),
        tz=_TZ,
        clock=_utc_now,
        gcal_token_store=_FakeTokenStore(initial=None),
        gmail_token_store=_FakeTokenStore(initial=None),
        external_item_store=store,
    )
    assert registry._sink is not None


@_requires_pg
def test_tk245_ac1_sink_persists_whitelisted_projections_and_skips_reply_events() -> None:
    """AC1: fake gcal+gmail ``SourceEvent``s (the gmail one carrying a SECRET-MARKER body) fed
    through ``build_external_item_sink`` land keyed (source, item_key); the gcal payload round-
    trips ``CalendarEvent.from_payload``; the gmail payload carries EXACTLY the five whitelisted
    keys; the SECRET-MARKER and the ``body_text`` key appear NOWHERE in the stored payloads; a
    ``reply:``-prefixed event produces NO row."""
    assert _DSN is not None
    with psycopg.connect(_DSN) as conn:
        ensure_external_items_schema(conn)
        with conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE wombat_external_items")
        conn.commit()

    store = ExternalItemStore(_DSN)
    try:
        secret_marker = "SECRET-MARKER-2f9c8e"
        gmail_rules = load_triage_rules()
        sink = build_external_item_sink(store, gmail_rules=gmail_rules, clock=lambda: _NOW)

        gcal_event = SourceEvent(
            event_key="evt1",
            payload=CalendarEvent(
                event_id="evt1",
                title="Standup",
                start=datetime(2026, 7, 2, 9, 0, tzinfo=UTC),
                end=datetime(2026, 7, 2, 9, 30, tzinfo=UTC),
                all_day=False,
            ).to_payload(),
        )
        message = GmailMessageItem(
            message_id="m1",
            subject="hi",
            sender="a@example.com",
            received_at=datetime(2026, 7, 2, 8, 0, tzinfo=UTC),
            body_text=f"do not persist me: {secret_marker}",
        )
        gmail_event = SourceEvent(event_key="m1", payload=message.to_payload())
        reply_event = SourceEvent(
            event_key="reply:m1", payload={"quoted_excerpt": secret_marker}
        )

        sink("gcal", [gcal_event])
        sink("gmail", [gmail_event, reply_event])

        gcal_rows = store.get_recent("gcal", limit=10)
        assert [row["item_key"] for row in gcal_rows] == ["evt1"]
        assert CalendarEvent.from_payload(gcal_rows[0]["payload"]).event_id == "evt1"

        gmail_rows = store.get_recent("gmail", limit=10)
        assert [row["item_key"] for row in gmail_rows] == ["m1"]  # reply: event skipped
        assert set(gmail_rows[0]["payload"].keys()) == {
            "message_id",
            "subject",
            "sender",
            "received_at",
            "priority_band",
        }

        with psycopg.connect(_DSN) as conn, conn.cursor() as cur:
            cur.execute("SELECT payload::text FROM wombat_external_items")
            texts = [row[0] for row in cur.fetchall()]
        assert not any("body_text" in text for text in texts)
        assert not any(secret_marker in text for text in texts)
    finally:
        store.close()


# --------------------------------------------------------------- TK-295: the chat-turn sink -----


class _RaisingChatTurnStore(ChatTurnStore):
    """A real ``ChatTurnStore`` subclass (never opens a connection — ``record_turn`` is fully
    overridden to raise) mirroring the ``_RecordingScratchpadStore``/``_RecordingExternalItemStore``
    precedent in ``tests/unit/test_runtime.py``."""

    def __init__(self) -> None:
        super().__init__("postgresql://fake-host/fake-db")

    def record_turn(self, text: str, voice: bool, captured_at: datetime) -> None:
        raise RuntimeError("boom")


class _InMemoryChatTurnStore(ChatTurnStore):
    """A real ``ChatTurnStore`` subclass (never opens a connection — ``record_turn`` is fully
    overridden to record in-memory) mirroring ``_RaisingChatTurnStore`` above; used to observe
    which turns actually reach the store."""

    def __init__(self) -> None:
        super().__init__("postgresql://fake-host/fake-db")
        self.recorded: list[tuple[str, bool, datetime]] = []

    def record_turn(self, text: str, voice: bool, captured_at: datetime) -> None:
        self.recorded.append((text, voice, captured_at))


@dataclass
class _OneShotChatSource:
    """A minimal ``InputSource`` that yields ONE chat ``SourceEvent`` on its first ``poll()``,
    then empty forever after — enough to drive one registry poll iteration end-to-end."""

    id: str = "chat"
    poll_interval_seconds: float = 0.01

    def __post_init__(self) -> None:
        self._fired = False

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def poll(self) -> list[SourceEvent]:
        if self._fired:
            return []
        self._fired = True
        return [
            SourceEvent(
                event_key="chat1",
                payload={"item_kind": "chat", "text": "hello", "received_at": _NOW.isoformat()},
            )
        ]


def test_build_source_registry_sink_is_external_only_without_a_chat_turn_store() -> None:
    """No ``chat_turn_store`` given -> the composed sink is EXACTLY the external-item sink (the
    SAME ``build_external_item_sink`` closure, never wrapped by ``_compose_sinks``'s ``composed``
    wrapper) — byte-identical to today's behavior. ``build_external_item_sink``'s inner closure
    is named ``sink``; ``_compose_sinks``'s wrapper is named ``composed`` — a distinct name here
    would mean an (unwanted) wrapper is in play."""
    config = _make_config()
    store = ExternalItemStore("postgresql://fake-host/fake-db")  # lazy — never connects here
    registry = build_source_registry(
        config,
        _FakeEnqueuer(),
        tz=_TZ,
        clock=_utc_now,
        gcal_token_store=_FakeTokenStore(initial=None),
        gmail_token_store=_FakeTokenStore(initial=None),
        external_item_store=store,
    )
    assert registry._sink is not None
    assert registry._sink.__name__ == "sink"


def test_build_source_registry_threads_a_chat_turn_sink_when_a_chat_turn_store_is_given() -> None:
    """A ``chat_turn_store`` given (no ``external_item_store``) -> the registry carries a sink —
    mirrors the TK-245 external-item-only threading test."""
    config = _make_config()
    chat_store = ChatTurnStore("postgresql://fake-host/fake-db")  # lazy — never connects here
    registry = build_source_registry(
        config,
        _FakeEnqueuer(),
        tz=_TZ,
        clock=_utc_now,
        gcal_token_store=_FakeTokenStore(initial=None),
        gmail_token_store=_FakeTokenStore(initial=None),
        chat_turn_store=chat_store,
    )
    assert registry._sink is not None


@_requires_pg
def test_ac2_composed_sink_records_exactly_the_two_chat_turns_and_skips_gmail_gcal() -> None:
    """AC2: a registry built with BOTH the external-item sink AND the chat-turn sink composed --
    a typed chat event, a voice-turn event, and a gcal event polled through the SAME composed
    callable -- records EXACTLY the two chat turns with the right text/voice/captured_at; the
    gcal event records nothing into wombat_chat_turns; the external-item sink's own write is
    byte-identical (still lands the gcal row)."""
    assert _DSN is not None
    with psycopg.connect(_DSN) as conn:
        ensure_chat_turns_schema(conn)
        ensure_external_items_schema(conn)
        with conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE wombat_chat_turns")
            cur.execute("TRUNCATE TABLE wombat_external_items")
        conn.commit()

    chat_store = ChatTurnStore(_DSN)
    external_store = ExternalItemStore(_DSN)
    try:
        external_sink = build_external_item_sink(external_store, gmail_rules=None, clock=_utc_now)
        chat_sink = build_chat_turn_sink(chat_store, clock=_utc_now)

        def composed(source_id: str, events: list[SourceEvent]) -> None:
            external_sink(source_id, events)
            chat_sink(source_id, events)

        typed_chat_event = SourceEvent(
            event_key="chat1",
            payload={
                "item_kind": "chat",
                "text": "typed hello",
                "received_at": "2026-07-29T12:00:00+00:00",
            },
        )
        voice_chat_event = SourceEvent(
            event_key="chat2",
            payload={
                "item_kind": "chat",
                "voice_turn": True,
                "transcript": "spoken hello",
                "captured_at": "2026-07-29T13:00:00+00:00",
            },
        )
        gcal_event = SourceEvent(
            event_key="evt1",
            payload=CalendarEvent(
                event_id="evt1",
                title="Standup",
                start=datetime(2026, 7, 2, 9, 0, tzinfo=UTC),
                end=datetime(2026, 7, 2, 9, 30, tzinfo=UTC),
                all_day=False,
            ).to_payload(),
        )

        composed("chat", [typed_chat_event, voice_chat_event])
        composed("gcal", [gcal_event])

        rows = chat_store.turns_since(datetime(2026, 7, 1, tzinfo=UTC))
        assert [row["text"] for row in rows] == ["typed hello", "spoken hello"]
        assert [row["voice"] for row in rows] == [False, True]

        gcal_rows = external_store.get_recent("gcal", limit=10)
        assert [row["item_key"] for row in gcal_rows] == ["evt1"]
    finally:
        chat_store.close()
        external_store.close()


def test_tk308_malformed_captured_at_on_one_event_still_records_the_next_event(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """TK-308: event 1 is chat with a garbage ``received_at`` -- ``datetime.fromisoformat`` raises
    INSIDE the per-event try, so it can no longer escape the sink and starve the rest of the
    batch. Exactly ONE WARNING for event 1, no exception escapes, and event 2 (well-formed chat)
    still reaches ``record_turn``."""
    store = _InMemoryChatTurnStore()
    sink = build_chat_turn_sink(store, clock=_utc_now)

    bad_event = SourceEvent(
        event_key="chat-bad",
        payload={"item_kind": "chat", "text": "garbled", "received_at": "not-a-timestamp"},
    )
    good_event = SourceEvent(
        event_key="chat-good",
        payload={
            "item_kind": "chat",
            "text": "hello",
            "received_at": "2026-07-29T12:00:00+00:00",
        },
    )

    caplog.set_level(logging.WARNING, logger="wombat.sources.bootstrap")
    sink("chat", [bad_event, good_event])  # must not raise

    assert [t[0] for t in store.recorded] == ["hello"]

    warnings = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "chat-turn parse-or-store failed" in r.message
    ]
    assert len(warnings) == 1
    assert warnings[0].args == ("chat",)


async def test_ac3_raising_chat_turn_store_logs_one_warning_and_event_still_enqueues(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC3: a ``ChatTurnStore`` whose ``record_turn`` raises -> ONE WARNING, the event still
    enqueues -- the ledger can never block a turn."""
    raising_store = _RaisingChatTurnStore()
    sink = build_chat_turn_sink(raising_store, clock=_utc_now)
    enqueuer = _FakeEnqueuer()
    registry = SourceRegistry(enqueuer, sink=sink)
    registry.register(_OneShotChatSource())

    caplog.set_level(logging.WARNING, logger="wombat.sources.bootstrap")
    await registry.start()
    try:
        await _wait_until(lambda: len(enqueuer.items) >= 1)
    finally:
        await registry.stop()

    assert len(enqueuer.items) == 1
    warnings = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "chat-turn parse-or-store failed" in r.message
    ]
    assert len(warnings) == 1
