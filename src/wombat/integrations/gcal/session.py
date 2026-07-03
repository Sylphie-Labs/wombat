"""wombat.integrations.gcal.session — make_calendar_session (TK-16, Q-61).

The Q-61 BINDING ruling: this is the ONE place
``google.auth.transport.requests.AuthorizedSession`` is constructed for calendar reads.
``CalendarPoller`` (TK-72) is designed to receive an authorized HTTP session INJECTED (it
depends only on the minimal ``_CalendarSession`` Protocol); nothing in ``src/`` previously
constructed that session for it — this module owns that wire and nothing else.

``make_calendar_session`` builds a ``CalendarAuth`` from the injected config/token_store,
pulls its credentials via ``CalendarAuth.get_credentials()`` (TK-71's one consumption seam),
and wraps them in an ``AuthorizedSession``. It does no polling, no scope logic, no token
persistence of its own — all of that is TK-71's job; this module is pure composition.
"""

from __future__ import annotations

from google.auth.transport.requests import AuthorizedSession

from wombat.config import WombatConfig
from wombat.integrations.gcal.auth import CalendarAuth
from wombat.integrations.gcal.token_store import TokenStore


def make_calendar_session(
    config: WombatConfig, *, token_store: TokenStore | None = None
) -> AuthorizedSession:
    """Build the real authorized HTTP session ``CalendarPoller`` receives injected.

    Constructs ``CalendarAuth(config=config, token_store=token_store)`` and wraps its
    ``get_credentials()`` result in an ``AuthorizedSession`` — the ONE place this happens
    for calendar reads (Q-61). Callers that must avoid triggering interactive OAuth consent
    at boot (e.g. ``build_source_registry``) are responsible for confirming a token is already
    stored BEFORE calling this function; ``get_credentials()`` itself will run the interactive
    flow if no token is stored.
    """
    auth = CalendarAuth(config=config, token_store=token_store)
    credentials = auth.get_credentials()
    return AuthorizedSession(credentials)  # type: ignore[no-untyped-call]


__all__ = ["make_calendar_session"]
