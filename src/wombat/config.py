"""wombat configuration — the env-sourced settings the composition root needs (TK-1).

Fails LOUD (``ConfigurationError`` naming the first missing variable) rather than starting
silently broken. Reads the model egress credentials only; everything deterministic is config-free.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Literal, get_args

from pydantic import SecretStr, TypeAdapter, ValidationError
from pydantic_settings import (
    BaseSettings,
    JsonConfigSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

logger = logging.getLogger(__name__)

# Per-field pydantic TypeAdapters used to validate wombat.settings.json values before they
# reach WombatConfig (TK-226/CR5-2) — cached so repeated __call__ invocations (e.g. multiple
# load_config() calls in a process) don't rebuild one per field per call.
_FIELD_TYPE_ADAPTERS: dict[str, TypeAdapter[Any]] = {}


def _type_adapter_for(field_name: str, annotation: Any) -> TypeAdapter[Any]:
    adapter = _FIELD_TYPE_ADAPTERS.get(field_name)
    if adapter is None:
        adapter = TypeAdapter(annotation)
        _FIELD_TYPE_ADAPTERS[field_name] = adapter
    return adapter


class ConfigurationError(RuntimeError):
    """Raised when wombat is launched without a required configuration value."""


# Declared in the order they are reported as missing (AC2 names the FIRST missing one).
REQUIRED_ENV: tuple[str, ...] = ("DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL")

# The gitignored, app-editable, CWD-relative settings file (TK-196, EP-32, DEC-32). NOTHING
# writes this file yet — TK-197 owns the write path; this ticket only wires it in as a read
# source under the pinned precedence: env > .env > wombat.settings.json > defaults.
WOMBAT_SETTINGS_FILE = "wombat.settings.json"

# The documented admitted-field schema for wombat.settings.json (TK-196, Q-106(b)): keys named
# here are the ONLY ones the app-editable file may populate. TK-197 validates PUTs against it.
APP_EDITABLE_FIELDS: tuple[str, ...] = (
    "wombat_stt_provider",
    "wombat_tts_provider",
    "wombat_tts_voice_id",
    "wombat_stt_model",
    "wombat_assistant_name",
    # TK-208 (EP-33, DEC-37(g)): the persona matrix tier — app-editable so a settings UI can
    # hot-apply persona changes without an env var/restart.
    "wombat_persona_brevity",
    "wombat_persona_warmth",
    "wombat_persona_directness",
    "wombat_persona_humor",
    "wombat_persona_proactivity",
)


class _AppEditableJsonSettingsSource(JsonConfigSettingsSource):
    """Reads ``wombat.settings.json`` (TK-196) for ``WombatConfig``.

    Structural guards, all loud-not-silent:
      * no secrets load from this file — a loaded key naming a ``SecretStr``-typed
        ``WombatConfig`` field (the ``*_api_key`` fields, ``deepseek_api_key``,
        ``google_oauth_client_secret``) is dropped with exactly one ``logger.warning`` naming
        the field; the value never reaches the model.
      * a malformed or unreadable file can never fail boot (CON-3) — a ``json.JSONDecodeError``,
        ``UnicodeDecodeError`` (e.g. undecodable bytes, or bytes invalid for the pinned UTF-8
        encoding), or ``OSError`` while reading is caught, one ``logger.warning`` is logged, and
        the file is treated as absent. A genuinely-missing file is already a silent no-op
        (inherited from the base class).
      * a syntactically-valid file whose top level isn't a JSON object (an array, string,
        number, bool, or ``null``) is the same treated-as-absent posture: one ``logger.warning``
        is logged and the file is treated as empty, rather than letting the base class's
        ``dict``-only merge raise ``TypeError``/``ValueError`` and brick boot.
      * an admitted field's value that fails validation against its ``WombatConfig`` annotation
        (e.g. an out-of-vocab ``Literal`` like ``wombat_persona_humor``) is DROPPED with one
        ``logger.warning`` naming the field and the offending value, falling back to that
        field's default instead of bricking the entire process at ``load_config()`` (CR5-2).

    The file is always read as UTF-8 (``json_file_encoding="utf-8"`` at the construction site
    in ``settings_customise_sources``), matching every writer of this file (``persona/live.py``
    ``_persist``, TK-197's PUT path) — CR5-1.

    Any other loaded key that isn't in ``APP_EDITABLE_FIELDS`` (and isn't a secret field) is
    dropped silently — only the admitted-field schema may come from this file.
    """

    def _read_file(self, file_path: Path) -> dict[str, Any]:
        try:
            loaded = super()._read_file(file_path)
        except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
            logger.warning("%s is unreadable (%s); ignoring it", WOMBAT_SETTINGS_FILE, exc)
            return {}
        if not isinstance(loaded, dict):
            logger.warning(
                "%s does not contain a JSON object at the top level; ignoring it",
                WOMBAT_SETTINGS_FILE,
            )
            return {}
        return loaded

    def __call__(self) -> dict[str, Any]:
        loaded = super().__call__()
        secret_fields = {
            name
            for name, field in self.settings_cls.model_fields.items()
            if field.annotation is SecretStr or SecretStr in get_args(field.annotation)
        }
        filtered: dict[str, Any] = {}
        for key, value in loaded.items():
            if key in APP_EDITABLE_FIELDS:
                annotation = self.settings_cls.model_fields[key].annotation
                adapter = _type_adapter_for(key, annotation)
                try:
                    adapter.validate_python(value)
                except ValidationError:
                    logger.warning(
                        "%s contains an invalid value for %s: %r; ignoring it "
                        "(falling back to the field default)",
                        WOMBAT_SETTINGS_FILE,
                        key,
                        value,
                    )
                    continue
                filtered[key] = value
            elif key in secret_fields:
                logger.warning(
                    "%s contains %r, a secret field; ignoring it "
                    "(secrets never load from the app-editable settings file)",
                    WOMBAT_SETTINGS_FILE,
                    key,
                )
            # else: not an admitted field — dropped silently.
        return filtered


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

    # OPTIONAL (TK-187, DEC-28): voice/persona config surface — provider selection, key
    # overrides, voice id, and assistant name. Selecting a provider here is a structural
    # opt-in only (DEC-28: selection is necessary but not sufficient); TK-193 owns
    # constructing the actual STT/TTS clients from these fields, TK-188 owns resolving keys
    # from the keyring. Deliberately NOT in REQUIRED_ENV — the drain spine/demo/tests must
    # keep booting fully offline with every field at its default. The provider vocabulary is
    # closed (a ``Literal``) and enforced at boot: an unrecognized value fails ``load_config``
    # loudly, naming the offending variable (e.g. ``WOMBAT_STT_PROVIDER``).
    wombat_stt_provider: Literal["local", "deepgram", "elevenlabs", "fish"] = "local"
    wombat_tts_provider: Literal["local", "deepgram", "elevenlabs", "fish"] = "local"
    wombat_tts_voice_id: str | None = None
    # Cloud-STT model override. Distinct from ``wombat_asr_model`` above (TK-162's local
    # faster-whisper model name) — this one only applies when ``wombat_stt_provider`` names a
    # cloud provider.
    wombat_stt_model: str | None = None
    wombat_elevenlabs_api_key: SecretStr | None = None
    wombat_deepgram_api_key: SecretStr | None = None
    wombat_fish_api_key: SecretStr | None = None
    wombat_assistant_name: str = "Steward"

    # OPTIONAL (TK-208, EP-33, DEC-33/DEC-37): the five-axis persona matrix config surface
    # (``wombat.persona.matrix.PersonaMatrix``). Deliberately NOT in REQUIRED_ENV — the drain
    # spine/demo/tests must keep booting fully offline with every field at its default, which is
    # ``DEFAULT_MATRIX`` exactly (proactivity's default is BALANCED per DEC-37(a), superseding
    # DEC-33's original "minimal" default text). Nothing reads these fields yet — TK-209 owns
    # hot-applying them into a live persona, TK-215 owns proactivity's gate-side actuation. Each
    # axis's vocabulary is closed (a ``Literal``) and enforced at boot: an unrecognized value
    # fails ``load_config`` loudly, naming the offending variable (e.g. ``WOMBAT_PERSONA_HUMOR``).
    wombat_persona_brevity: Literal["terse", "balanced", "expansive"] = "terse"
    wombat_persona_warmth: Literal["reserved", "neutral", "warm"] = "reserved"
    wombat_persona_directness: Literal["gentle", "plain", "blunt"] = "plain"
    wombat_persona_humor: Literal["none", "dry"] = "none"
    wombat_persona_proactivity: Literal["minimal", "balanced", "forward"] = "balanced"

    # OPTIONAL (TK-222, EP-32, Q-110(d)): the runtime chat surface's handshake-file path — an
    # operator .env-tier setting, deliberately NOT in APP_EDITABLE_FIELDS (this is a launch-time
    # process wiring concern, not a persona/voice preference a settings UI edits). Chat is
    # enabled IFF this is non-blank (loud-skip parity with sources.bootstrap's
    # ``_maybe_register_*`` pattern); ``wombat.runtime.serve()`` writes exactly one
    # ``{"port": ..., "token": ...}`` JSON line here per launch, once the surface has bound its
    # ephemeral port. Deliberately NOT in REQUIRED_ENV — the drain spine/demo/tests must keep
    # booting fully offline with chat disabled.
    wombat_chat_handshake_file: str | None = None

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Pin precedence (TK-196): init kwargs > env > .env > wombat.settings.json > defaults.

        Sources are consulted in the returned order — earlier wins. Direct-construction kwargs
        (``init_settings``, used by many existing tests calling ``WombatConfig(...)`` directly)
        stay highest, so this addition is behavior-preserving for every caller that doesn't touch
        ``wombat.settings.json``.
        """
        json_settings = _AppEditableJsonSettingsSource(
            settings_cls, json_file=WOMBAT_SETTINGS_FILE, json_file_encoding="utf-8"
        )
        return init_settings, env_settings, dotenv_settings, json_settings, file_secret_settings


def load_config() -> WombatConfig:
    """Load + validate config from the environment, or raise ConfigurationError loudly.

    Env vars and the repo-root ``.env`` (if present) are both read by pydantic-settings,
    with explicit env vars taking precedence over ``.env`` values (TK-186: the pre-pydantic
    ``os.environ`` check used to short-circuit before ``.env`` was ever consulted).

    A non-missing validation failure (e.g. a ``WOMBAT_STT_PROVIDER`` value outside its closed
    vocabulary) also fails loud, naming the offending variable (TK-187).
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
        for error in exc.errors():
            if error["loc"]:
                field = str(error["loc"][0]).upper()
                raise ConfigurationError(
                    f"invalid environment variable {field}; wombat will not start"
                ) from exc
        raise
