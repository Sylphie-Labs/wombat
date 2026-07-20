"""TK-197/TK-242 acceptance criteria — wombat.settings_app, the Electron renderer's CONFIG
backend (EP-32, DEC-31/32, DEC-43).

AC1 (auth + read shape, TK-242 pg round-trip): ``test_missing_token_is_401``,
    ``test_wrong_token_is_401``,
    ``test_tokened_get_settings_returns_current_values_and_key_presence_booleans``,
    ``test_no_response_ever_echoes_a_stored_key``,
    ``test_ac1_tokened_put_then_get_round_trips_over_real_pg`` (pg-gated).
AC2 (write paths + validation + TK-242 degrade posture):
    ``test_put_settings_updates_only_sent_fields_and_preserves_the_rest``,
    ``test_put_key_lands_in_the_store``, ``test_put_settings_out_of_vocab_value_is_422``,
    ``test_put_settings_unknown_key_is_422``, ``test_put_key_store_error_is_5xx_without_the_key``,
    ``test_get_settings_no_store_returns_all_null_with_flag``,
    ``test_put_settings_no_store_is_503``,
    ``test_get_settings_store_raises_degrades_to_all_null_with_flag``,
    ``test_put_settings_store_raises_is_503_with_fixed_detail_never_bare_500``.
AC3 (structural): ``test_importing_api_never_imports_bootstrap_or_runtime``,
    ``test_bind_host_constant_is_loopback_only``, ``test_subprocess_handshake_and_smoke``,
    ``test_api_module_has_no_file_read_write_helper``,
    ``test_main_invokes_import_legacy_settings_file_exactly_once``,
    ``test_subprocess_invokes_legacy_import_at_startup_over_real_pg`` (pg-gated, v2.58(a)
    chdir'd), ``test_subprocess_boots_degraded_when_dsn_absent``.
Lock-step drift test: ``test_mirror_model_field_set_matches_app_editable_fields``,
    ``test_mirror_model_literal_vocab_matches_wombat_config``.

TK-246 (DEC-45(e)): GET /external/calendar + GET /external/gmail — AC1
    ``test_ac1_external_routes_windowed_ordered_and_tokened_over_real_pg`` (pg-gated); AC2
    ``test_get_external_calendar_no_store_returns_empty_items_with_flag``,
    ``test_get_external_gmail_no_store_returns_empty_items_with_flag``,
    ``test_get_external_calendar_store_raises_degrades_to_empty_items_with_flag``,
    ``test_get_external_gmail_store_raises_degrades_to_empty_items_with_flag``; AC3
    ``test_no_non_get_method_is_routable_under_external``.

TK-256 (DEC-50): GET /google/status + POST /google/{service}/connect — AC1
    ``test_get_google_status_no_connections_returns_not_configured_for_both``,
    ``test_get_google_status_reads_per_service_status_from_the_manager``; AC2
    ``test_post_google_connect_no_connections_is_503``,
    ``test_post_google_connect_unknown_service_is_422``,
    ``test_post_google_connect_returns_202_reports_in_progress_then_connected``,
    ``test_second_post_google_connect_while_in_progress_is_409``,
    ``test_post_google_connect_raising_runner_surfaces_error_and_process_stays_up``.
"""

from __future__ import annotations

import inspect
import json
import os
import subprocess
import sys
import threading
import time
import typing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient

from wombat.config import APP_EDITABLE_FIELDS, WombatConfig
from wombat.external_store import ExternalItem, ExternalItemStore
from wombat.external_store import ensure_schema as ensure_external_items_schema
from wombat.settings_app.api import (
    _STORAGE_UNAVAILABLE_DETAIL,
    BIND_HOST,
    SettingsUpdate,
    create_app,
)
from wombat.settings_app.google_connect import GoogleConnectionManager, GoogleServiceConnection
from wombat.settings_store import SettingsStore, ensure_schema
from wombat.voice.key_store import VoiceKeyStoreError

TOKEN = "the-test-token"

_PG_DSN = os.environ.get("WOMBAT_TEST_PG_DSN")

_requires_pg = pytest.mark.skipif(
    not _PG_DSN,
    reason=(
        "WOMBAT_TEST_PG_DSN is not set — skipping settings API tests that require a real "
        "throwaway Postgres. Start one with:\n"
        "  docker run --rm -d -p 5433:5432 -e POSTGRES_PASSWORD=wombat postgres:16\n"
        "then export WOMBAT_TEST_PG_DSN=postgresql://postgres:wombat@localhost:5433/postgres"
    ),
)


class _FakeVoiceKeyStore:
    """In-memory fake (Q-57(a) parity) — unit tests never touch the real vault."""

    def __init__(self, *, initial: dict[str, str] | None = None) -> None:
        self._values = dict(initial or {})

    def get(self, provider: str) -> str | None:
        return self._values.get(provider)

    def set(self, provider: str, key: str) -> None:
        self._values[provider] = key

    def delete(self, provider: str) -> None:
        self._values.pop(provider, None)


class _RaisingSetStore:
    """A fake whose ``set`` always raises ``VoiceKeyStoreError`` — proves the 500 path never
    echoes the key (AC2)."""

    def get(self, provider: str) -> str | None:
        return None

    def set(self, provider: str, key: str) -> None:
        raise VoiceKeyStoreError("voice key vault write failed (wombat/voice-fish-api-key): boom")

    def delete(self, provider: str) -> None:
        raise AssertionError("not exercised")


class _FakeSettingsStore:
    """In-memory fake SettingsStore (Q-57(a) parity) — unit tests never touch real Postgres."""

    def __init__(self, *, initial: dict[str, object] | None = None) -> None:
        self._rows: dict[str, object] = dict(initial or {})

    def get_all(self) -> dict[str, object]:
        return dict(self._rows)

    def put(self, mapping: dict[str, object]) -> None:
        self._rows.update(mapping)


class _RaisingSettingsStore:
    """A fake whose every method raises — proves the DEC-43 read-only degrade posture (AC2):
    GET never 500s, PUT always 503s with the fixed detail."""

    def get_all(self) -> dict[str, object]:
        raise RuntimeError("simulated pg unreachable")

    def put(self, mapping: dict[str, object]) -> None:
        raise RuntimeError("simulated pg unreachable")


class _RaisingExternalItemStore:
    """A fake whose every read raises — proves the TK-246 read-only degrade posture (AC2)."""

    def get_window(self, source: str, start: object, end: object) -> list[dict[str, object]]:
        raise RuntimeError("simulated pg unreachable")

    def get_recent(self, source: str, limit: int) -> list[dict[str, object]]:
        raise RuntimeError("simulated pg unreachable")


def _client(
    *,
    key_store: object | None = None,
    store: object | None = None,
    external_store: object | None = None,
    google_connections: object | None = None,
) -> TestClient:
    voice_store = key_store if key_store is not None else _FakeVoiceKeyStore()
    settings_store = store if store is not None else _FakeSettingsStore()
    app = create_app(
        settings_store,  # type: ignore[arg-type]
        voice_store,  # type: ignore[arg-type]
        TOKEN,
        external_store,  # type: ignore[arg-type]
        google_connections,  # type: ignore[arg-type]
    )
    return TestClient(app)


# --- AC1: auth --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "json_body"),
    [
        ("GET", "/settings", None),
        ("PUT", "/settings", {}),
        ("PUT", "/keys/fish", {"key": "abc"}),
        ("GET", "/external/calendar", None),
        ("GET", "/external/gmail", None),
        ("GET", "/google/status", None),
        ("POST", "/google/gmail/connect", None),
    ],
)
def test_missing_token_is_401(
    method: str, path: str, json_body: dict[str, object] | None
) -> None:
    client = _client()
    response = client.request(method, path, json=json_body)
    assert response.status_code == 401


@pytest.mark.parametrize(
    ("method", "path", "json_body"),
    [
        ("GET", "/settings", None),
        ("PUT", "/settings", {}),
        ("PUT", "/keys/fish", {"key": "abc"}),
        ("GET", "/external/calendar", None),
        ("GET", "/external/gmail", None),
        ("GET", "/google/status", None),
        ("POST", "/google/gmail/connect", None),
    ],
)
def test_wrong_token_is_401(method: str, path: str, json_body: dict[str, object] | None) -> None:
    client = _client()
    response = client.request(
        method, path, json=json_body, headers={"X-Wombat-Token": "not-the-token"}
    )
    assert response.status_code == 401


def test_tokened_get_settings_returns_current_values_and_key_presence_booleans() -> None:
    store = _FakeSettingsStore(
        initial={"wombat_assistant_name": "Jeeves", "unrelated_key": "kept-elsewhere"}
    )
    voice_store = _FakeVoiceKeyStore(initial={"elevenlabs": "super-secret-key"})
    app = create_app(store, voice_store, TOKEN)  # type: ignore[arg-type]
    client = TestClient(app)

    response = client.get("/settings", headers={"X-Wombat-Token": TOKEN})

    assert response.status_code == 200
    body = response.json()
    assert body["settings"]["wombat_assistant_name"] == "Jeeves"
    assert set(body["settings"]) == set(APP_EDITABLE_FIELDS)
    assert body["keys"] == {"elevenlabs": True, "deepgram": False, "fish": False}
    assert body["storage_unavailable"] is False


def test_no_response_ever_echoes_a_stored_key() -> None:
    voice_store = _FakeVoiceKeyStore(initial={"elevenlabs": "super-secret-key"})
    client = _client(key_store=voice_store)

    response = client.get("/settings", headers={"X-Wombat-Token": TOKEN})

    assert "super-secret-key" not in response.text


@_requires_pg
def test_ac1_tokened_put_then_get_round_trips_over_real_pg() -> None:
    assert _PG_DSN is not None
    with psycopg.connect(_PG_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS wombat_settings CASCADE")
        conn.commit()
        ensure_schema(conn)
        conn.commit()

    store = SettingsStore(_PG_DSN)
    try:
        app = create_app(store, _FakeVoiceKeyStore(), TOKEN)
        client = TestClient(app)

        put_response = client.put(
            "/settings",
            json={"wombat_assistant_name": "Real PG Name"},
            headers={"X-Wombat-Token": TOKEN},
        )
        assert put_response.status_code == 200

        get_response = client.get("/settings", headers={"X-Wombat-Token": TOKEN})
        assert get_response.status_code == 200
        body = get_response.json()
        assert body["settings"]["wombat_assistant_name"] == "Real PG Name"
        assert body["storage_unavailable"] is False

        assert client.get("/settings").status_code == 401
        assert (
            client.get("/settings", headers={"X-Wombat-Token": "wrong"}).status_code == 401
        )
        for response in (put_response, get_response):
            assert "postgresql://" not in response.text
    finally:
        store.close()

    with psycopg.connect(_PG_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT value FROM wombat_settings WHERE key = %s", ("wombat_assistant_name",)
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] == "Real PG Name"


@_requires_pg
def test_ac1_external_routes_windowed_ordered_and_tokened_over_real_pg() -> None:
    """TK-246 AC1: a real store seeded with gcal rows inside/outside the window plus gmail rows —
    GET /external/calendar returns only in-window items ordered by occurs_at, GET /external/gmail
    returns recent items, and a missing/wrong token is 401 on both."""
    assert _PG_DSN is not None
    with psycopg.connect(_PG_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS wombat_external_items CASCADE")
        conn.commit()
        ensure_external_items_schema(conn)
        conn.commit()

    external_store = ExternalItemStore(_PG_DSN)
    try:
        now = datetime.now(UTC)
        external_store.upsert_many(
            "gcal",
            [
                ExternalItem(
                    item_key="in-window-later",
                    payload={"summary": "later", "event_id": "in-window-later"},
                    occurs_at=now + timedelta(hours=2),
                ),
                ExternalItem(
                    item_key="in-window-earlier",
                    payload={"summary": "earlier", "event_id": "in-window-earlier"},
                    occurs_at=now + timedelta(hours=1),
                ),
                ExternalItem(
                    item_key="out-of-window",
                    payload={"summary": "far future", "event_id": "out-of-window"},
                    occurs_at=now + timedelta(hours=1000),
                ),
            ],
            fetched_at=now,
        )
        external_store.upsert_many(
            "gmail",
            [
                ExternalItem(
                    item_key="msg-1",
                    payload={
                        "message_id": "msg-1",
                        "subject": "hi",
                        "sender": "a@example.com",
                        "received_at": now.isoformat(),
                        "priority_band": "NORMAL",
                    },
                    occurs_at=now,
                )
            ],
            fetched_at=now,
        )

        app = create_app(None, _FakeVoiceKeyStore(), TOKEN, external_store)
        client = TestClient(app)

        cal_response = client.get(
            "/external/calendar", headers={"X-Wombat-Token": TOKEN}
        )
        assert cal_response.status_code == 200
        cal_body = cal_response.json()
        assert cal_body["storage_unavailable"] is False
        assert [item["event_id"] for item in cal_body["items"]] == [
            "in-window-earlier",
            "in-window-later",
        ]

        gmail_response = client.get("/external/gmail", headers={"X-Wombat-Token": TOKEN})
        assert gmail_response.status_code == 200
        gmail_body = gmail_response.json()
        assert gmail_body["storage_unavailable"] is False
        assert [item["message_id"] for item in gmail_body["items"]] == ["msg-1"]

        assert client.get("/external/calendar").status_code == 401
        assert (
            client.get(
                "/external/calendar", headers={"X-Wombat-Token": "wrong"}
            ).status_code
            == 401
        )
        assert client.get("/external/gmail").status_code == 401
    finally:
        external_store.close()


# --- AC2: writes + validation + degrade posture ------------------------------------------------


def test_put_settings_updates_only_sent_fields_and_preserves_the_rest() -> None:
    store = _FakeSettingsStore(
        initial={"wombat_assistant_name": "Old Name", "unrelated_key": "kept-verbatim"}
    )
    app = create_app(store, _FakeVoiceKeyStore(), TOKEN)  # type: ignore[arg-type]
    client = TestClient(app)

    response = client.put(
        "/settings",
        json={"wombat_assistant_name": "New Name"},
        headers={"X-Wombat-Token": TOKEN},
    )

    assert response.status_code == 200
    rows = store.get_all()
    assert rows["wombat_assistant_name"] == "New Name"
    assert rows["unrelated_key"] == "kept-verbatim"


def test_put_key_lands_in_the_store() -> None:
    store = _FakeVoiceKeyStore()
    client = _client(key_store=store)

    response = client.put(
        "/keys/fish", json={"key": "a-fish-key"}, headers={"X-Wombat-Token": TOKEN}
    )

    assert response.status_code == 200
    assert store.get("fish") == "a-fish-key"


def test_put_settings_out_of_vocab_value_is_422() -> None:
    client = _client()
    response = client.put(
        "/settings",
        json={"wombat_stt_provider": "not-a-real-provider"},
        headers={"X-Wombat-Token": TOKEN},
    )
    assert response.status_code == 422


def test_put_settings_unknown_key_is_422() -> None:
    client = _client()
    response = client.put(
        "/settings", json={"not_an_admitted_field": "x"}, headers={"X-Wombat-Token": TOKEN}
    )
    assert response.status_code == 422


def test_put_settings_wombat_voice_enabled_round_trips() -> None:
    """TK-224 (Q-111(b)): the newly-admitted bool field writes and reads back."""
    client = _client()
    response = client.put(
        "/settings", json={"wombat_voice_enabled": True}, headers={"X-Wombat-Token": TOKEN}
    )
    assert response.status_code == 200
    assert response.json()["settings"]["wombat_voice_enabled"] is True

    get_response = client.get("/settings", headers={"X-Wombat-Token": TOKEN})
    assert get_response.json()["settings"]["wombat_voice_enabled"] is True


def test_put_settings_wombat_voice_enabled_non_bool_is_422() -> None:
    client = _client()
    response = client.put(
        "/settings",
        json={"wombat_voice_enabled": ["not", "a", "bool"]},
        headers={"X-Wombat-Token": TOKEN},
    )
    assert response.status_code == 422


def test_put_settings_wombat_ptt_binding_round_trips() -> None:
    """TK-275 (DEC-58 c/d): the newly-admitted str field writes and reads back."""
    client = _client()
    response = client.put(
        "/settings", json={"wombat_ptt_binding": "key:KeyK"}, headers={"X-Wombat-Token": TOKEN}
    )
    assert response.status_code == 200
    assert response.json()["settings"]["wombat_ptt_binding"] == "key:KeyK"

    get_response = client.get("/settings", headers={"X-Wombat-Token": TOKEN})
    assert get_response.json()["settings"]["wombat_ptt_binding"] == "key:KeyK"


def test_get_settings_wombat_ptt_binding_defaults_to_null_when_unset() -> None:
    """TK-275: unset means "" (unbound) at the WombatConfig layer, but the settings-table view
    (before any PUT) shows null like every other unset admitted field."""
    client = _client()
    response = client.get("/settings", headers={"X-Wombat-Token": TOKEN})
    assert response.json()["settings"]["wombat_ptt_binding"] is None


def test_put_key_unknown_provider_is_404_or_422() -> None:
    client = _client()
    response = client.put(
        "/keys/not-a-provider", json={"key": "abc"}, headers={"X-Wombat-Token": TOKEN}
    )
    assert response.status_code in (404, 422)


def test_put_key_blank_body_is_422() -> None:
    client = _client()
    response = client.put("/keys/fish", json={"key": "   "}, headers={"X-Wombat-Token": TOKEN})
    assert response.status_code == 422


def test_put_key_store_error_is_5xx_without_the_key() -> None:
    client = _client(key_store=_RaisingSetStore())

    response = client.put(
        "/keys/fish", json={"key": "a-secret-value"}, headers={"X-Wombat-Token": TOKEN}
    )

    assert 500 <= response.status_code < 600
    assert "a-secret-value" not in response.text


def test_get_settings_no_store_returns_all_null_with_flag() -> None:
    """TK-242: DSN absent at boot -> ``store=None`` -> GET degrades to 200, never 500."""
    app = create_app(None, _FakeVoiceKeyStore(), TOKEN)
    client = TestClient(app)

    response = client.get("/settings", headers={"X-Wombat-Token": TOKEN})

    assert response.status_code == 200
    body = response.json()
    assert body["storage_unavailable"] is True
    assert all(value is None for value in body["settings"].values())


def test_put_settings_no_store_is_503() -> None:
    app = create_app(None, _FakeVoiceKeyStore(), TOKEN)
    client = TestClient(app)

    response = client.put(
        "/settings", json={"wombat_assistant_name": "New"}, headers={"X-Wombat-Token": TOKEN}
    )

    assert response.status_code == 503
    assert response.json()["detail"] == _STORAGE_UNAVAILABLE_DETAIL


def test_get_settings_store_raises_degrades_to_all_null_with_flag() -> None:
    client = _client(store=_RaisingSettingsStore())

    response = client.get("/settings", headers={"X-Wombat-Token": TOKEN})

    assert response.status_code == 200
    body = response.json()
    assert body["storage_unavailable"] is True
    assert all(value is None for value in body["settings"].values())


def test_put_settings_store_raises_is_503_with_fixed_detail_never_bare_500() -> None:
    client = _client(store=_RaisingSettingsStore())

    response = client.put(
        "/settings", json={"wombat_assistant_name": "New"}, headers={"X-Wombat-Token": TOKEN}
    )

    assert response.status_code == 503
    assert response.status_code != 500
    assert response.json()["detail"] == _STORAGE_UNAVAILABLE_DETAIL
    assert "simulated pg unreachable" not in response.text


def test_get_external_calendar_no_store_returns_empty_items_with_flag() -> None:
    """TK-246: no ``external_store`` -> 200 with empty items + storage_unavailable true."""
    client = _client(external_store=None)

    response = client.get("/external/calendar", headers={"X-Wombat-Token": TOKEN})

    assert response.status_code == 200
    body = response.json()
    assert body["items"] == []
    assert body["storage_unavailable"] is True


def test_get_external_gmail_no_store_returns_empty_items_with_flag() -> None:
    client = _client(external_store=None)

    response = client.get("/external/gmail", headers={"X-Wombat-Token": TOKEN})

    assert response.status_code == 200
    body = response.json()
    assert body["items"] == []
    assert body["storage_unavailable"] is True


def test_get_external_calendar_store_raises_degrades_to_empty_items_with_flag() -> None:
    client = _client(external_store=_RaisingExternalItemStore())

    response = client.get("/external/calendar", headers={"X-Wombat-Token": TOKEN})

    assert response.status_code == 200
    body = response.json()
    assert body["items"] == []
    assert body["storage_unavailable"] is True


def test_get_external_gmail_store_raises_degrades_to_empty_items_with_flag() -> None:
    client = _client(external_store=_RaisingExternalItemStore())

    response = client.get("/external/gmail", headers={"X-Wombat-Token": TOKEN})

    assert response.status_code == 200
    body = response.json()
    assert body["items"] == []
    assert body["storage_unavailable"] is True


# --- TK-256 (DEC-50): GET /google/status + POST /google/{service}/connect ----------------------


class _NullAuth:
    """Never-called fake — proves the load()-None case never reaches the route's connections."""

    def get_credentials(self) -> object:
        raise AssertionError("get_credentials must not be called for a not_configured service")


class _OkAuth:
    def get_credentials(self) -> object:
        return object()


class _BlockingConnectAuth:
    """A fake whose ``get_credentials()`` blocks until the test releases it, then saves a token
    — proves POST /google/{service}/connect returns before the flow completes (CON-5) and that
    GET /google/status reflects in_progress while it's running."""

    def __init__(
        self, token_store: object, started: threading.Event, resume: threading.Event
    ) -> None:
        self._token_store = token_store
        self._started = started
        self._resume = resume

    def get_credentials(self) -> object:
        self._started.set()
        self._resume.wait(timeout=5)
        self._token_store.save("consented-token")  # type: ignore[attr-defined]
        return object()


class _RaisingConnectAuth:
    def get_credentials(self) -> object:
        raise RuntimeError("consent flow failed: user closed the browser")


class _InMemoryTokenStore:
    def __init__(self, initial: str | None = None) -> None:
        self.token = initial

    def load(self) -> str | None:
        return self.token

    def save(self, value: str) -> None:
        self.token = value

    def clear(self) -> None:
        self.token = None


def test_get_google_status_no_connections_returns_not_configured_for_both() -> None:
    """TK-256: create_app(google_connections=None) mirrors the store=None degrade — an honest
    not_configured for every service, never a crash."""
    client = _client(google_connections=None)

    response = client.get("/google/status", headers={"X-Wombat-Token": TOKEN})

    assert response.status_code == 200
    assert response.json() == {
        "gmail": {"status": "not_configured", "consent": "idle"},
        "gcal": {"status": "not_configured", "consent": "idle"},
    }


def test_get_google_status_reads_per_service_status_from_the_manager() -> None:
    manager = GoogleConnectionManager(
        {
            "gmail": GoogleServiceConnection(
                configured=False, token_store=_InMemoryTokenStore(), auth_factory=_NullAuth
            ),
            "gcal": GoogleServiceConnection(
                configured=True,
                token_store=_InMemoryTokenStore("stored-token"),
                auth_factory=_OkAuth,
            ),
        }
    )
    client = _client(google_connections=manager)

    response = client.get("/google/status", headers={"X-Wombat-Token": TOKEN})

    assert response.status_code == 200
    assert response.json() == {
        "gmail": {"status": "not_configured", "consent": "idle"},
        "gcal": {"status": "connected", "consent": "idle"},
    }


def test_post_google_connect_no_connections_is_503() -> None:
    client = _client(google_connections=None)

    response = client.post("/google/gmail/connect", headers={"X-Wombat-Token": TOKEN})

    assert response.status_code == 503


def test_post_google_connect_unknown_service_is_422() -> None:
    manager = GoogleConnectionManager(
        {
            "gmail": GoogleServiceConnection(
                configured=False, token_store=_InMemoryTokenStore(), auth_factory=_NullAuth
            ),
            "gcal": GoogleServiceConnection(
                configured=False, token_store=_InMemoryTokenStore(), auth_factory=_NullAuth
            ),
        }
    )
    client = _client(google_connections=manager)

    response = client.post("/google/outlook/connect", headers={"X-Wombat-Token": TOKEN})

    assert response.status_code == 422


def test_post_google_connect_returns_202_reports_in_progress_then_connected() -> None:
    token_store = _InMemoryTokenStore(None)
    started = threading.Event()
    resume = threading.Event()
    manager = GoogleConnectionManager(
        {
            "gmail": GoogleServiceConnection(
                configured=True,
                token_store=token_store,
                auth_factory=lambda: _BlockingConnectAuth(token_store, started, resume),
            ),
            "gcal": GoogleServiceConnection(
                configured=False, token_store=_InMemoryTokenStore(), auth_factory=_NullAuth
            ),
        }
    )
    client = _client(google_connections=manager)

    connect_response = client.post("/google/gmail/connect", headers={"X-Wombat-Token": TOKEN})
    assert connect_response.status_code == 202
    assert started.wait(timeout=5)

    in_progress = client.get("/google/status", headers={"X-Wombat-Token": TOKEN})
    assert in_progress.json()["gmail"]["consent"] == "in_progress"

    resume.set()
    deadline = time.monotonic() + 5
    body: dict[str, typing.Any] = {}
    while time.monotonic() < deadline:
        body = client.get("/google/status", headers={"X-Wombat-Token": TOKEN}).json()
        if body["gmail"] == {"status": "connected", "consent": "idle"}:
            break
        time.sleep(0.01)
    assert body["gmail"] == {"status": "connected", "consent": "idle"}


def test_second_post_google_connect_while_in_progress_is_409() -> None:
    token_store = _InMemoryTokenStore(None)
    started = threading.Event()
    resume = threading.Event()
    manager = GoogleConnectionManager(
        {
            "gmail": GoogleServiceConnection(
                configured=True,
                token_store=token_store,
                auth_factory=lambda: _BlockingConnectAuth(token_store, started, resume),
            ),
            "gcal": GoogleServiceConnection(
                configured=False, token_store=_InMemoryTokenStore(), auth_factory=_NullAuth
            ),
        }
    )
    client = _client(google_connections=manager)

    first = client.post("/google/gmail/connect", headers={"X-Wombat-Token": TOKEN})
    assert first.status_code == 202
    assert started.wait(timeout=5)

    second = client.post("/google/gmail/connect", headers={"X-Wombat-Token": TOKEN})
    assert second.status_code == 409

    resume.set()  # release the background thread before the test ends


def test_post_google_connect_raising_runner_surfaces_error_and_process_stays_up() -> None:
    manager = GoogleConnectionManager(
        {
            "gmail": GoogleServiceConnection(
                configured=True,
                token_store=_InMemoryTokenStore(None),
                auth_factory=_RaisingConnectAuth,
            ),
            "gcal": GoogleServiceConnection(
                configured=False, token_store=_InMemoryTokenStore(), auth_factory=_NullAuth
            ),
        }
    )
    client = _client(google_connections=manager)

    connect_response = client.post("/google/gmail/connect", headers={"X-Wombat-Token": TOKEN})
    assert connect_response.status_code == 202

    deadline = time.monotonic() + 5
    body: dict[str, typing.Any] = {}
    while time.monotonic() < deadline:
        body = client.get("/google/status", headers={"X-Wombat-Token": TOKEN}).json()
        if body["gmail"]["consent"] == "error":
            break
        time.sleep(0.01)
    assert body["gmail"]["consent"] == "error"
    # DEC-51: a concise, non-raw message — the raw exception text never reaches the payload.
    assert body["gmail"]["error"] == (
        "Google consent flow failed - see the application logs for details."
    )

    # the process stays up — a plain, unrelated request still works.
    still_up = client.get("/settings", headers={"X-Wombat-Token": TOKEN})
    assert still_up.status_code == 200


# --- AC3: structural ---------------------------------------------------------------------------


def test_no_non_get_method_is_routable_under_external() -> None:
    """TK-246 AC3: /external/ is strictly read-only — no PUT/POST/DELETE/PATCH route exists under
    it, proven structurally by iterating every registered route."""
    app = create_app(None, _FakeVoiceKeyStore(), TOKEN, None)
    for route in app.routes:
        path = getattr(route, "path", "")
        if not path.startswith("/external/"):
            continue
        methods: set[str] = getattr(route, "methods", set()) or set()
        assert methods <= {"GET", "HEAD"}, f"{path} exposes non-GET methods: {methods}"


def test_importing_api_never_imports_bootstrap_or_runtime() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys\n"
            "import wombat.settings_app.api\n"
            "assert 'wombat.bootstrap' not in sys.modules\n"
            "assert 'wombat.runtime' not in sys.modules\n"
            "print('OK')\n",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "OK"


def test_bind_host_constant_is_loopback_only() -> None:
    assert BIND_HOST == "127.0.0.1"


def test_api_module_has_no_file_read_write_helper() -> None:
    """TK-242 AC3: the file-path settings I/O (``_read_settings``/``_write_settings``, TK-235's
    atomic-write machinery) is gone — ``api.py`` never reads/writes a settings file directly."""
    import wombat.settings_app.api as api_module

    source = Path(api_module.__file__).read_text(encoding="utf-8")
    assert "_read_settings" not in source
    assert "_write_settings" not in source
    assert "tempfile" not in source


def test_main_invokes_import_legacy_settings_file_exactly_once() -> None:
    """TK-242 AC3: ``__main__`` invokes the DEC-44 opt-in import exactly once at startup — the
    second and last production call site (the first is ``wombat.runtime.serve()``)."""
    import wombat.settings_app.__main__ as main_module

    source = inspect.getsource(main_module.main)
    assert source.count("import_legacy_settings_file(") == 1


def test_subprocess_handshake_and_smoke(tmp_path: Path) -> None:
    proc = subprocess.Popen(
        [sys.executable, "-m", "wombat.settings_app"],
        cwd=tmp_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None
        line = proc.stdout.readline()
        handshake = json.loads(line)
        port = handshake["port"]
        token = handshake["token"]
        assert isinstance(port, int)
        assert isinstance(token, str) and token

        base_url = f"http://{BIND_HOST}:{port}"
        tokened = httpx.get(f"{base_url}/settings", headers={"X-Wombat-Token": token}, timeout=5)
        assert tokened.status_code == 200

        tokenless = httpx.get(f"{base_url}/settings", timeout=5)
        assert tokenless.status_code == 401
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def test_subprocess_boots_degraded_when_dsn_absent(tmp_path: Path) -> None:
    """TK-242: no ``WOMBAT_PG_DSN`` in env and no ``.env`` in ``tmp_path`` (a fresh, empty cwd,
    never the repo root) -> the process still boots in the read-only degrade posture instead of
    crashing: GET is 200 all-null + storage_unavailable, PUT is 503.

    TK-256 (DEC-50): the root ``tests/conftest.py`` fixture forces
    ``GOOGLE_OAUTH_CLIENT_ID``/``GOOGLE_OAUTH_CLIENT_SECRET`` to the empty string for every test
    (hermeticity) — that empty-string env override rides into this subprocess's ``env`` too, so
    it also proves ``__main__`` boots creds-less into DEC-50's ``not_configured`` degrade for
    both Google services, rather than crashing."""
    env = {k: v for k, v in os.environ.items() if k != "WOMBAT_PG_DSN"}
    proc = subprocess.Popen(
        [sys.executable, "-m", "wombat.settings_app"],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None
        line = proc.stdout.readline()
        handshake = json.loads(line)
        port = handshake["port"]
        token = handshake["token"]
        base_url = f"http://{BIND_HOST}:{port}"

        get_response = httpx.get(
            f"{base_url}/settings", headers={"X-Wombat-Token": token}, timeout=5
        )
        assert get_response.status_code == 200
        assert get_response.json()["storage_unavailable"] is True

        put_response = httpx.put(
            f"{base_url}/settings",
            json={"wombat_assistant_name": "New"},
            headers={"X-Wombat-Token": token},
            timeout=5,
        )
        assert put_response.status_code == 503

        google_status = httpx.get(
            f"{base_url}/google/status", headers={"X-Wombat-Token": token}, timeout=5
        )
        assert google_status.status_code == 200
        assert google_status.json() == {
            "gmail": {"status": "not_configured", "consent": "idle"},
            "gcal": {"status": "not_configured", "consent": "idle"},
        }
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


@_requires_pg
def test_subprocess_invokes_legacy_import_at_startup_over_real_pg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TK-242 AC3 + v2.58(a) ruling: over a real throwaway pg, chdir'd into ``tmp_path`` (never
    the repo root — the legacy-import file resolution must never see the operator's real
    ``wombat.settings.json``), the subprocess's ``ensure_schema`` + one-time
    ``import_legacy_settings_file`` actually run at startup: a legacy file in the cwd lands in
    ``wombat_settings`` and is renamed, and the API reflects it immediately."""
    assert _PG_DSN is not None
    with psycopg.connect(_PG_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS wombat_settings CASCADE")
        conn.commit()

    monkeypatch.chdir(tmp_path)
    settings_path = tmp_path / "wombat.settings.json"
    settings_path.write_text(
        json.dumps({"wombat_assistant_name": "Legacy Name"}), encoding="utf-8"
    )

    env = dict(os.environ)
    env["WOMBAT_PG_DSN"] = _PG_DSN
    proc = subprocess.Popen(
        [sys.executable, "-m", "wombat.settings_app"],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None
        line = proc.stdout.readline()
        handshake = json.loads(line)
        port = handshake["port"]
        token = handshake["token"]
        base_url = f"http://{BIND_HOST}:{port}"

        response = httpx.get(f"{base_url}/settings", headers={"X-Wombat-Token": token}, timeout=5)
        assert response.status_code == 200
        body = response.json()
        assert body["storage_unavailable"] is False
        assert body["settings"]["wombat_assistant_name"] == "Legacy Name"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)

    assert not settings_path.exists()
    assert (tmp_path / "wombat.settings.json.migrated").exists()


def test_subprocess_honors_keyring_service_env_override(tmp_path: Path) -> None:
    """TK-201 (Q-111(d)): WOMBAT_KEYRING_SERVICE, when set, is threaded into the
    KeyringVoiceKeyStore the subprocess constructs — the process still boots and serves
    normally (no crash, no behavior change visible from outside a keyring read), never
    touching the default "wombat" vault entry."""
    env = dict(os.environ)
    env["WOMBAT_KEYRING_SERVICE"] = f"wombat-test-{os.getpid()}"
    proc = subprocess.Popen(
        [sys.executable, "-m", "wombat.settings_app"],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None
        line = proc.stdout.readline()
        handshake = json.loads(line)
        port = handshake["port"]
        token = handshake["token"]

        base_url = f"http://{BIND_HOST}:{port}"
        response = httpx.get(f"{base_url}/settings", headers={"X-Wombat-Token": token}, timeout=5)
        assert response.status_code == 200
        assert response.json()["keys"] == {"elevenlabs": False, "deepgram": False, "fish": False}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


# --- Lock-step drift test -----------------------------------------------------------------------


def test_mirror_model_field_set_matches_app_editable_fields() -> None:
    assert set(SettingsUpdate.model_fields) == set(APP_EDITABLE_FIELDS)


def _literal_args(annotation: object) -> frozenset[object] | None:
    """Extract a ``Literal``'s value set from ``annotation``, whether it's a bare ``Literal[...]``
    or an ``Optional``/``X | None`` wrapping one; ``None`` if it isn't Literal-typed at all."""
    if typing.get_origin(annotation) is typing.Literal:
        return frozenset(typing.get_args(annotation))
    args = typing.get_args(annotation)
    non_none = [a for a in args if a is not type(None)]
    if len(non_none) == 1 and typing.get_origin(non_none[0]) is typing.Literal:
        return frozenset(typing.get_args(non_none[0]))
    return None


def test_mirror_model_literal_vocab_matches_wombat_config() -> None:
    for name in APP_EDITABLE_FIELDS:
        config_literal = _literal_args(WombatConfig.model_fields[name].annotation)
        mirror_literal = _literal_args(SettingsUpdate.model_fields[name].annotation)
        assert config_literal == mirror_literal, f"{name} vocabulary drifted"
