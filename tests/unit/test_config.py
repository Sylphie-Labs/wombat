"""TK-186 — load_config: a .env-only DeepSeek setup boots (LIVE BUG fix).

The pre-pydantic ``os.environ`` REQUIRED_ENV check used to run before pydantic-settings ever
read the repo-root ``.env``, so an operator with credentials ONLY in ``.env`` (no exported env
vars) could never boot. These tests pin the fixed order: ``.env`` is a valid source, explicit
env vars still win over it, and the loud-boot message shape (first missing var, named) is
unchanged.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from pydantic import SecretStr

from wombat.config import APP_EDITABLE_FIELDS, REQUIRED_ENV, ConfigurationError, load_config
from wombat.persona.matrix import DEFAULT_MATRIX, Brevity, Humor, matrix_from_config


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


# --- TK-196: wombat.settings.json — app-editable, non-secret, pinned precedence -------------


def _write_settings_file(tmp_path: Path, data: dict[str, object]) -> None:
    (tmp_path / "wombat.settings.json").write_text(json.dumps(data), encoding="utf-8")


# --- AC1: settings.json < .env < explicit env var, all CWD-relative and chdir-isolated ------


def test_load_config_settings_json_is_lowest_of_the_three_live_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _set_required_env(monkeypatch)
    _write_settings_file(tmp_path, {"wombat_assistant_name": "Marvin"})

    config = load_config()
    assert config.wombat_assistant_name == "Marvin"

    # A .env value in the same tmp cwd wins over the settings file.
    (tmp_path / ".env").write_text(
        "DEEPSEEK_API_KEY=sk-test\nDEEPSEEK_BASE_URL=https://example.com\n"
        "WOMBAT_ASSISTANT_NAME=Env\n",
        encoding="utf-8",
    )
    config = load_config()
    assert config.wombat_assistant_name == "Env"

    # An explicit process env var wins over both.
    monkeypatch.setenv("WOMBAT_ASSISTANT_NAME", "FromProcessEnv")
    config = load_config()
    assert config.wombat_assistant_name == "FromProcessEnv"


# --- AC2: a secret field named in the file is dropped, loudly, and never loads --------------


def test_load_config_settings_json_drops_secret_field_with_one_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.chdir(tmp_path)
    _set_required_env(monkeypatch)
    _write_settings_file(tmp_path, {"wombat_fish_api_key": "sekrit"})

    with caplog.at_level(logging.WARNING, logger="wombat.config"):
        config = load_config()

    assert config.wombat_fish_api_key is None
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "wombat_fish_api_key" in warnings[0].message


# --- AC3: absent file is byte-unchanged behavior; malformed file is one loud warning + absent


def test_load_config_no_settings_file_is_a_silent_no_op(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _set_required_env(monkeypatch)
    # No wombat.settings.json in tmp_path at all.

    config = load_config()

    assert config.wombat_assistant_name == "Steward"


def test_load_config_malformed_settings_file_warns_once_and_is_treated_as_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.chdir(tmp_path)
    _set_required_env(monkeypatch)
    (tmp_path / "wombat.settings.json").write_text("{not valid json", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="wombat.config"):
        config = load_config()

    assert config.wombat_assistant_name == "Steward"
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "wombat.settings.json" in warnings[0].message


@pytest.mark.parametrize("raw", ["[1, 2, 3]", "null"])
def test_load_config_non_object_settings_file_warns_once_and_is_treated_as_absent(
    raw: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.chdir(tmp_path)
    _set_required_env(monkeypatch)
    (tmp_path / "wombat.settings.json").write_text(raw, encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="wombat.config"):
        config = load_config()

    assert config.wombat_assistant_name == "Steward"
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "wombat.settings.json" in warnings[0].message


def test_gitignore_excludes_wombat_settings_json() -> None:
    gitignore = Path(__file__).resolve().parents[2] / ".gitignore"
    lines = {line.strip() for line in gitignore.read_text(encoding="utf-8").splitlines()}
    assert "wombat.settings.json" in lines


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


def test_settings_json_persona_field_is_app_editable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC4: a settings.json carrying a persona field loads, riding the TK-196 app-editable tier."""

    monkeypatch.chdir(tmp_path)
    _set_required_env(monkeypatch)
    _write_settings_file(tmp_path, {"wombat_persona_humor": "dry"})

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


# --- TK-226 (CR5-1/CR5-2): UTF-8 pin, decode-guard widening, per-value validate-or-drop ------


# --- AC1: a non-ASCII value undefined in cp1252 round-trips exactly, no mojibake, no crash --


def test_load_config_settings_json_round_trips_utf8_value_undefined_in_cp1252(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _set_required_env(monkeypatch)
    # 'Ё' encodes to UTF-8 byte 0x81, which is UNDEFINED in cp1252 — reading this file with the
    # locale default (the CR5-1 bug) raises UnicodeDecodeError; reading it as UTF-8 must not.
    (tmp_path / "wombat.settings.json").write_text(
        json.dumps({"wombat_assistant_name": "Ёncins"}, ensure_ascii=False), encoding="utf-8"
    )

    config = load_config()

    assert config.wombat_assistant_name == "Ёncins"


# --- AC2: undecodable bytes, and a raw OSError on read, each warn once and fall back to defaults


def test_load_config_settings_json_undecodable_bytes_warns_once_and_is_treated_as_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.chdir(tmp_path)
    _set_required_env(monkeypatch)
    (tmp_path / "wombat.settings.json").write_bytes(b"\xff\xfe garbage")

    with caplog.at_level(logging.WARNING, logger="wombat.config"):
        config = load_config()

    assert config.wombat_assistant_name == "Steward"
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "wombat.settings.json" in warnings[0].message


def test_load_config_settings_json_os_error_warns_once_and_is_treated_as_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.chdir(tmp_path)
    _set_required_env(monkeypatch)
    _write_settings_file(tmp_path, {"wombat_assistant_name": "Marvin"})

    def _raise_os_error(self: object, file_path: Path) -> dict[str, object]:
        raise OSError("simulated unreadable file")

    monkeypatch.setattr(
        "wombat.config.JsonConfigSettingsSource._read_file", _raise_os_error, raising=True
    )

    with caplog.at_level(logging.WARNING, logger="wombat.config"):
        config = load_config()

    assert config.wombat_assistant_name == "Steward"
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "wombat.settings.json" in warnings[0].message


# --- AC3: an out-of-vocab admitted value is dropped (one warning naming field + value); the
# --- rest of the file still loads, and load_config does not raise -------------------------


def test_load_config_settings_json_drops_invalid_admitted_value_with_one_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.chdir(tmp_path)
    _set_required_env(monkeypatch)
    _write_settings_file(
        tmp_path, {"wombat_persona_humor": "playful", "wombat_assistant_name": "Kip"}
    )

    with caplog.at_level(logging.WARNING, logger="wombat.config"):
        config = load_config()  # must not raise

    assert config.wombat_persona_humor == "none"  # falls back to the field default
    assert config.wombat_assistant_name == "Kip"  # the valid sibling value still loads
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "wombat_persona_humor" in warnings[0].message
    assert "playful" in warnings[0].message


# --- AC4: the identical out-of-vocab value via the ENV tier still fails loud, naming the var
# --- (TK-187 behavior pinned unchanged — only the app-file tier grew tolerant) --------------


def test_load_config_rejects_unknown_persona_humor_env_var_naming_it_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _set_required_env(monkeypatch)
    monkeypatch.setenv("WOMBAT_PERSONA_HUMOR", "playful")

    with pytest.raises(ConfigurationError) as exc_info:
        load_config()

    assert "WOMBAT_PERSONA_HUMOR" in str(exc_info.value)
