"""wombat.integrations.gcal.auth — Google Calendar OAuth2 credential lifecycle (TK-71, Q-57).

TK-71 is auth-lifecycle ONLY: obtain + store + refresh + scope-guard a Google
``calendar.readonly`` credential. It makes ZERO Google Calendar data-API calls (that is
TK-72). It exposes exactly ONE consumption seam — ``CalendarAuth.get_credentials()`` returns a
refreshed, scope-guarded ``google.oauth2.credentials.Credentials`` — which TK-72's poller
receives INJECTED (constructor arg); this module never calls the Calendar API itself.

Design (Q-57 BINDING rulings):
  * ``GCAL_SCOPES`` is the ONE hard-coded scope this ticket ever requests — calendar.readonly
    ONLY. Google Calendar write/insert/patch scope is NEVER granted in v1 (DEC-16).
    ``assert_readonly_scopes`` is the runnable scope-guard: it rejects (raises) any token
    carrying a scope beyond this set.
  * Secrets (``GOOGLE_OAUTH_CLIENT_ID`` / ``GOOGLE_OAUTH_CLIENT_SECRET``) come from
    ``WombatConfig`` (Q-57(b)) and are validated AT CONSTRUCTION of ``CalendarAuth`` — the
    TK-8 ``ComposeStage`` precedent — raising ``ConfigurationError`` naming the first
    missing/blank var. They are OPTIONAL on ``WombatConfig`` itself (not in ``REQUIRED_ENV``)
    so the drain spine/demo keep booting Google-less; only *this* component refuses to be
    built without them.
  * The token store is the OS keyring vault ONLY (``wombat.integrations.gcal.token_store``,
    Q-57(a)) — ``CalendarAuth`` depends on the ``TokenStore`` Protocol, never on ``keyring``
    directly, so tests inject an in-memory fake.
  * ``get_credentials()`` is the ONE seam: no stored token -> run the one-time interactive
    consent flow (``InstalledAppFlow``); a stored-but-expired token with a refresh token ->
    non-interactive refresh (``Credentials.refresh``), never prompting the user (AC2). Either
    path is scope-guarded before returning.
  * ``python -m wombat.integrations.gcal.auth`` is the one-time interactive consent CLI (Jim's
    bring-up step, not a test) — it just calls ``get_credentials()`` against an empty vault.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable

from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from wombat.config import ConfigurationError, WombatConfig, load_config
from wombat.integrations.gcal.token_store import KeyringTokenStore, TokenStore

logger = logging.getLogger(__name__)

# GCAL_SCOPES: hard-coded to calendar.readonly ONLY (DEC-16). AC1 asserts the OAuth flow is
# constructed with EXACTLY this tuple — never widened, never derived from config.
GCAL_SCOPES: tuple[str, ...] = ("https://www.googleapis.com/auth/calendar.readonly",)

_TOKEN_URI = "https://oauth2.googleapis.com/token"
_AUTH_URI = "https://accounts.google.com/o/oauth2/auth"


class ScopeViolationError(RuntimeError):
    """Raised when a credential carries a scope outside ``GCAL_SCOPES`` (write/manage scope
    present) — the scope-guard's rejection (AC1)."""


def assert_readonly_scopes(granted_scopes: Iterable[str] | None) -> None:
    """The scope-guard helper (AC1): reject ANY credential carrying a scope beyond
    ``GCAL_SCOPES``. Feed it a write/manage-scoped token and it raises ``ScopeViolationError``
    — the runnable proof that wombat never operates with more than calendar.readonly
    (DEC-16). ``granted_scopes`` accepts anything iterable-of-str, or ``None``; ``None``/empty
    is treated as "no scopes granted", which trivially satisfies the guard.
    """
    granted = set(granted_scopes) if granted_scopes else set()
    extra = granted - set(GCAL_SCOPES)
    if extra:
        raise ScopeViolationError(
            f"gcal credential carries scope(s) beyond calendar.readonly: {sorted(extra)}"
        )


def _client_config(client_id: str, client_secret: str) -> dict[str, dict[str, str]]:
    """The ``InstalledAppFlow`` client config shape, built from validated constructor
    values — never logged (it carries ``client_secret``)."""
    return {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": _AUTH_URI,
            "token_uri": _TOKEN_URI,
        }
    }


class CalendarAuth:
    """Obtain + store + refresh + scope-guard a Google Calendar read-only credential.

    Validates ``GOOGLE_OAUTH_CLIENT_ID``/``GOOGLE_OAUTH_CLIENT_SECRET`` AT CONSTRUCTION
    (TK-8 precedent) — building this with either missing/blank raises ``ConfigurationError``
    naming the first missing var. ``get_credentials()`` is the ONE seam TK-72 consumes.
    """

    def __init__(self, *, config: WombatConfig, token_store: TokenStore | None = None) -> None:
        client_id = (config.google_oauth_client_id or "").strip()
        if not client_id:
            raise ConfigurationError(
                "CalendarAuth: GOOGLE_OAUTH_CLIENT_ID is missing/blank; "
                "wombat cannot obtain a Google Calendar credential"
            )
        client_secret = (
            config.google_oauth_client_secret.get_secret_value().strip()
            if config.google_oauth_client_secret is not None
            else ""
        )
        if not client_secret:
            raise ConfigurationError(
                "CalendarAuth: GOOGLE_OAUTH_CLIENT_SECRET is missing/blank; "
                "wombat cannot obtain a Google Calendar credential"
            )
        self._client_id = client_id
        self._client_secret = client_secret
        self._token_store: TokenStore = (
            token_store if token_store is not None else KeyringTokenStore()
        )

    def get_credentials(self) -> Credentials:
        """Return a refreshed, scope-guarded ``Credentials`` — the ONE seam TK-72 consumes.

        No stored token -> run the one-time interactive consent flow. A stored, expired token
        with a refresh token -> non-interactive refresh (AC2, never prompts). Either way the
        result is scope-guarded before it is returned.
        """
        stored = self._token_store.load()
        creds: Credentials
        if stored is None:
            creds = self._run_interactive_consent()
        else:
            stored_info = json.loads(stored)
            # TK-168: assert the STORED token's own scopes BEFORE constructing Credentials —
            # ``from_authorized_user_info(..., scopes=list(GCAL_SCOPES))`` below overwrites
            # ``creds.scopes`` with the passed constant, so checking ``creds.scopes`` afterward
            # checks the constant against itself and can never catch a vaulted token that
            # actually granted a broader scope (older consent, manual edit, future scope change
            # without re-consent).
            assert_readonly_scopes(stored_info.get("scopes"))
            creds = Credentials.from_authorized_user_info(  # type: ignore[no-untyped-call]
                stored_info, scopes=list(GCAL_SCOPES)
            )
            if creds.expired and creds.refresh_token:
                logger.info("gcal: stored access token expired — refreshing non-interactively")
                creds.refresh(GoogleAuthRequest())  # type: ignore[no-untyped-call]
                self._token_store.save(creds.to_json())  # type: ignore[no-untyped-call]
        assert_readonly_scopes(creds.scopes or list(GCAL_SCOPES))
        return creds

    def _run_interactive_consent(self) -> Credentials:
        """The one-time interactive consent flow (fresh host, no stored credential, AC1).
        Never invoked on the refresh path (AC2)."""
        logger.info("gcal: no stored credential found — starting interactive OAuth consent")
        flow = InstalledAppFlow.from_client_config(
            _client_config(self._client_id, self._client_secret), scopes=list(GCAL_SCOPES)
        )
        creds: Credentials = flow.run_local_server(port=0)
        self._token_store.save(creds.to_json())  # type: ignore[no-untyped-call]
        return creds


def main() -> None:
    """The one-time interactive consent CLI: ``python -m wombat.integrations.gcal.auth``.
    Jim's bring-up step (not a test) — obtains a fresh credential (prompting for consent if
    none is stored yet) and stores it in the OS keyring vault."""
    logging.basicConfig(level=logging.INFO)
    config = load_config()
    creds = CalendarAuth(config=config).get_credentials()
    granted = sorted(creds.scopes or list(GCAL_SCOPES))
    logger.info("gcal OAuth consent complete; granted scopes: %s", granted)


if __name__ == "__main__":
    main()


__all__ = [
    "GCAL_SCOPES",
    "CalendarAuth",
    "ScopeViolationError",
    "assert_readonly_scopes",
    "main",
]
