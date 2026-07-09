"""wombat.integrations.gmail.auth — Gmail OAuth2 credential lifecycle (TK-75, Q-65).

Mirrors ``wombat.integrations.gcal.auth.CalendarAuth`` (TK-71) exactly: auth-lifecycle ONLY —
obtain + store + refresh + scope-guard a Gmail credential. It makes ZERO Gmail data-API calls
(that is ``GmailPoller``, this same ticket). It exposes exactly ONE consumption seam —
``GmailAuth.get_credentials()`` returns a refreshed, scope-guarded
``google.oauth2.credentials.Credentials`` — which ``GmailPoller`` receives INJECTED (an
``AuthorizedSession`` built from it at the composition root); this module never calls the Gmail
API itself.

Design (Q-65 BINDING rulings):
  * ``GMAIL_SCOPES`` is the ONE hard-coded scope this ticket ever requests — gmail.readonly
    ONLY (ruling 1, SUPERSEDES the earlier dual-scope plan). Google's ``gmail.compose`` scope
    permits SENDING, and a send-capable token must not exist on this host before TK-78's
    review-before-send machinery exists (CON-5 least-privilege). ``assert_gmail_readonly_scopes``
    is the runnable scope-guard: it rejects (raises) any token carrying a scope beyond this set
    — INCLUDING gmail.compose.
  * Credentials (``GOOGLE_OAUTH_CLIENT_ID`` / ``GOOGLE_OAUTH_CLIENT_SECRET``) are REUSED from
    ``WombatConfig`` (Q-65 ruling 2, extends Q-57(b)) — ONE Google Cloud app, scopes are
    per-token not per-client, so NO new env vars are introduced for gmail. Validated AT
    CONSTRUCTION of ``GmailAuth`` (the TK-8/TK-71 precedent), raising ``ConfigurationError``
    naming the first missing/blank var.
  * The token store is the OS keyring vault ONLY, under the SEPARATE ``gmail-oauth-token``
    account (``wombat.integrations.gmail.token_store``, Q-65 ruling 2) — ``GmailAuth`` depends
    on the ``TokenStore`` Protocol, never on ``keyring`` directly, so tests inject an in-memory
    fake.
  * ``get_credentials()`` is the ONE seam: no stored token -> run the one-time interactive
    consent flow (``InstalledAppFlow``); a stored-but-expired token with a refresh token ->
    non-interactive refresh (``Credentials.refresh``), never prompting the user. Either path is
    scope-guarded before returning.
  * ``python -m wombat.integrations.gmail.auth`` is the one-time interactive consent CLI (Jim's
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
from wombat.integrations.gmail.token_store import (
    GMAIL_KEYRING_ACCOUNT,
    KeyringTokenStore,
    TokenStore,
)

logger = logging.getLogger(__name__)

# GMAIL_SCOPES: hard-coded to gmail.readonly ONLY (Q-65 ruling 1). AC3 asserts the OAuth flow is
# constructed with EXACTLY this tuple — never widened, never derived from config. gmail.compose
# (send-capable) is NEVER requested or accepted here; that is TK-78's bring-up, deliberately.
GMAIL_SCOPES: tuple[str, ...] = ("https://www.googleapis.com/auth/gmail.readonly",)

_TOKEN_URI = "https://oauth2.googleapis.com/token"
_AUTH_URI = "https://accounts.google.com/o/oauth2/auth"


class ScopeViolationError(RuntimeError):
    """Raised when a credential carries a scope outside ``GMAIL_SCOPES`` (compose/send or any
    other scope present) — the scope-guard's rejection (Q-65 ruling 1)."""


def assert_gmail_readonly_scopes(granted_scopes: Iterable[str] | None) -> None:
    """The scope-guard helper (Q-65 ruling 1): reject ANY credential carrying a scope beyond
    ``GMAIL_SCOPES`` — including ``gmail.compose``, which is send-capable. Feed it a
    compose-scoped token and it raises ``ScopeViolationError`` — the runnable proof that wombat
    never operates a Gmail credential capable of sending before TK-78's review-before-send
    machinery exists (CON-5). ``granted_scopes`` accepts anything iterable-of-str, or ``None``;
    ``None``/empty is treated as "no scopes granted", which trivially satisfies the guard.
    """
    granted = set(granted_scopes) if granted_scopes else set()
    extra = granted - set(GMAIL_SCOPES)
    if extra:
        raise ScopeViolationError(
            f"gmail credential carries scope(s) beyond gmail.readonly: {sorted(extra)}"
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


class GmailAuth:
    """Obtain + store + refresh + scope-guard a Gmail read-only credential.

    Validates ``GOOGLE_OAUTH_CLIENT_ID``/``GOOGLE_OAUTH_CLIENT_SECRET`` AT CONSTRUCTION (reused
    from ``WombatConfig``, Q-65 ruling 2) — building this with either missing/blank raises
    ``ConfigurationError`` naming the first missing var. ``get_credentials()`` is the ONE seam
    ``GmailPoller`` consumes. Defaults to a ``KeyringTokenStore`` scoped to the SEPARATE
    ``gmail-oauth-token`` keyring account (distinct from gcal's).
    """

    def __init__(self, *, config: WombatConfig, token_store: TokenStore | None = None) -> None:
        client_id = (config.google_oauth_client_id or "").strip()
        if not client_id:
            raise ConfigurationError(
                "GmailAuth: GOOGLE_OAUTH_CLIENT_ID is missing/blank; "
                "wombat cannot obtain a Gmail credential"
            )
        client_secret = (
            config.google_oauth_client_secret.get_secret_value().strip()
            if config.google_oauth_client_secret is not None
            else ""
        )
        if not client_secret:
            raise ConfigurationError(
                "GmailAuth: GOOGLE_OAUTH_CLIENT_SECRET is missing/blank; "
                "wombat cannot obtain a Gmail credential"
            )
        self._client_id = client_id
        self._client_secret = client_secret
        self._token_store: TokenStore = (
            token_store
            if token_store is not None
            else KeyringTokenStore(account=GMAIL_KEYRING_ACCOUNT)
        )

    def get_credentials(self) -> Credentials:
        """Return a refreshed, scope-guarded ``Credentials`` — the ONE seam ``GmailPoller``
        consumes.

        No stored token -> run the one-time interactive consent flow. A stored, expired token
        with a refresh token -> non-interactive refresh (never prompts). Either way the result
        is scope-guarded before it is returned.
        """
        stored = self._token_store.load()
        creds: Credentials
        if stored is None:
            creds = self._run_interactive_consent()
        else:
            stored_info = json.loads(stored)
            # TK-168: assert the STORED token's own scopes BEFORE constructing Credentials —
            # ``from_authorized_user_info(..., scopes=list(GMAIL_SCOPES))`` below overwrites
            # ``creds.scopes`` with the passed constant, so checking ``creds.scopes`` afterward
            # checks the constant against itself and can never catch a vaulted token that
            # actually granted a broader scope (older consent, manual edit, future scope change
            # without re-consent).
            assert_gmail_readonly_scopes(stored_info.get("scopes"))
            creds = Credentials.from_authorized_user_info(  # type: ignore[no-untyped-call]
                stored_info, scopes=list(GMAIL_SCOPES)
            )
            if creds.expired and creds.refresh_token:
                logger.info("gmail: stored access token expired — refreshing non-interactively")
                creds.refresh(GoogleAuthRequest())  # type: ignore[no-untyped-call]
                self._token_store.save(creds.to_json())  # type: ignore[no-untyped-call]
        assert_gmail_readonly_scopes(creds.scopes or list(GMAIL_SCOPES))
        return creds

    def _run_interactive_consent(self) -> Credentials:
        """The one-time interactive consent flow (fresh host, no stored credential)."""
        logger.info("gmail: no stored credential found — starting interactive OAuth consent")
        flow = InstalledAppFlow.from_client_config(
            _client_config(self._client_id, self._client_secret), scopes=list(GMAIL_SCOPES)
        )
        creds: Credentials = flow.run_local_server(port=0)
        self._token_store.save(creds.to_json())  # type: ignore[no-untyped-call]
        return creds


def main() -> None:
    """The one-time interactive consent CLI: ``python -m wombat.integrations.gmail.auth``.
    Jim's bring-up step (not a test) — obtains a fresh credential (prompting for consent if
    none is stored yet) and stores it in the OS keyring vault under ``gmail-oauth-token``."""
    logging.basicConfig(level=logging.INFO)
    config = load_config()
    creds = GmailAuth(config=config).get_credentials()
    granted = sorted(creds.scopes or list(GMAIL_SCOPES))
    logger.info("gmail OAuth consent complete; granted scopes: %s", granted)


if __name__ == "__main__":
    main()


__all__ = [
    "GMAIL_SCOPES",
    "GmailAuth",
    "ScopeViolationError",
    "assert_gmail_readonly_scopes",
    "main",
]
