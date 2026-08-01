"""TK-186 — load_config: a .env-only DeepSeek setup boots (LIVE BUG fix).

The pre-pydantic ``os.environ`` REQUIRED_ENV check used to run before pydantic-settings ever
read the repo-root ``.env``, so an operator with credentials ONLY in ``.env`` (no exported env
vars) could never boot. These tests pin the fixed order: ``.env`` is a valid source, explicit
env vars still win over it, and the loud-boot message shape (first missing var, named) is
unchanged.
"""

from __future__ import annotations

import inspect
import logging
import os
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg
import pytest
from pydantic import SecretStr

from wombat import config as config_module
from wombat.config import (
    APP_EDITABLE_FIELDS,
    REQUIRED_ENV,
    ConfigurationError,
    WombatConfig,
    load_config,
    resolve_wombat_zone,
)
from wombat.persona.matrix import DEFAULT_MATRIX, Brevity, Humor, Warmth, matrix_from_config
from wombat.settings_store import SettingsStore, ensure_schema

_PG_DSN = os.environ.get("WOMBAT_TEST_PG_DSN")

_requires_pg = pytest.mark.skipif(
    not _PG_DSN,
    reason=(
        "WOMBAT_TEST_PG_DSN is not set — skipping settings-table-backed config tests. Start a "
        "throwaway pg with:\n"
        "  docker run --rm -d -p 5433:5432 -e POSTGRES_PASSWORD=wombat postgres:16\n"
        "then export WOMBAT_TEST_PG_DSN=postgresql://postgres:wombat@localhost:5433/postgres"
    ),
)


@pytest.fixture
def fresh_settings_table() -> None:
    """Drop + recreate ``wombat_settings`` on the throwaway pg, empty, for one test."""
    assert _PG_DSN is not None
    with psycopg.connect(_PG_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS wombat_settings CASCADE")
        conn.commit()
        ensure_schema(conn)


def _write_env_file(tmp_path: Path, *, api_key: str | None, base_url: str | None) -> None:
    lines = []
    if api_key is not None:
        lines.append(f"DEEPSEEK_API_KEY={api_key}")
    if base_url is not None:
        lines.append(f"DEEPSEEK_BASE_URL={base_url}")
    (tmp_path / ".env").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _clear_process_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in REQUIRED_ENV:
        monkeypatch.delenv(var, raising=False)


# --- AC1: .env-only setup boots ------------------------------------------------------------------


def test_load_config_populates_from_env_file_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_process_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    _write_env_file(tmp_path, api_key="sk-from-dotenv", base_url="https://dotenv.example.com")

    config = load_config()

    assert config.deepseek_api_key.get_secret_value() == "sk-from-dotenv"
    assert config.deepseek_base_url == "https://dotenv.example.com"


# --- AC2: neither env nor .env provides the required var -> loud, naming the first missing --------


def test_load_config_raises_naming_first_missing_var_when_nothing_provides_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_process_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    # No .env file at all in tmp_path.

    with pytest.raises(ConfigurationError) as exc_info:
        load_config()

    assert str(exc_info.value) == (
        f"missing required environment variable {REQUIRED_ENV[0]}; wombat will not start"
    )


def test_load_config_raises_naming_first_missing_var_when_only_second_is_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_process_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    _write_env_file(tmp_path, api_key=None, base_url="https://dotenv.example.com")

    with pytest.raises(ConfigurationError) as exc_info:
        load_config()

    assert str(exc_info.value) == (
        f"missing required environment variable {REQUIRED_ENV[0]}; wombat will not start"
    )


# --- AC3: explicit env var wins over a differing .env value ---------------------------------------


def test_load_config_prefers_explicit_env_var_over_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_env_file(tmp_path, api_key="sk-from-dotenv", base_url="https://dotenv.example.com")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-from-process-env")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://process-env.example.com")

    config = load_config()

    assert config.deepseek_api_key.get_secret_value() == "sk-from-process-env"
    assert config.deepseek_base_url == "https://process-env.example.com"


# --- TK-187: voice/persona config surface ----------------------------------------------------


def _set_required_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://example.com")


# --- AC1: default environment -> both providers 'local', fully offline ----------------------


def test_load_config_voice_persona_defaults_stay_fully_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)  # no .env in tmp_path -> nothing but process env applies
    _set_required_env(monkeypatch)

    config = load_config()

    assert config.wombat_stt_provider == "local"
    assert config.wombat_tts_provider == "local"
    assert config.wombat_assistant_name == "Steward"
    assert config.wombat_tts_voice_id is None
    assert config.wombat_stt_model is None
    assert config.wombat_elevenlabs_api_key is None
    assert config.wombat_deepgram_api_key is None
    assert config.wombat_fish_api_key is None


# --- AC2: provider/voice/key vars set -> populated and typed --------------------------------


def test_load_config_populates_voice_persona_fields_when_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _set_required_env(monkeypatch)
    monkeypatch.setenv("WOMBAT_STT_PROVIDER", "deepgram")
    monkeypatch.setenv("WOMBAT_TTS_PROVIDER", "fish")
    monkeypatch.setenv("WOMBAT_TTS_VOICE_ID", "voice-123")
    monkeypatch.setenv("WOMBAT_DEEPGRAM_API_KEY", "dg-secret")

    config = load_config()

    assert config.wombat_stt_provider == "deepgram"
    assert config.wombat_tts_provider == "fish"
    assert config.wombat_tts_voice_id == "voice-123"
    assert isinstance(config.wombat_deepgram_api_key, SecretStr)
    assert config.wombat_deepgram_api_key.get_secret_value() == "dg-secret"
    assert "dg-secret" not in repr(config.wombat_deepgram_api_key)
    assert repr(config.wombat_deepgram_api_key) == "SecretStr('**********')"
    # Unset key fields stay None (and, when set, every *_api_key field is a SecretStr).
    assert config.wombat_elevenlabs_api_key is None
    assert config.wombat_fish_api_key is None


# --- AC3: unknown provider value -> loud, naming the offending var --------------------------


def test_load_config_rejects_unknown_stt_provider_naming_the_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _set_required_env(monkeypatch)
    monkeypatch.setenv("WOMBAT_STT_PROVIDER", "nonsense")

    with pytest.raises(ConfigurationError) as exc_info:
        load_config()

    assert "WOMBAT_STT_PROVIDER" in str(exc_info.value)


# --- TK-241 (DEC-43): wombat_settings TABLE — app-editable, non-secret, pinned precedence ---


def _set_dsn(monkeypatch: pytest.MonkeyPatch, dsn: str | None) -> None:
    if dsn is None:
        monkeypatch.delenv("WOMBAT_PG_DSN", raising=False)
    else:
        monkeypatch.setenv("WOMBAT_PG_DSN", dsn)


# --- AC1: table < .env < explicit env var, all CWD-relative and chdir-isolated (DSN-gated) ---


@_requires_pg
def test_load_config_table_is_lowest_of_the_three_live_sources(
    fresh_settings_table: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _PG_DSN is not None
    monkeypatch.chdir(tmp_path)
    _set_required_env(monkeypatch)
    _set_dsn(monkeypatch, _PG_DSN)
    store = SettingsStore(_PG_DSN)
    try:
        store.put({"wombat_assistant_name": "Marvin"})
    finally:
        store.close()

    config = load_config()
    assert config.wombat_assistant_name == "Marvin"

    # A .env value in the same tmp cwd wins over the table.
    (tmp_path / ".env").write_text(
        f"DEEPSEEK_API_KEY=sk-test\nDEEPSEEK_BASE_URL=https://example.com\n"
        f"WOMBAT_PG_DSN={_PG_DSN}\nWOMBAT_ASSISTANT_NAME=Env\n",
        encoding="utf-8",
    )
    config = load_config()
    assert config.wombat_assistant_name == "Env"

    # An explicit process env var wins over both.
    monkeypatch.setenv("WOMBAT_ASSISTANT_NAME", "FromProcessEnv")
    config = load_config()
    assert config.wombat_assistant_name == "FromProcessEnv"


# --- AC2(a): no WOMBAT_PG_DSN anywhere -> no pg connection attempted, defaults/env only ------


def test_load_config_no_dsn_never_connects_and_uses_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)  # no .env in tmp_path -> nothing but process env applies
    _set_required_env(monkeypatch)
    _set_dsn(monkeypatch, None)
    calls: list[object] = []
    monkeypatch.setattr(
        "wombat.settings_store.psycopg.connect", lambda *a, **kw: calls.append((a, kw))
    )

    config = load_config()

    assert calls == []  # structurally: zero connection attempts
    assert config.wombat_assistant_name == "Steward"


# --- AC2(b): unreachable host -> exactly one WARNING, boots on defaults, bounded time --------


def test_load_config_unreachable_dsn_warns_once_and_boots_on_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.chdir(tmp_path)
    _set_required_env(monkeypatch)
    # Port 1 on localhost refuses immediately (no listener) — bounded without waiting out the
    # connect_timeout, and never touches any real service.
    _set_dsn(monkeypatch, "postgresql://nope:nope@127.0.0.1:1/nope")

    started = time.monotonic()
    with caplog.at_level(logging.WARNING, logger="wombat.config"):
        config = load_config()  # must not raise
    elapsed = time.monotonic() - started

    assert elapsed < 5.0  # bounded — never hangs boot
    assert config.wombat_assistant_name == "Steward"
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1


# --- AC3: a secret-tier row and an invalid admitted-value row are each dropped, one WARNING each,
# --- naming the field; the valid sibling still loads; load_config never raises (DSN-gated) ---


@_requires_pg
def test_load_config_table_drops_secret_row_and_invalid_admitted_value(
    fresh_settings_table: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A secret-field row can never land via ``SettingsStore.put`` (it refuses loudly) — this
    seeds one directly via SQL to prove the ``_SettingsTableSource`` guard is defense in depth
    against a row landing by some other path (e.g. a stray manual INSERT)."""
    assert _PG_DSN is not None
    monkeypatch.chdir(tmp_path)
    _set_required_env(monkeypatch)
    _set_dsn(monkeypatch, _PG_DSN)
    with psycopg.connect(_PG_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO wombat_settings (key, value) VALUES (%s, %s)",
            ("deepseek_api_key", '"sk-must-never-land"'),
        )
        conn.commit()
    store = SettingsStore(_PG_DSN)
    try:
        # TK-300 (DEC-67b): "playful" is now a valid humor level — use "sarcastic", still
        # outside the closed set, to exercise the out-of-vocab drop.
        store.put({"wombat_persona_humor": "sarcastic", "wombat_assistant_name": "Kip"})
    finally:
        store.close()

    with caplog.at_level(logging.WARNING, logger="wombat.config"):
        config = load_config()  # must not raise

    assert config.deepseek_api_key.get_secret_value() == "sk-test"  # untouched by the row
    assert config.wombat_persona_humor == "none"  # falls back to the field default
    assert config.wombat_assistant_name == "Kip"  # the valid sibling value still loads
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2
    assert any("deepseek_api_key" in w.message for w in warnings)
    assert any(
        "wombat_persona_humor" in w.message and "sarcastic" in w.message for w in warnings
    )


# --- AC3 (removal): no wombat.settings.json read path remains in config.py -------------------


def test_config_module_no_longer_reads_the_json_settings_file() -> None:
    source = inspect.getsource(config_module)
    assert "wombat.settings.json" not in source
    assert "JsonConfigSettingsSource" not in source
    assert "WOMBAT_SETTINGS_FILE" not in source


def test_gitignore_excludes_wombat_settings_json() -> None:
    """The legacy file is gone from config.py's read path (TK-241), but it's still the one-time
    migration source ``settings_store.import_legacy_settings_file`` reads — still gitignored."""
    gitignore = Path(__file__).resolve().parents[2] / ".gitignore"
    lines = {line.strip() for line in gitignore.read_text(encoding="utf-8").splitlines()}
    assert "wombat.settings.json" in lines


# --- AC4: bare WombatConfig() construction performs ZERO database I/O, even with a DSN set ----


def test_bare_wombat_config_construction_performs_zero_db_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WOMBAT_PG_DSN", "postgresql://nope:nope@127.0.0.1:1/nope")
    calls: list[object] = []
    monkeypatch.setattr(
        "wombat.settings_store.psycopg.connect", lambda *a, **kw: calls.append((a, kw))
    )

    config = WombatConfig(deepseek_api_key="sk-test", deepseek_base_url="https://example.com")

    assert calls == []  # structurally: zero connection attempts at bare construction
    assert config.wombat_assistant_name == "Steward"


# --- TK-208: persona matrix config surface ---------------------------------------------------


def test_load_config_persona_defaults_match_default_matrix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC1: with no persona env vars, load_config() -> matrix_from_config() == DEFAULT_MATRIX."""

    monkeypatch.chdir(tmp_path)  # no .env/settings.json in tmp_path -> nothing but process env
    _set_required_env(monkeypatch)

    config = load_config()

    assert matrix_from_config(config) == DEFAULT_MATRIX


def test_load_config_populates_persona_fields_when_set_others_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC2: setting humor + brevity leaves warmth/directness/proactivity at DEFAULT_MATRIX."""

    monkeypatch.chdir(tmp_path)
    _set_required_env(monkeypatch)
    monkeypatch.setenv("WOMBAT_PERSONA_HUMOR", "dry")
    monkeypatch.setenv("WOMBAT_PERSONA_BREVITY", "expansive")

    config = load_config()
    matrix = matrix_from_config(config)

    assert matrix.humor == Humor.DRY
    assert matrix.brevity == Brevity.EXPANSIVE
    assert matrix.warmth == DEFAULT_MATRIX.warmth
    assert matrix.directness == DEFAULT_MATRIX.directness
    assert matrix.proactivity == DEFAULT_MATRIX.proactivity


def test_load_config_accepts_the_tk300_widened_persona_levels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TK-300 (DEC-67b/c): the widened brevity/warmth/humor levels load without raising."""

    monkeypatch.chdir(tmp_path)
    _set_required_env(monkeypatch)
    monkeypatch.setenv("WOMBAT_PERSONA_BREVITY", "exhaustive")
    monkeypatch.setenv("WOMBAT_PERSONA_WARMTH", "affectionate")
    monkeypatch.setenv("WOMBAT_PERSONA_HUMOR", "comedian")

    config = load_config()
    matrix = matrix_from_config(config)

    assert matrix.brevity == Brevity.EXHAUSTIVE
    assert matrix.warmth == Warmth.AFFECTIONATE
    assert matrix.humor == Humor.COMEDIAN


def test_load_config_rejects_unknown_persona_warmth_naming_the_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC3: an out-of-vocabulary WOMBAT_PERSONA_WARMTH fails load_config loudly, naming it."""

    monkeypatch.chdir(tmp_path)
    _set_required_env(monkeypatch)
    monkeypatch.setenv("WOMBAT_PERSONA_WARMTH", "nonsense")

    with pytest.raises(ConfigurationError) as exc_info:
        load_config()

    assert "WOMBAT_PERSONA_WARMTH" in str(exc_info.value)


@_requires_pg
def test_settings_table_persona_field_is_app_editable(
    fresh_settings_table: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC4: a wombat_settings row carrying a persona field loads, riding the TK-196 app-editable
    tier (table-sourced by TK-241)."""
    assert _PG_DSN is not None
    monkeypatch.chdir(tmp_path)
    _set_required_env(monkeypatch)
    _set_dsn(monkeypatch, _PG_DSN)
    store = SettingsStore(_PG_DSN)
    try:
        store.put({"wombat_persona_humor": "dry"})
    finally:
        store.close()

    config = load_config()

    assert config.wombat_persona_humor == "dry"
    for name in (
        "wombat_persona_brevity",
        "wombat_persona_warmth",
        "wombat_persona_directness",
        "wombat_persona_humor",
        "wombat_persona_proactivity",
    ):
        assert name in APP_EDITABLE_FIELDS


# --- TK-224: wombat_voice_enabled joins the app-editable tier -------------------------------


@_requires_pg
def test_load_config_table_accepts_voice_enabled_bool(
    fresh_settings_table: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC (Q-111(b)): a bool value for the newly-admitted field loads (table-sourced)."""
    assert _PG_DSN is not None
    monkeypatch.chdir(tmp_path)
    _set_required_env(monkeypatch)
    _set_dsn(monkeypatch, _PG_DSN)
    store = SettingsStore(_PG_DSN)
    try:
        store.put({"wombat_voice_enabled": True})
    finally:
        store.close()

    config = load_config()

    assert config.wombat_voice_enabled is True
    assert "wombat_voice_enabled" in APP_EDITABLE_FIELDS


# --- TK-275 (DEC-58 c/d): wombat_ptt_binding joins the app-editable tier --------------------


def test_wombat_ptt_binding_defaults_to_unbound() -> None:
    """AC3: default "" means unbound; the field is admitted."""
    config = WombatConfig(deepseek_api_key="k", deepseek_base_url="https://example.invalid")
    assert config.wombat_ptt_binding == ""
    assert "wombat_ptt_binding" in APP_EDITABLE_FIELDS


@_requires_pg
def test_load_config_table_accepts_ptt_binding_str(
    fresh_settings_table: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A str value for the newly-admitted field loads (table-sourced)."""
    assert _PG_DSN is not None
    monkeypatch.chdir(tmp_path)
    _set_required_env(monkeypatch)
    _set_dsn(monkeypatch, _PG_DSN)
    store = SettingsStore(_PG_DSN)
    try:
        store.put({"wombat_ptt_binding": "key:KeyK"})
    finally:
        store.close()

    config = load_config()

    assert config.wombat_ptt_binding == "key:KeyK"
    assert "wombat_ptt_binding" in APP_EDITABLE_FIELDS


# --- TK-292 (DEC-65a/c): wombat_user_name joins the app-editable tier ------------------------


def test_wombat_user_name_defaults_to_empty_string() -> None:
    """AC5: absent row = "" — the CHAT mouth's user-name slot is unset by default."""
    config = WombatConfig(deepseek_api_key="k", deepseek_base_url="https://example.invalid")
    assert config.wombat_user_name == ""
    assert "wombat_user_name" in APP_EDITABLE_FIELDS


@_requires_pg
def test_load_config_table_accepts_user_name_str(
    fresh_settings_table: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC5: wombat_user_name round-trips through the settings table (app-editable tier)."""
    assert _PG_DSN is not None
    monkeypatch.chdir(tmp_path)
    _set_required_env(monkeypatch)
    _set_dsn(monkeypatch, _PG_DSN)
    store = SettingsStore(_PG_DSN)
    try:
        store.put({"wombat_user_name": "Jim"})
    finally:
        store.close()

    config = load_config()

    assert config.wombat_user_name == "Jim"
    assert "wombat_user_name" in APP_EDITABLE_FIELDS


# --- AC4: the identical out-of-vocab value via the ENV tier still fails loud, naming the var
# --- (TK-187 behavior pinned unchanged — only the app-file tier grew tolerant) --------------


# --- TK-303 (DEC-67e/f): reply window / spoken-reply cap / asr_model become config fields --------


def test_load_config_reply_window_speech_cap_asr_model_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _set_required_env(monkeypatch)

    config = load_config()

    assert config.wombat_reply_window_seconds == 120.0
    assert config.wombat_spoken_reply_max_chars == 400
    assert config.wombat_asr_model == "base"
    for name in (
        "wombat_reply_window_seconds",
        "wombat_spoken_reply_max_chars",
        "wombat_asr_model",
    ):
        assert name in APP_EDITABLE_FIELDS


def test_load_config_reads_reply_window_speech_cap_asr_model_from_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _set_required_env(monkeypatch)
    monkeypatch.setenv("WOMBAT_REPLY_WINDOW_SECONDS", "300")
    monkeypatch.setenv("WOMBAT_SPOKEN_REPLY_MAX_CHARS", "800")
    monkeypatch.setenv("WOMBAT_ASR_MODEL", "small")

    config = load_config()

    assert config.wombat_reply_window_seconds == 300.0
    assert config.wombat_spoken_reply_max_chars == 800
    assert config.wombat_asr_model == "small"


def test_load_config_rejects_out_of_bounds_reply_window_naming_the_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _set_required_env(monkeypatch)
    monkeypatch.setenv("WOMBAT_REPLY_WINDOW_SECONDS", "20")

    with pytest.raises(ConfigurationError) as exc_info:
        load_config()

    assert "WOMBAT_REPLY_WINDOW_SECONDS" in str(exc_info.value)


def test_load_config_rejects_out_of_bounds_speech_cap_naming_the_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _set_required_env(monkeypatch)
    monkeypatch.setenv("WOMBAT_SPOKEN_REPLY_MAX_CHARS", "5000")

    with pytest.raises(ConfigurationError) as exc_info:
        load_config()

    assert "WOMBAT_SPOKEN_REPLY_MAX_CHARS" in str(exc_info.value)


def test_load_config_rejects_unknown_asr_model_naming_the_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _set_required_env(monkeypatch)
    monkeypatch.setenv("WOMBAT_ASR_MODEL", "huge")

    with pytest.raises(ConfigurationError) as exc_info:
        load_config()

    assert "WOMBAT_ASR_MODEL" in str(exc_info.value)


@_requires_pg
def test_load_config_table_accepts_reply_window_speech_cap_asr_model(
    fresh_settings_table: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC4: all three round-trip through the settings table (app-editable tier)."""
    assert _PG_DSN is not None
    monkeypatch.chdir(tmp_path)
    _set_required_env(monkeypatch)
    _set_dsn(monkeypatch, _PG_DSN)
    store = SettingsStore(_PG_DSN)
    try:
        store.put(
            {
                "wombat_reply_window_seconds": 300.0,
                "wombat_spoken_reply_max_chars": 800,
                "wombat_asr_model": "small",
            }
        )
    finally:
        store.close()

    config = load_config()

    assert config.wombat_reply_window_seconds == 300.0
    assert config.wombat_spoken_reply_max_chars == 800
    assert config.wombat_asr_model == "small"


def test_load_config_rejects_unknown_persona_humor_env_var_naming_it_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # TK-300 (DEC-67b): "playful" is now a valid humor level — use "sarcastic", still outside
    # the closed set, so this still exercises the env-tier fail-loud path.
    monkeypatch.chdir(tmp_path)
    _set_required_env(monkeypatch)
    monkeypatch.setenv("WOMBAT_PERSONA_HUMOR", "sarcastic")

    with pytest.raises(ConfigurationError) as exc_info:
        load_config()

    assert "WOMBAT_PERSONA_HUMOR" in str(exc_info.value)


# --- TK-304 (DEC-67g): wombat_quiet_start/wombat_quiet_end -------------------------------------


def test_load_config_quiet_hours_defaults_to_blank(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _set_required_env(monkeypatch)

    config = load_config()

    assert config.wombat_quiet_start == ""
    assert config.wombat_quiet_end == ""
    assert "wombat_quiet_start" in APP_EDITABLE_FIELDS
    assert "wombat_quiet_end" in APP_EDITABLE_FIELDS


def test_load_config_reads_quiet_hours_from_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _set_required_env(monkeypatch)
    monkeypatch.setenv("WOMBAT_QUIET_START", "22:00")
    monkeypatch.setenv("WOMBAT_QUIET_END", "07:00")

    config = load_config()

    assert config.wombat_quiet_start == "22:00"
    assert config.wombat_quiet_end == "07:00"


def test_load_config_rejects_malformed_quiet_start_naming_the_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _set_required_env(monkeypatch)
    monkeypatch.setenv("WOMBAT_QUIET_START", "25:99")

    with pytest.raises(ConfigurationError) as exc_info:
        load_config()

    assert "WOMBAT_QUIET_START" in str(exc_info.value)


@_requires_pg
def test_load_config_table_drops_malformed_quiet_end_with_warning(
    fresh_settings_table: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC4 (table tier): a malformed table-sourced value is DROPPED with one warning, falling
    back to the field default, rather than crashing load_config() (the DEC-43 CON-3 posture —
    mirrors ``test_load_config_rejects_unknown_asr_model_naming_the_var``'s table-tier sibling
    for the closed-vocabulary fields)."""
    assert _PG_DSN is not None
    monkeypatch.chdir(tmp_path)
    _set_required_env(monkeypatch)
    _set_dsn(monkeypatch, _PG_DSN)
    store = SettingsStore(_PG_DSN)
    try:
        store.put({"wombat_quiet_end": "25:99"})
    finally:
        store.close()

    with caplog.at_level(logging.WARNING):
        config = load_config()

    assert config.wombat_quiet_end == ""  # dropped, falls back to the default
    assert "wombat_quiet_end" in caplog.text


@_requires_pg
def test_load_config_table_accepts_quiet_hours(
    fresh_settings_table: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _PG_DSN is not None
    monkeypatch.chdir(tmp_path)
    _set_required_env(monkeypatch)
    _set_dsn(monkeypatch, _PG_DSN)
    store = SettingsStore(_PG_DSN)
    try:
        store.put({"wombat_quiet_start": "22:00", "wombat_quiet_end": "07:00"})
    finally:
        store.close()

    config = load_config()

    assert config.wombat_quiet_start == "22:00"
    assert config.wombat_quiet_end == "07:00"


# --- TK-228 (DEC-40): resolve_wombat_zone — WOMBAT_TIMEZONE, tzlocal fallback, NO silent UTC -----


def _wombat_config(*, wombat_timezone: str | None = None) -> WombatConfig:
    return WombatConfig(
        deepseek_api_key="sk-test",
        deepseek_base_url="https://api.deepseek.com",
        wombat_timezone=wombat_timezone,
    )


# --- AC2(a): an explicit, valid IANA value resolves to that exact ZoneInfo -------------------


def test_resolve_wombat_zone_explicit_value_resolves_to_that_zone() -> None:
    config = _wombat_config(wombat_timezone="America/New_York")

    assert resolve_wombat_zone(config) == ZoneInfo("America/New_York")


# --- AC2(b): unset -> tzlocal.get_localzone(), a real IANA ZoneInfo, never a silent UTC ------


def test_resolve_wombat_zone_unset_resolves_via_tzlocal(monkeypatch: pytest.MonkeyPatch) -> None:
    import tzlocal

    monkeypatch.setattr(tzlocal, "get_localzone", lambda: ZoneInfo("Europe/Berlin"))

    zone = resolve_wombat_zone(_wombat_config())

    assert isinstance(zone, ZoneInfo)
    assert zone.key  # non-empty IANA key
    assert zone == ZoneInfo("Europe/Berlin")


# --- AC2(c): an unrecognized IANA key fails loud, naming WOMBAT_TIMEZONE ---------------------


def test_resolve_wombat_zone_invalid_value_raises_naming_the_var() -> None:
    config = _wombat_config(wombat_timezone="Not/AZone")

    with pytest.raises(ConfigurationError) as exc_info:
        resolve_wombat_zone(config)

    assert "WOMBAT_TIMEZONE" in str(exc_info.value)


# --- AC2(d): a tzlocal resolution failure ALSO fails loud, naming WOMBAT_TIMEZONE ------------


def test_resolve_wombat_zone_tzlocal_failure_raises_naming_the_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tzlocal

    def _raise() -> ZoneInfo:
        raise LookupError("no timezone could be determined for this host")

    monkeypatch.setattr(tzlocal, "get_localzone", _raise)

    with pytest.raises(ConfigurationError) as exc_info:
        resolve_wombat_zone(_wombat_config())

    assert "WOMBAT_TIMEZONE" in str(exc_info.value)


# --- AC2(e): wombat_timezone is NOT app-editable and NOT required -----------------------------


def test_wombat_timezone_is_not_app_editable_and_not_required() -> None:
    assert "wombat_timezone" not in APP_EDITABLE_FIELDS
    assert "WOMBAT_TIMEZONE" not in REQUIRED_ENV


# --- TK-261 (DEC-52e): wombat_singleton_port — documented default, operator .env-tier field ----


def test_wombat_singleton_port_default_and_not_app_editable_or_required() -> None:
    config = WombatConfig(
        deepseek_api_key="sk-test",
        deepseek_base_url="https://api.deepseek.com",
    )
    assert config.wombat_singleton_port == 63218
    assert "wombat_singleton_port" not in APP_EDITABLE_FIELDS
    assert "WOMBAT_SINGLETON_PORT" not in REQUIRED_ENV


def test_wombat_singleton_port_reads_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WOMBAT_SINGLETON_PORT", "54321")

    config = WombatConfig(
        deepseek_api_key="sk-test",
        deepseek_base_url="https://api.deepseek.com",
    )

    assert config.wombat_singleton_port == 54321


# --- TK-309 (DEC-68b): wombat_observe_screen/webcam/mic join the app-editable tier -----------


def test_wombat_observe_fields_default_to_false() -> None:
    """AC1: fresh install (no env, no pg) -> all three False; each is app-editable."""
    config = WombatConfig(deepseek_api_key="k", deepseek_base_url="https://example.invalid")
    assert config.wombat_observe_screen is False
    assert config.wombat_observe_webcam is False
    assert config.wombat_observe_mic is False
    for name in ("wombat_observe_screen", "wombat_observe_webcam", "wombat_observe_mic"):
        assert name in APP_EDITABLE_FIELDS


@_requires_pg
def test_load_config_table_accepts_observe_fields(
    fresh_settings_table: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC2: a pg row true + env unset -> True (table-sourced), siblings stay at their False
    default."""
    assert _PG_DSN is not None
    monkeypatch.chdir(tmp_path)
    _set_required_env(monkeypatch)
    _set_dsn(monkeypatch, _PG_DSN)
    store = SettingsStore(_PG_DSN)
    try:
        store.put({"wombat_observe_screen": True})
    finally:
        store.close()

    config = load_config()

    assert config.wombat_observe_screen is True
    assert config.wombat_observe_webcam is False
    assert config.wombat_observe_mic is False


@_requires_pg
def test_load_config_env_wins_over_true_pg_row_for_observe_screen(
    fresh_settings_table: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC2: DEC-43 precedence pinned — env 'false' wins over a true pg row."""
    assert _PG_DSN is not None
    monkeypatch.chdir(tmp_path)
    _set_required_env(monkeypatch)
    _set_dsn(monkeypatch, _PG_DSN)
    store = SettingsStore(_PG_DSN)
    try:
        store.put({"wombat_observe_screen": True})
    finally:
        store.close()
    monkeypatch.setenv("WOMBAT_OBSERVE_SCREEN", "false")

    config = load_config()

    assert config.wombat_observe_screen is False


# --- TK-319 (DEC-70(c)): wombat_observe_screenpipe joins the app-editable tier; ----------------
# --- wombat_screenpipe_url is an operator .env-tier field, not app-editable --------------------


def test_wombat_observe_screenpipe_defaults_to_false_and_url_defaults() -> None:
    """AC(a): fresh install (no env, no pg) -> observe_screenpipe False, url pinned exactly."""
    config = WombatConfig(deepseek_api_key="k", deepseek_base_url="https://example.invalid")
    assert config.wombat_observe_screenpipe is False
    assert config.wombat_screenpipe_url == "http://127.0.0.1:3030"
    assert "wombat_observe_screenpipe" in APP_EDITABLE_FIELDS
    assert "wombat_screenpipe_url" not in APP_EDITABLE_FIELDS


@_requires_pg
def test_load_config_table_accepts_observe_screenpipe(
    fresh_settings_table: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC(b): a pg row true + env unset -> True (table-sourced)."""
    assert _PG_DSN is not None
    monkeypatch.chdir(tmp_path)
    _set_required_env(monkeypatch)
    _set_dsn(monkeypatch, _PG_DSN)
    store = SettingsStore(_PG_DSN)
    try:
        store.put({"wombat_observe_screenpipe": True})
    finally:
        store.close()

    config = load_config()

    assert config.wombat_observe_screenpipe is True


@_requires_pg
def test_load_config_env_wins_over_true_pg_row_for_observe_screenpipe(
    fresh_settings_table: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC(b): DEC-43 precedence pinned — env 'false' wins over a true pg row."""
    assert _PG_DSN is not None
    monkeypatch.chdir(tmp_path)
    _set_required_env(monkeypatch)
    _set_dsn(monkeypatch, _PG_DSN)
    store = SettingsStore(_PG_DSN)
    try:
        store.put({"wombat_observe_screenpipe": True})
    finally:
        store.close()
    monkeypatch.setenv("WOMBAT_OBSERVE_SCREENPIPE", "false")

    config = load_config()

    assert config.wombat_observe_screenpipe is False


# --- TK-318 (DEC-69b): wombat_speak_full_replies joins the app-editable tier ------------------


def test_wombat_speak_full_replies_defaults_to_false() -> None:
    """AC1: fresh install (no env, no pg) -> False; app-editable."""
    config = WombatConfig(deepseek_api_key="k", deepseek_base_url="https://example.invalid")
    assert config.wombat_speak_full_replies is False
    assert "wombat_speak_full_replies" in APP_EDITABLE_FIELDS


@_requires_pg
def test_load_config_table_accepts_speak_full_replies(
    fresh_settings_table: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC2: a pg row true + env unset -> True (table-sourced)."""
    assert _PG_DSN is not None
    monkeypatch.chdir(tmp_path)
    _set_required_env(monkeypatch)
    _set_dsn(monkeypatch, _PG_DSN)
    store = SettingsStore(_PG_DSN)
    try:
        store.put({"wombat_speak_full_replies": True})
    finally:
        store.close()

    config = load_config()

    assert config.wombat_speak_full_replies is True


@_requires_pg
def test_load_config_env_wins_over_true_pg_row_for_speak_full_replies(
    fresh_settings_table: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC2: DEC-43 precedence pinned — env 'false' wins over a true pg row."""
    assert _PG_DSN is not None
    monkeypatch.chdir(tmp_path)
    _set_required_env(monkeypatch)
    _set_dsn(monkeypatch, _PG_DSN)
    store = SettingsStore(_PG_DSN)
    try:
        store.put({"wombat_speak_full_replies": True})
    finally:
        store.close()
    monkeypatch.setenv("WOMBAT_SPEAK_FULL_REPLIES", "false")

    config = load_config()

    assert config.wombat_speak_full_replies is False


# --- TK-326 (DEC-71a/DEC-72a): wombat_fish_model pins the Fish engine version, operator .env-tier
# --- (deliberately NOT app-editable — the wombat_screenpipe_url precedent) ---------------------


def test_wombat_fish_model_defaults_to_s21_pro() -> None:
    """AC1: fresh install (no env) -> 's2.1-pro'; the field is deliberately NOT app-editable."""
    config = WombatConfig(deepseek_api_key="k", deepseek_base_url="https://example.invalid")
    assert config.wombat_fish_model == "s2.1-pro"
    assert "wombat_fish_model" not in APP_EDITABLE_FIELDS


def test_load_config_rejects_unknown_fish_model_naming_the_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _set_required_env(monkeypatch)
    monkeypatch.setenv("WOMBAT_FISH_MODEL", "s3-ultra")

    with pytest.raises(ConfigurationError) as exc_info:
        load_config()

    assert "WOMBAT_FISH_MODEL" in str(exc_info.value)


def test_wombat_fish_model_docstring_names_the_free_variant() -> None:
    """Structural check the briefing calls for: the field's docstring/comment must name
    ``s2.1-pro-free`` as the zero-credit free-tier variant of the same bracket-grammar family.

    TK-328 ISS-38(m4): anchored on the ``wombat_fish_model`` field declaration itself (the
    comment block immediately precedes it) rather than the first ``"TK-326"`` occurrence in the
    whole module + a fixed 900-char window — that anchor would silently stop covering the comment
    if any earlier ``"TK-326"`` mention were ever added, or the block grew past the window."""
    source = inspect.getsource(config_module)
    field_start = source.index("wombat_fish_model:")
    comment_block = source[max(0, field_start - 900) : field_start]
    assert "s2.1-pro-free" in comment_block
    assert "zero-credit" in comment_block or "free-tier" in comment_block


# --- TK-241 AC5 (v2.64): suite hermeticity — closes the CLASS of collection-time DB hazards ---


def test_persona_live_eval_module_reimport_performs_zero_db_connections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact TK-210 line-115 defect this pins: ``tests/persona/test_output_effects_live.py``
    used to compute ``_MISSING_LIVE_REQUIREMENTS = _missing_live_requirements()`` at MODULE
    level, calling ``load_config()`` (hence, post-TK-241, a real settings-table read) at
    import/collection time — regardless of whether the live eval was armed. Reimporting that
    module here, with ``WOMBAT_PG_DSN`` pointed at a DSN and ``psycopg.connect`` replaced by a
    call-recording stub, proves import performs ZERO connection attempts. Against the OLD eager
    code this would record a call (``load_config()`` reads the table whenever a DSN resolves,
    unconditionally) and this test would fail — proving the check would have caught the defect.
    """
    import importlib

    monkeypatch.setenv("WOMBAT_PG_DSN", "postgresql://nope:nope@127.0.0.1:1/nope")
    monkeypatch.delenv("WOMBAT_TEST_PERSONA_EVAL_LIVE", raising=False)
    calls: list[object] = []
    monkeypatch.setattr(
        "wombat.settings_store.psycopg.connect", lambda *a, **kw: calls.append((a, kw))
    )

    import tests.persona.test_output_effects_live as live_mod

    importlib.reload(live_mod)

    assert calls == []
