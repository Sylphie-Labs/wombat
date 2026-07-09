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
