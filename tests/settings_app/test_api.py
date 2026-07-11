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
"""

from __future__ import annotations

import inspect
import json
import os
import subprocess
import sys
import typing
from pathlib import Path

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient

from wombat.config import APP_EDITABLE_FIELDS, WombatConfig
from wombat.settings_app.api import (
    _STORAGE_UNAVAILABLE_DETAIL,
    BIND_HOST,
    SettingsUpdate,
    create_app,
)
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


def _client(*, key_store: object | None = None, store: object | None = None) -> TestClient:
    voice_store = key_store if key_store is not None else _FakeVoiceKeyStore()
    settings_store = store if store is not None else _FakeSettingsStore()
    app = create_app(settings_store, voice_store, TOKEN)  # type: ignore[arg-type]
    return TestClient(app)


# --- AC1: auth --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "json_body"),
    [
        ("GET", "/settings", None),
        ("PUT", "/settings", {}),
        ("PUT", "/keys/fish", {"key": "abc"}),
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
    crashing: GET is 200 all-null + storage_unavailable, PUT is 503."""
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
