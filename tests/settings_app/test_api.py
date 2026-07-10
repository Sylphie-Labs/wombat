"""TK-197 acceptance criteria — wombat.settings_app, the Electron renderer's CONFIG backend
(EP-32, DEC-31/32).

AC1 (auth + read shape): ``test_missing_token_is_401``, ``test_wrong_token_is_401``,
    ``test_tokened_get_settings_returns_current_values_and_key_presence_booleans``,
    ``test_no_response_ever_echoes_a_stored_key``.
AC2 (write paths + validation):
    ``test_put_settings_updates_only_sent_fields_and_preserves_the_rest``,
    ``test_put_key_lands_in_the_store``, ``test_put_settings_out_of_vocab_value_is_422``,
    ``test_put_settings_unknown_key_is_422``, ``test_put_key_store_error_is_5xx_without_the_key``.
AC3 (structural): ``test_importing_api_never_imports_bootstrap_or_runtime``,
    ``test_bind_host_constant_is_loopback_only``, ``test_subprocess_handshake_and_smoke``.
Lock-step drift test: ``test_mirror_model_field_set_matches_app_editable_fields``,
    ``test_mirror_model_literal_vocab_matches_wombat_config``.
"""

from __future__ import annotations

import json
import subprocess
import sys
import typing
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from wombat.config import APP_EDITABLE_FIELDS, WombatConfig
from wombat.settings_app.api import BIND_HOST, SettingsUpdate, create_app
from wombat.voice.key_store import VoiceKeyStoreError

TOKEN = "the-test-token"


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


def _client(tmp_path: Path, *, key_store: object | None = None) -> TestClient:
    store = key_store if key_store is not None else _FakeVoiceKeyStore()
    app = create_app(tmp_path / "wombat.settings.json", store, TOKEN)  # type: ignore[arg-type]
    return TestClient(app)


# --- AC1: auth ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "json_body"),
    [
        ("GET", "/settings", None),
        ("PUT", "/settings", {}),
        ("PUT", "/keys/fish", {"key": "abc"}),
    ],
)
def test_missing_token_is_401(
    tmp_path: Path, method: str, path: str, json_body: dict[str, object] | None
) -> None:
    client = _client(tmp_path)
    response = client.request(method, path, json=json_body)
    assert response.status_code == 401


@pytest.mark.parametrize(
    ("method", "path", "json_body"),
    [
        ("GET", "/settings", None),
        ("PUT", "/settings", {}),
        ("PUT", "/keys/fish", {"key": "abc"}),
    ],
)
def test_wrong_token_is_401(
    tmp_path: Path, method: str, path: str, json_body: dict[str, object] | None
) -> None:
    client = _client(tmp_path)
    response = client.request(
        method, path, json=json_body, headers={"X-Wombat-Token": "not-the-token"}
    )
    assert response.status_code == 401


def test_tokened_get_settings_returns_current_values_and_key_presence_booleans(
    tmp_path: Path,
) -> None:
    settings_path = tmp_path / "wombat.settings.json"
    settings_path.write_text(
        json.dumps({"wombat_assistant_name": "Jeeves", "unrelated_key": "kept-elsewhere"}),
        encoding="utf-8",
    )
    store = _FakeVoiceKeyStore(initial={"elevenlabs": "super-secret-key"})
    app = create_app(settings_path, store, TOKEN)
    client = TestClient(app)

    response = client.get("/settings", headers={"X-Wombat-Token": TOKEN})

    assert response.status_code == 200
    body = response.json()
    assert body["settings"]["wombat_assistant_name"] == "Jeeves"
    assert set(body["settings"]) == set(APP_EDITABLE_FIELDS)
    assert body["keys"] == {"elevenlabs": True, "deepgram": False, "fish": False}


def test_no_response_ever_echoes_a_stored_key(tmp_path: Path) -> None:
    store = _FakeVoiceKeyStore(initial={"elevenlabs": "super-secret-key"})
    app = create_app(tmp_path / "wombat.settings.json", store, TOKEN)
    client = TestClient(app)

    response = client.get("/settings", headers={"X-Wombat-Token": TOKEN})

    assert "super-secret-key" not in response.text


# --- AC2: writes + validation -----------------------------------------------------------------


def test_put_settings_updates_only_sent_fields_and_preserves_the_rest(tmp_path: Path) -> None:
    settings_path = tmp_path / "wombat.settings.json"
    settings_path.write_text(
        json.dumps({"wombat_assistant_name": "Old Name", "unrelated_key": "kept-verbatim"}),
        encoding="utf-8",
    )
    app = create_app(settings_path, _FakeVoiceKeyStore(), TOKEN)
    client = TestClient(app)

    response = client.put(
        "/settings",
        json={"wombat_assistant_name": "New Name"},
        headers={"X-Wombat-Token": TOKEN},
    )

    assert response.status_code == 200
    on_disk = json.loads(settings_path.read_text(encoding="utf-8"))
    assert on_disk["wombat_assistant_name"] == "New Name"
    assert on_disk["unrelated_key"] == "kept-verbatim"


def test_put_key_lands_in_the_store(tmp_path: Path) -> None:
    store = _FakeVoiceKeyStore()
    app = create_app(tmp_path / "wombat.settings.json", store, TOKEN)
    client = TestClient(app)

    response = client.put(
        "/keys/fish", json={"key": "a-fish-key"}, headers={"X-Wombat-Token": TOKEN}
    )

    assert response.status_code == 200
    assert store.get("fish") == "a-fish-key"


def test_put_settings_out_of_vocab_value_is_422(tmp_path: Path) -> None:
    client = _client(tmp_path)
    response = client.put(
        "/settings",
        json={"wombat_stt_provider": "not-a-real-provider"},
        headers={"X-Wombat-Token": TOKEN},
    )
    assert response.status_code == 422


def test_put_settings_unknown_key_is_422(tmp_path: Path) -> None:
    client = _client(tmp_path)
    response = client.put(
        "/settings", json={"not_an_admitted_field": "x"}, headers={"X-Wombat-Token": TOKEN}
    )
    assert response.status_code == 422


def test_put_settings_wombat_voice_enabled_round_trips(tmp_path: Path) -> None:
    """TK-224 (Q-111(b)): the newly-admitted bool field writes and reads back."""
    client = _client(tmp_path)
    response = client.put(
        "/settings", json={"wombat_voice_enabled": True}, headers={"X-Wombat-Token": TOKEN}
    )
    assert response.status_code == 200
    assert response.json()["settings"]["wombat_voice_enabled"] is True

    get_response = client.get("/settings", headers={"X-Wombat-Token": TOKEN})
    assert get_response.json()["settings"]["wombat_voice_enabled"] is True


def test_put_settings_wombat_voice_enabled_non_bool_is_422(tmp_path: Path) -> None:
    client = _client(tmp_path)
    response = client.put(
        "/settings",
        json={"wombat_voice_enabled": ["not", "a", "bool"]},
        headers={"X-Wombat-Token": TOKEN},
    )
    assert response.status_code == 422


def test_put_key_unknown_provider_is_404_or_422(tmp_path: Path) -> None:
    client = _client(tmp_path)
    response = client.put(
        "/keys/not-a-provider", json={"key": "abc"}, headers={"X-Wombat-Token": TOKEN}
    )
    assert response.status_code in (404, 422)


def test_put_key_blank_body_is_422(tmp_path: Path) -> None:
    client = _client(tmp_path)
    response = client.put("/keys/fish", json={"key": "   "}, headers={"X-Wombat-Token": TOKEN})
    assert response.status_code == 422


def test_put_key_store_error_is_5xx_without_the_key(tmp_path: Path) -> None:
    app = create_app(tmp_path / "wombat.settings.json", _RaisingSetStore(), TOKEN)
    client = TestClient(app)

    response = client.put(
        "/keys/fish", json={"key": "a-secret-value"}, headers={"X-Wombat-Token": TOKEN}
    )

    assert 500 <= response.status_code < 600
    assert "a-secret-value" not in response.text


# --- AC3: structural ---------------------------------------------------------------------------


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
