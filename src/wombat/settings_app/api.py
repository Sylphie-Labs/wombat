"""wombat.settings_app.api — the Electron renderer's CONFIG backend (TK-197, EP-32, DEC-31/32).

``create_app`` builds a small FastAPI app over THREE existing seams, none of which touches the
wombat runtime:
  * ``wombat.settings_store`` — ``SettingsStore`` (TK-240, DEC-43) is the Postgres-backed
    reader/writer over the ``wombat_settings`` table; ``wombat.config.APP_EDITABLE_FIELDS``
    (TK-196) still names the admitted app-editable settings for the response view (never via
    ``WombatConfig``/``load_config`` itself, which would additionally require the DeepSeek env
    vars ``settings_app`` has no business needing).
  * ``wombat.voice.key_store`` — ``VoiceKeyStore`` (TK-188) is the write-only vault seam for
    cloud voice-provider API keys; this module never reads a stored key back out, only whether
    one is present (``get(provider) is not None``).
  * ``wombat.external_store`` — ``ExternalItemStore`` (TK-246, DEC-45(e)) is the READ-ONLY seam
    over ``wombat_external_items`` behind ``GET /external/calendar``/``GET /external/gmail``; the
    TK-245 runtime sync is the only writer, so no write/delete route exists here.

Every route requires the ``X-Wombat-Token`` header to equal the per-launch token
``create_app`` was constructed with (the ``__main__`` handshake, DEC-31) — a missing or wrong
token is a 401 on every route, never a 404 (that would leak route existence to an unauthenticated
caller).

DEGRADE POSTURE (TK-242, DEC-43 ruling — loud read-only): a ``None`` store (no DSN at boot) or a
store call that raises degrades GET to a 200 with every admitted field ``null`` plus
``storage_unavailable: true``; the same conditions degrade PUT to a 503 with a fixed, generic
detail naming the settings storage — never a bare 500, never a secret in the body. ``GET
/external/calendar``/``GET /external/gmail`` (TK-246) ride the SAME read-degrade shape verbatim: a
``None`` ``external_store`` or a raising read is a 200 with empty ``items`` and
``storage_unavailable: true``.

STRUCTURAL: this module (and the ``wombat.settings_app`` package as a whole) imports NOTHING from
``wombat.bootstrap`` or ``wombat.runtime`` — the settings app must run while ``serve()`` is down
(``tests/settings_app/test_api.py``'s subprocess-clean check proves this at import time).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, field_validator

from wombat.config import APP_EDITABLE_FIELDS
from wombat.external_store import ExternalItemStore
from wombat.settings_store import SettingsStore
from wombat.voice.key_store import VoiceKeyStore, VoiceKeyStoreError

# TK-246 (DEC-45(e), ruling v2.68 r4): the default lookback/lookahead window (in hours) for
# GET /external/calendar when the caller omits ``window_hours``.
DEFAULT_CALENDAR_WINDOW_HOURS = 168

# TK-246: the default row count for GET /external/gmail when the caller omits ``limit``.
DEFAULT_GMAIL_LIMIT = 50

# Fixed, generic PUT-degrade detail (DEC-43 ruling) — NEVER derived from the underlying exception,
# so a storage failure can never echo a secret (e.g. a DSN fragment) back to the caller.
_STORAGE_UNAVAILABLE_DETAIL = "settings storage is unavailable; try again later"

# The loopback bind address this API is served on — NEVER 0.0.0.0 (DEC-30/31). A pinned module
# constant so a structural test can assert it directly.
BIND_HOST = "127.0.0.1"

# The cloud voice-provider key vault's closed provider vocabulary (TK-188) — "local" has no key
# to store, so it is deliberately excluded here (distinct from WombatConfig's provider-selection
# Literal, which also admits "local").
KEY_PROVIDERS: tuple[str, ...] = ("elevenlabs", "deepgram", "fish")

_KeyProvider = Literal["elevenlabs", "deepgram", "fish"]


class SettingsUpdate(BaseModel):
    """Mirror of ``WombatConfig``'s app-editable fields (``wombat.config.APP_EDITABLE_FIELDS``)
    — every field Optional (a PUT need only carry the fields it changes) with the SAME closed
    vocabulary as ``WombatConfig`` (an out-of-vocab value 422s here identically to how it would
    fail ``load_config``). ``extra="forbid"`` — an unknown key 422s rather than silently
    vanishing.

    Kept in lock-step with ``WombatConfig`` by
    ``tests/settings_app/test_api.py::test_mirror_model_field_set_matches_app_editable_fields``
    and ``::test_mirror_model_literal_vocab_matches_wombat_config`` — a future admitted-field
    addition (or vocabulary change) to ``WombatConfig`` fails loudly there instead of drifting
    silently out of sync with this API.
    """

    model_config = ConfigDict(extra="forbid")

    wombat_stt_provider: Literal["local", "deepgram", "elevenlabs", "fish"] | None = None
    wombat_tts_provider: Literal["local", "deepgram", "elevenlabs", "fish"] | None = None
    wombat_tts_voice_id: str | None = None
    wombat_stt_model: str | None = None
    wombat_assistant_name: str | None = None
    wombat_persona_brevity: Literal["terse", "balanced", "expansive"] | None = None
    wombat_persona_warmth: Literal["reserved", "neutral", "warm"] | None = None
    wombat_persona_directness: Literal["gentle", "plain", "blunt"] | None = None
    wombat_persona_humor: Literal["none", "dry"] | None = None
    wombat_persona_proactivity: Literal["minimal", "balanced", "forward"] | None = None
    # TK-224 (EP-32, Q-111(b)): mirrors WombatConfig.wombat_voice_enabled, newly admitted to
    # APP_EDITABLE_FIELDS — a bool, not a Literal, so it is exempt from the mirror test's
    # vocabulary check (there is no vocabulary to drift).
    wombat_voice_enabled: bool | None = None


class KeyBody(BaseModel):
    """``PUT /keys/{provider}`` request body — a single non-blank key string."""

    model_config = ConfigDict(extra="forbid")

    key: str

    @field_validator("key")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("key must not be blank")
        return value


def _settings_view(existing: dict[str, Any]) -> dict[str, Any]:
    """The admitted-field-only view of ``existing`` — every ``APP_EDITABLE_FIELDS`` key, ``null``
    when absent from the file."""
    return {field: existing.get(field) for field in APP_EDITABLE_FIELDS}


def _key_presence(key_store: VoiceKeyStore) -> dict[str, bool]:
    """Presence booleans ONLY — never the stored key itself (DEC-32). A broken vault degrades a
    provider to ``False`` rather than 500ing the whole read (CON-3)."""
    presence: dict[str, bool] = {}
    for provider in KEY_PROVIDERS:
        try:
            presence[provider] = key_store.get(provider) is not None
        except Exception:
            presence[provider] = False
    return presence


def create_app(
    store: SettingsStore | None,
    key_store: VoiceKeyStore,
    token: str,
    external_store: ExternalItemStore | None = None,
) -> FastAPI:
    """Build the settings API (TK-197). ``token`` is the per-launch handshake secret every route
    requires via the ``X-Wombat-Token`` header (the ``__main__`` handshake, DEC-31).

    ``store`` is ``None`` when ``__main__`` found no usable ``WOMBAT_PG_DSN`` at boot — the app
    still serves, permanently in the read-only degrade posture (TK-242, DEC-43 ruling).

    ``external_store`` (TK-246, DEC-45(e)) is the read-only seam over ``wombat_external_items``
    for ``GET /external/calendar``/``GET /external/gmail`` — ``None`` the same way ``store`` can
    be, riding the SAME loud-degrade posture (a ``None`` store or a raising read returns 200 with
    empty ``items`` and ``storage_unavailable: true``, never a bare 500). No write/delete route
    exists here; the TK-245 runtime sync is the only writer."""

    app = FastAPI(title="wombat-settings")

    def _require_token(
        x_wombat_token: str | None = Header(default=None, alias="X-Wombat-Token"),
    ) -> None:
        if x_wombat_token != token:
            raise HTTPException(status_code=401, detail="missing or invalid token")

    @app.get("/settings", dependencies=[Depends(_require_token)])
    def get_settings() -> dict[str, Any]:
        existing: dict[str, Any] = {}
        unavailable = True
        if store is not None:
            try:
                existing = store.get_all()
                unavailable = False
            except Exception:
                existing = {}
        return {
            "settings": _settings_view(existing),
            "keys": _key_presence(key_store),
            "storage_unavailable": unavailable,
        }

    @app.put("/settings", dependencies=[Depends(_require_token)])
    def put_settings(body: SettingsUpdate) -> dict[str, Any]:
        if store is None:
            raise HTTPException(status_code=503, detail=_STORAGE_UNAVAILABLE_DETAIL)
        try:
            mapping = body.model_dump(exclude_unset=True)
            if mapping:
                store.put(mapping)
            existing = store.get_all()
        except Exception as exc:
            raise HTTPException(status_code=503, detail=_STORAGE_UNAVAILABLE_DETAIL) from exc
        return {"settings": _settings_view(existing)}

    @app.put("/keys/{provider}", dependencies=[Depends(_require_token)])
    def put_key(provider: _KeyProvider, body: KeyBody) -> dict[str, bool]:
        try:
            key_store.set(provider, body.key)
        except VoiceKeyStoreError as exc:
            # The detail is a fixed, generic message — NEVER derived from the key or from
            # str(exc) — so a vault-write failure can never echo a secret back (DEC-32).
            raise HTTPException(
                status_code=500, detail=f"failed to store the {provider} key"
            ) from exc
        return {"ok": True}

    @app.get("/external/calendar", dependencies=[Depends(_require_token)])
    def get_external_calendar(window_hours: int = DEFAULT_CALENDAR_WINDOW_HOURS) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        unavailable = True
        if external_store is not None:
            try:
                now = datetime.now(UTC)
                rows = external_store.get_window(
                    "gcal", now, now + timedelta(hours=window_hours)
                )
                items = [row["payload"] for row in rows]
                unavailable = False
            except Exception:
                items = []
        return {"items": items, "storage_unavailable": unavailable}

    @app.get("/external/gmail", dependencies=[Depends(_require_token)])
    def get_external_gmail(limit: int = DEFAULT_GMAIL_LIMIT) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        unavailable = True
        if external_store is not None:
            try:
                rows = external_store.get_recent("gmail", limit)
                items = [row["payload"] for row in rows]
                unavailable = False
            except Exception:
                items = []
        return {"items": items, "storage_unavailable": unavailable}

    return app


__all__ = [
    "BIND_HOST",
    "DEFAULT_CALENDAR_WINDOW_HOURS",
    "DEFAULT_GMAIL_LIMIT",
    "KEY_PROVIDERS",
    "KeyBody",
    "SettingsUpdate",
    "create_app",
]
