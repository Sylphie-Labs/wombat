"""TK-186 — load_config: a .env-only DeepSeek setup boots (LIVE BUG fix).

The pre-pydantic ``os.environ`` REQUIRED_ENV check used to run before pydantic-settings ever
read the repo-root ``.env``, so an operator with credentials ONLY in ``.env`` (no exported env
vars) could never boot. These tests pin the fixed order: ``.env`` is a valid source, explicit
env vars still win over it, and the loud-boot message shape (first missing var, named) is
unchanged.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr

from wombat.config import REQUIRED_ENV, ConfigurationError, load_config


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
