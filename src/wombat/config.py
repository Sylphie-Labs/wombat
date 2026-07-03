"""wombat configuration — the env-sourced settings the composition root needs (TK-1).

Fails LOUD (``ConfigurationError`` naming the first missing variable) rather than starting
silently broken. Reads the model egress credentials only; everything deterministic is config-free.
"""

from __future__ import annotations

import os

from pydantic import SecretStr
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


def load_config() -> WombatConfig:
    """Load + validate config from the environment, or raise ConfigurationError loudly."""
    for var in REQUIRED_ENV:
        if not os.environ.get(var):
            raise ConfigurationError(
                f"missing required environment variable {var}; wombat will not start"
            )
    return WombatConfig()  # populated from the environment by pydantic-settings
