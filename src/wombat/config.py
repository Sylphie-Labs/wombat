"""wombat configuration — the env-sourced settings the composition root needs (TK-1).

Fails LOUD (``ConfigurationError`` naming the first missing variable) rather than starting
silently broken. Reads the model egress credentials only; everything deterministic is config-free.
"""

from __future__ import annotations

from pydantic import SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict


class ConfigurationError(RuntimeError):
    """Raised when wombat is launched without a required configuration value."""


# Declared in the order they are reported as missing (AC2 names the FIRST missing one).
REQUIRED_ENV: tuple[str, ...] = ("DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL")


class WombatConfig(BaseSettings):
    """Typed view of wombat's required environment (the DeepSeek egress, ASMP-1)."""

    # Keys come from the environment; a repo-root .env is loaded if present (real values live
    # there, never in source — see .env.example). Explicit env vars still take precedence.
    model_config = SettingsConfigDict(
        populate_by_name=True, extra="ignore", env_file=".env", env_file_encoding="utf-8"
    )

    deepseek_api_key: SecretStr
    deepseek_base_url: str

    # OPTIONAL (TK-71, Q-57(b)): the Google OAuth client credentials for the gcal integration.
    # Deliberately NOT in REQUIRED_ENV — the drain spine/demo must keep booting Google-less.
    # Validation is deferred to construction of the gcal auth component (CalendarAuth), which
    # follows the TK-8 AC3-at-construction precedent and raises ConfigurationError naming the
    # first missing/blank var.
    google_oauth_client_id: str | None = None
    google_oauth_client_secret: SecretStr | None = None

    # OPTIONAL (TK-53, Q-36/Q-71): the Postgres DSN backing the standing runtime's queue/daily-
    # ledger/pending-journal (the queue is pg-only). Deliberately NOT in REQUIRED_ENV — the
    # drain spine/demo/tests must keep booting without it; ``wombat.runtime.serve()`` requires
    # it and fails loud (``ConfigurationError`` naming it) before starting.
    wombat_pg_dsn: str | None = None

    # OPTIONAL (TK-101, Q-78): the morning brief's text-sink append-only file path and whether
    # voice delivery is enabled. Deliberately NOT in REQUIRED_ENV — the drain spine/demo/tests
    # must keep booting without them; ``bootstrap.build_brief_deliver_stage`` requires a
    # non-blank ``wombat_brief_path`` and fails loud (``ConfigurationError`` naming it) at
    # construction.
    wombat_brief_path: str | None = None
    wombat_voice_enabled: bool = False

    # OPTIONAL (TK-176): the explicit-feedback file channel's path (TK-51's ``FeedbackInputSource``
    # v1 file channel). Deliberately NOT in REQUIRED_ENV — the drain spine/demo/tests must keep
    # booting without it; ``sources.bootstrap._maybe_register_feedback`` skips the file channel
    # with a loud log naming this var when it is missing/blank (the push channel is unaffected).
    wombat_feedback_file: str | None = None

    # OPTIONAL (TK-162, Q-97): the local ASR drop-directory channel — ``wombat_asr_drop_dir`` is
    # the watched directory an operator drops audio recordings into; ``wombat_asr_model`` names
    # the faster-whisper model. Deliberately NOT in REQUIRED_ENV — the drain spine/demo/tests
    # must keep booting without them; ``sources.bootstrap._maybe_register_asr`` skips the ASR
    # source with a loud log naming ``WOMBAT_ASR_DROP_DIR`` when the directory is missing/blank,
    # and separately when faster-whisper (the ``[voice]`` extra) is not installed.
    wombat_asr_drop_dir: str | None = None
    wombat_asr_model: str = "base"


def load_config() -> WombatConfig:
    """Load + validate config from the environment, or raise ConfigurationError loudly.

    Env vars and the repo-root ``.env`` (if present) are both read by pydantic-settings,
    with explicit env vars taking precedence over ``.env`` values (TK-186: the pre-pydantic
    ``os.environ`` check used to short-circuit before ``.env`` was ever consulted).
    """
    try:
        return WombatConfig()  # populated from the environment (and/or .env) by pydantic-settings
    except ValidationError as exc:
        missing = {str(error["loc"][0]) for error in exc.errors() if error["loc"]}
        for var in REQUIRED_ENV:
            if var.lower() in missing:
                raise ConfigurationError(
                    f"missing required environment variable {var}; wombat will not start"
                ) from exc
        raise
