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


def load_config() -> WombatConfig:
    """Load + validate config from the environment, or raise ConfigurationError loudly."""
    for var in REQUIRED_ENV:
        if not os.environ.get(var):
            raise ConfigurationError(
                f"missing required environment variable {var}; wombat will not start"
            )
    return WombatConfig()  # populated from the environment by pydantic-settings
