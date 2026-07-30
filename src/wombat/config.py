"""wombat configuration — the env-sourced settings the composition root needs (TK-1).

Fails LOUD (``ConfigurationError`` naming the first missing variable) rather than starting
silently broken. Reads the model egress credentials only; everything deterministic is config-free.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Literal, get_args
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import tzlocal
from dotenv import dotenv_values
from pydantic import Field, SecretStr, TypeAdapter, ValidationError
from pydantic.fields import FieldInfo
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

logger = logging.getLogger(__name__)

# Per-field pydantic TypeAdapters used to validate wombat_settings table values before they
# reach WombatConfig (TK-226/CR5-2, ported to the table by TK-241) — cached so repeated __call__
# invocations (e.g. multiple load_config() calls in a process) don't rebuild one per field per
# call.
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

# The documented admitted-field schema for the app-editable settings tier (TK-196, Q-106(b),
# ported to the DEC-43 wombat_settings table by TK-241): keys named here are the ONLY ones the
# app-editable tier may populate. TK-197/settings_app validates PUTs against it.
APP_EDITABLE_FIELDS: tuple[str, ...] = (
    "wombat_stt_provider",
    "wombat_tts_provider",
    "wombat_tts_voice_id",
    "wombat_stt_model",
    "wombat_assistant_name",
    # TK-292 (DEC-65a/c): the CHAT mouth's second name slot (the user's own name/what to call
    # them) — settings-table tier, same as wombat_assistant_name; "" means unset, rendering
    # falls back to "the user".
    "wombat_user_name",
    # TK-208 (EP-33, DEC-37(g)): the persona matrix tier — app-editable so a settings UI can
    # hot-apply persona changes without an env var/restart.
    "wombat_persona_brevity",
    "wombat_persona_warmth",
    "wombat_persona_directness",
    "wombat_persona_humor",
    "wombat_persona_proactivity",
    # TK-224 (EP-32, Q-111(b)): app-editable so the Electron settings UI can flip voice
    # delivery without an env var. Deliberately NOT paired with wombat_asr_drop_dir, which
    # stays operator .env-tier (the wombat_chat_handshake_file precedent) — a settings UI
    # toggle has no business relocating where the drop-dir watcher points.
    "wombat_voice_enabled",
    # TK-275 (DEC-58 c/d): the one-shot-captured push-to-talk binding
    # ("key:<code>"/"mouse:<button>", "" = unbound) - app-editable so the Electron settings UI
    # can persist it; the Python runtime never reads this field (the renderer, TK-276, is the
    # sole consumer), so no restart notice is warranted for it.
    "wombat_ptt_binding",
    # TK-303 (DEC-67e/f): the DEC-64 walkie-talkie reply window (LastSpokenRegister's TTL),
    # the spoken-reply length cap (SpeechShapeStage's max_chars), and the local ASR model name
    # — all three restart-tier (no hot-apply; assemble_runtime reads them once at boot).
    "wombat_reply_window_seconds",
    "wombat_spoken_reply_max_chars",
    "wombat_asr_model",
)


# The boot-read half of DEC-43 (TK-241): load_config() populates this holder from the DEC-43
# wombat_settings table (TK-240's SettingsStore) BEFORE constructing WombatConfig(), and clears
# it in a finally, ALWAYS — so a bare WombatConfig() construction (any call site that isn't
# load_config()) sees an empty holder, hence _SettingsTableSource below never performs any I/O
# of its own: zero DB I/O at construction, by construction (AC4).
_TABLE_SETTINGS_HOLDER: dict[str, Any] = {}


class _SettingsTableSource(PydanticBaseSettingsSource):
    """Reads the DEC-43 ``wombat_settings`` table (TK-241) for ``WombatConfig`` — occupies the
    EXACT precedence slot the removed legacy JSON-file settings source (TK-196/TK-226) used
    to occupy: env > .env > table > defaults.

    Never touches the database itself — ``__call__`` only reads ``_TABLE_SETTINGS_HOLDER``, which
    ``load_config()`` populates (one ``SettingsStore.get_all()``) before constructing
    ``WombatConfig()`` and clears afterwards regardless of outcome. A bare ``WombatConfig(...)``
    call site never populates the holder, so this source always sees it empty (AC4).

    Structural guards, ported verbatim from the removed JSON file source (CR5-2), all
    loud-not-silent:
      * no secrets load from the table — a row naming a ``SecretStr``-typed ``WombatConfig``
        field (the ``*_api_key`` fields, ``deepseek_api_key``, ``google_oauth_client_secret``) is
        dropped with exactly one ``logger.warning`` naming the field; the value never reaches the
        model. (``SettingsStore.put`` already refuses to write these — this is defense in depth
        against a row landing by some other path, e.g. a manual INSERT.)
      * an admitted field's value that fails validation against its ``WombatConfig`` annotation
        (e.g. an out-of-vocab ``Literal`` like ``wombat_persona_humor``) is DROPPED with one
        ``logger.warning`` naming the field and the offending value, falling back to that
        field's default instead of bricking the entire process at ``load_config()``.

    Any other row key that isn't in ``APP_EDITABLE_FIELDS`` (and isn't a secret field, e.g. the
    ``wombat_persona_pins`` bookkeeping row) is dropped silently — only the admitted-field schema
    may reach ``WombatConfig``.
    """

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        # Nothing to do here — __call__ is fully overridden below (the pydantic-settings
        # InitSettingsSource precedent for a source that doesn't do per-field lookups).
        return None, "", False

    def __call__(self) -> dict[str, Any]:
        secret_fields = {
            name
            for name, field in self.settings_cls.model_fields.items()
            if field.annotation is SecretStr or SecretStr in get_args(field.annotation)
        }
        filtered: dict[str, Any] = {}
        for key, value in _TABLE_SETTINGS_HOLDER.items():
            if key in APP_EDITABLE_FIELDS:
                annotation = self.settings_cls.model_fields[key].annotation
                adapter = _type_adapter_for(key, annotation)
                try:
                    adapter.validate_python(value)
                except ValidationError:
                    logger.warning(
                        "wombat_settings contains an invalid value for %s: %r; ignoring it "
                        "(falling back to the field default)",
                        key,
                        value,
                    )
                    continue
                filtered[key] = value
            elif key in secret_fields:
                logger.warning(
                    "wombat_settings contains %r, a secret field; ignoring it "
                    "(secrets never load from the app-editable settings table)",
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

    # OPTIONAL (TK-275, DEC-58 c/d): the one-shot-captured push-to-talk binding, persisted
    # through the app-editable settings tier. "" (default) means unbound. Deliberately a plain
    # str, not a Literal - the Python runtime never reads it (the renderer is the sole consumer).
    wombat_ptt_binding: str = ""

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
    # TK-303 (DEC-67e/f): ``wombat_asr_model`` narrows from a free ``str`` to a closed
    # ``Literal`` and joins ``APP_EDITABLE_FIELDS`` — an unrecognized value fails ``load_config``
    # loudly (env tier) or is dropped with a warning (table tier), same as every other closed-
    # vocabulary field above.
    wombat_asr_drop_dir: str | None = None
    wombat_asr_model: Literal["tiny", "base", "small", "medium"] = "base"

    # OPTIONAL (TK-303, DEC-67e/f): the DEC-64 walkie-talkie reply window — how long
    # ``voice.reply_context.LastSpokenRegister`` treats its last-spoken slot as fresh, and the
    # hard brevity bound ``stages.speech_shape.SpeechShapeStage`` shapes a spoken reply to.
    # Restart-tier (assemble_runtime reads them once at boot; no hot-apply). Bounds mirror the
    # SettingsUpdate PUT validation (wombat.settings_app.api) exactly.
    wombat_reply_window_seconds: float = Field(default=120.0, ge=30, le=600)
    wombat_spoken_reply_max_chars: int = Field(default=400, ge=200, le=1200)

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
    # TK-292 (DEC-65a/c): the CHAT mouth's second name slot — "" (the default) means unset;
    # ClauseAlgebraStrategy/LivePersona fall back to "the user" when rendering CHAT.
    wombat_user_name: str = ""

    # OPTIONAL (TK-208, EP-33, DEC-33/DEC-37): the five-axis persona matrix config surface
    # (``wombat.persona.matrix.PersonaMatrix``). Deliberately NOT in REQUIRED_ENV — the drain
    # spine/demo/tests must keep booting fully offline with every field at its default, which is
    # ``DEFAULT_MATRIX`` exactly (proactivity's default is BALANCED per DEC-37(a), superseding
    # DEC-33's original "minimal" default text). Nothing reads these fields yet — TK-209 owns
    # hot-applying them into a live persona, TK-215 owns proactivity's gate-side actuation. Each
    # axis's vocabulary is closed (a ``Literal``) and enforced at boot: an unrecognized value
    # fails ``load_config`` loudly, naming the offending variable (e.g. ``WOMBAT_PERSONA_HUMOR``).
    # TK-300 (DEC-67b/c): brevity/warmth/humor widened — see wombat.persona.matrix for the
    # closed-set rationale.
    # TK-301 (DEC-67c): proactivity widened with a fourth "eager" level.
    wombat_persona_brevity: Literal["terse", "balanced", "expansive", "exhaustive"] = "terse"
    wombat_persona_warmth: Literal["reserved", "neutral", "warm", "affectionate"] = "reserved"
    wombat_persona_directness: Literal["gentle", "plain", "blunt"] = "plain"
    wombat_persona_humor: Literal["none", "dry", "playful", "comedian"] = "none"
    wombat_persona_proactivity: Literal["minimal", "balanced", "forward", "eager"] = "balanced"

    # OPTIONAL (TK-222, EP-32, Q-110(d)): the runtime chat surface's handshake-file path — an
    # operator .env-tier setting, deliberately NOT in APP_EDITABLE_FIELDS (this is a launch-time
    # process wiring concern, not a persona/voice preference a settings UI edits). Chat is
    # enabled IFF this is non-blank (loud-skip parity with sources.bootstrap's
    # ``_maybe_register_*`` pattern); ``wombat.runtime.serve()`` writes exactly one
    # ``{"port": ..., "token": ...}`` JSON line here per launch, once the surface has bound its
    # ephemeral port. Deliberately NOT in REQUIRED_ENV — the drain spine/demo/tests must keep
    # booting fully offline with chat disabled.
    wombat_chat_handshake_file: str | None = None

    # OPTIONAL (TK-228, DEC-40, realizing DEC-21/Q-15): the canonical IANA timezone every wombat
    # tz consumer (BriefTimerStage, DailyLedger.wombat_today, the nightly dream-schedule boundary,
    # etc.) resolves against — an operator .env-tier field (deliberately NOT in
    # APP_EDITABLE_FIELDS, the ``wombat_asr_drop_dir`` precedent) and deliberately NOT in
    # REQUIRED_ENV: an unset value is a legitimate default (see ``resolve_wombat_zone`` below),
    # never a missing-config error. Nothing reads this field directly — every caller goes through
    # ``resolve_wombat_zone(config)``.
    wombat_timezone: str | None = None

    # OPTIONAL (TK-261, DEC-52e): the fixed loopback port ``wombat.__main__.main()`` binds
    # exclusively, for process lifetime, as the single-instance guard (ISS-14: three runtimes ran
    # live concurrently). An operator .env-tier field, deliberately NOT in APP_EDITABLE_FIELDS
    # (the ``wombat_chat_handshake_file``/``wombat_timezone`` precedent — this is launch-time
    # process wiring, not a settings-UI concern) and deliberately NOT in REQUIRED_ENV (it has a
    # documented default and the drain spine/library use/test suite never bind it — only
    # ``main()`` does). Nothing reads this field except ``main()``.
    wombat_singleton_port: int = 63218

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Pin precedence (TK-196, table-sourced by TK-241/DEC-43): init kwargs > env > .env >
        wombat_settings table > defaults.

        Sources are consulted in the returned order — earlier wins. Direct-construction kwargs
        (``init_settings``, used by many existing tests calling ``WombatConfig(...)`` directly)
        stay highest, so this addition is behavior-preserving for every caller that doesn't touch
        ``wombat_settings``.
        """
        table_settings = _SettingsTableSource(settings_cls)
        return init_settings, env_settings, dotenv_settings, table_settings, file_secret_settings


_PG_DSN_ENV_VAR = "WOMBAT_PG_DSN"

# A short, fail-fast connect timeout (seconds) for the ONE settings-table read load_config() ever
# attempts — an unreachable host must degrade in bounded time, never hang boot (AC2).
_TABLE_SOURCE_CONNECT_TIMEOUT_SECONDS = 3


def _resolve_pg_dsn() -> str | None:
    """``WOMBAT_PG_DSN`` from the process environment, else the same var in a cwd-relative
    ``.env`` — mirrors ``settings_app.__main__._resolve_pg_dsn`` (TK-242) exactly. Deliberately
    NEVER resolves the DSN from ``wombat_settings`` itself — the DSN is operator-tier (TK-241)."""
    from_env = os.environ.get(_PG_DSN_ENV_VAR)
    if from_env:
        return from_env
    return dotenv_values(".env").get(_PG_DSN_ENV_VAR) or None


def _dsn_with_short_connect_timeout(dsn: str) -> str:
    """Append a short libpq ``connect_timeout`` to ``dsn`` so an unreachable settings-table host
    degrades fast (AC2) instead of hanging boot."""
    if dsn.startswith(("postgres://", "postgresql://")):
        separator = "&" if "?" in dsn else "?"
        return f"{dsn}{separator}connect_timeout={_TABLE_SOURCE_CONNECT_TIMEOUT_SECONDS}"
    return f"{dsn} connect_timeout={_TABLE_SOURCE_CONNECT_TIMEOUT_SECONDS}"


def load_config() -> WombatConfig:
    """Load + validate config from the environment, or raise ConfigurationError loudly.

    Env vars and the repo-root ``.env`` (if present) are both read by pydantic-settings,
    with explicit env vars taking precedence over ``.env`` values (TK-186: the pre-pydantic
    ``os.environ`` check used to short-circuit before ``.env`` was ever consulted).

    TK-241 (DEC-43): if ``WOMBAT_PG_DSN`` resolves (env, else cwd-relative ``.env`` — never the
    settings table itself), ONE ``SettingsStore.get_all()`` populates ``_TABLE_SETTINGS_HOLDER``
    before ``WombatConfig()`` is constructed; the holder is cleared in a ``finally`` regardless of
    outcome, so a bare ``WombatConfig(...)`` call site never performs any settings-table I/O
    (AC4). A missing DSN is a silent no-op (byte-unchanged boot). An unreachable host, a query
    failure, or a missing table is caught, logged with exactly one ``logger.warning``, and
    treated as absent — this function NEVER raises for settings-store reasons (CON-3).

    A non-missing validation failure (e.g. a ``WOMBAT_STT_PROVIDER`` value outside its closed
    vocabulary) also fails loud, naming the offending variable (TK-187).
    """
    dsn = _resolve_pg_dsn()
    if dsn:
        # Deferred import: settings_store.py imports APP_EDITABLE_FIELDS/WombatConfig from this
        # module at its own top level, so importing SettingsStore at config.py's top level would
        # be circular. Deferring to here (only reached when a DSN is actually configured) breaks
        # the cycle without weakening either module's public surface.
        from wombat.settings_store import SettingsStore

        store = SettingsStore(_dsn_with_short_connect_timeout(dsn))
        try:
            _TABLE_SETTINGS_HOLDER.update(store.get_all())
        except Exception as exc:  # unreachable host, query/table failure, ... — CON-3
            logger.warning(
                "could not read wombat_settings from the configured WOMBAT_PG_DSN; treating "
                "the app-editable settings table as absent (%s)",
                exc,
            )
        finally:
            store.close()
    try:
        try:
            return WombatConfig()  # populated from env/.env/table by pydantic-settings
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
    finally:
        # ALWAYS clear, on every exit path (success, ConfigurationError, or a re-raised
        # ValidationError) — the next bare WombatConfig() construction, from any call site, must
        # never see a stale holder (AC4).
        _TABLE_SETTINGS_HOLDER.clear()


def resolve_wombat_zone(config: WombatConfig) -> ZoneInfo:
    """Resolve the ONE timezone every wombat tz consumer runs against (TK-228, DEC-40 —
    realizing the DEC-21/Q-15 "no caller hard-codes UTC or local independently" invariant).

    An explicit ``config.wombat_timezone`` constructs ``ZoneInfo(value)``; an unrecognized IANA
    key fails LOUD (``ConfigurationError`` naming ``WOMBAT_TIMEZONE``) rather than silently
    falling back. Unset (the default) resolves the HOST's own zone via
    ``tzlocal.get_localzone()`` — a real ``zoneinfo.ZoneInfo`` read from the OS (never a fixed
    UTC offset, never a DST-blind placebo); a resolution failure ALSO fails loud, instructing the
    operator to set ``WOMBAT_TIMEZONE`` explicitly. There is NO silent UTC fallback anywhere in
    this function — a silent fallback is exactly the ISS-8 live defect this ticket closes.
    """
    if config.wombat_timezone is not None:
        try:
            return ZoneInfo(config.wombat_timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ConfigurationError(
                f"invalid environment variable WOMBAT_TIMEZONE: {config.wombat_timezone!r} is "
                "not a recognized IANA timezone name; wombat will not start"
            ) from exc
    try:
        return tzlocal.get_localzone()
    except Exception as exc:
        raise ConfigurationError(
            "could not resolve the host's local timezone; set WOMBAT_TIMEZONE to a valid IANA "
            "zone name (e.g. America/New_York) in your .env; wombat will not start"
        ) from exc
