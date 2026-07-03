"""wombat.integrations.gmail.session — make_gmail_session (TK-16, Q-67).

Mirrors ``wombat.integrations.gcal.session.make_calendar_session`` exactly, over
``GmailAuth`` (TK-75). This is the ONE place
``google.auth.transport.requests.AuthorizedSession`` is constructed for Gmail reads —
``GmailPoller`` depends only on the minimal ``_GmailSession`` Protocol and receives its
session INJECTED; nothing in ``src/`` previously constructed that session for it.
"""

from __future__ import annotations

from google.auth.transport.requests import AuthorizedSession

from wombat.config import WombatConfig
from wombat.integrations.gmail.auth import GmailAuth
from wombat.integrations.gmail.token_store import TokenStore


def make_gmail_session(
    config: WombatConfig, *, token_store: TokenStore | None = None
) -> AuthorizedSession:
    """Build the real authorized HTTP session ``GmailPoller`` receives injected.

    Constructs ``GmailAuth(config=config, token_store=token_store)`` and wraps its
    ``get_credentials()`` result in an ``AuthorizedSession`` — the ONE place this happens
    for Gmail reads (Q-67, mirroring Q-61's gcal ruling). Callers that must avoid triggering
    interactive OAuth consent at boot (e.g. ``build_source_registry``) are responsible for
    confirming a token is already stored BEFORE calling this function; ``get_credentials()``
    itself will run the interactive flow if no token is stored.
    """
    auth = GmailAuth(config=config, token_store=token_store)
    credentials = auth.get_credentials()
    return AuthorizedSession(credentials)  # type: ignore[no-untyped-call]


__all__ = ["make_gmail_session"]
