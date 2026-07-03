"""wombat.sources.bootstrap — build_source_registry (TK-16, Q-61/Q-67).

The composition root for input sources: wires the already-built auth (TK-71 ``CalendarAuth`` /
TK-75 ``GmailAuth``) through the ONE authorized-session factories (Q-61/Q-67,
``integrations.gcal.session.make_calendar_session`` / ``integrations.gmail.session.
make_gmail_session``) into the already-built pollers (TK-72 ``CalendarPoller`` / TK-75
``GmailPoller``) and registers each into a ``SourceRegistry`` (TK-3) over the injected
``WombatQueue`` (ASMP-2: enqueue-only — this module never drains).

Each source is registered INDEPENDENTLY (Q-67): zero configured sources yields an empty,
working registry (the Google-less boot TK-71 guaranteed is preserved — the drain spine/demo
must still boot without Google); one configured yields just that source; both configured
yields both.

CRITICAL (Q-61 binding, load-bearing): this module NEVER triggers interactive OAuth consent
at boot. ``CalendarAuth.get_credentials()``/``GmailAuth.get_credentials()`` run an interactive
browser consent flow when no token is stored yet. ``build_source_registry`` therefore checks,
for each source, that client_id/secret are configured AND ``token_store.load() is not None``
BEFORE ever calling the session factory (which calls ``get_credentials()``). A source with
config but no stored token is treated exactly like an unconfigured source: a LOUD log naming
the missing piece, and the source is skipped — never raised, never blocked. Interactive
consent is Jim's one-time ``python -m wombat.integrations.<src>.auth`` CLI step, never a
boot-time action.

TK-96: the wired/unwired poller-construction logic is factored into ``_build_gcal_poller``/
``_build_gmail_poller`` (returning the poller or ``None``, with the SAME loud-skip logging as
before — a behavior-preserving extraction, TK-16's own tests are the regression net) so
``build_brief_fetches`` can reuse the EXACT SAME wired/unwired decision the drain-side
``build_source_registry`` makes, rather than a second copy of the creds/token checks.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import NoReturn
from zoneinfo import ZoneInfo

from wombat.calendar.models import CalendarEvent
from wombat.config import ConfigurationError, WombatConfig
from wombat.integrations.gcal.poller import CalendarPoller
from wombat.integrations.gcal.session import make_calendar_session
from wombat.integrations.gcal.token_store import KeyringTokenStore as GcalKeyringTokenStore
from wombat.integrations.gcal.token_store import TokenStore as GcalTokenStore
from wombat.integrations.gmail.models import GmailMessageItem
from wombat.integrations.gmail.poller import GmailPoller
from wombat.integrations.gmail.session import make_gmail_session
from wombat.integrations.gmail.token_store import GMAIL_KEYRING_ACCOUNT
from wombat.integrations.gmail.token_store import KeyringTokenStore as GmailKeyringTokenStore
from wombat.integrations.gmail.token_store import TokenStore as GmailTokenStore
from wombat.sources.registry import Enqueuer, SourceRegistry

logger = logging.getLogger(__name__)

# Sane composition-root defaults (TK-13 tunables are NOT invented here — no ticket asked for a
# config field, so these are plain constructor defaults, overridable by an explicit caller arg).
DEFAULT_GCAL_POLL_INTERVAL_SECONDS = 300.0
DEFAULT_GMAIL_POLL_INTERVAL_SECONDS = 300.0


def _utc_now() -> datetime:
    """The real-clock default injected into a source's poller, mirroring the pollers' own
    ``_utc_now`` default (this module never reads real wall-clock time itself)."""
    return datetime.now(UTC)


def _has_google_client_credentials(config: WombatConfig) -> bool:
    """True when both GOOGLE_OAUTH_CLIENT_ID/SECRET are present and non-blank — the SAME
    presence check ``CalendarAuth``/``GmailAuth`` apply at construction (mirrored here so we
    can decide whether to build the auth object at all, without constructing it just to probe)."""
    client_id = (config.google_oauth_client_id or "").strip()
    if not client_id:
        return False
    if config.google_oauth_client_secret is None:
        return False
    return bool(config.google_oauth_client_secret.get_secret_value().strip())


def _build_gcal_poller(
    config: WombatConfig,
    *,
    tz: ZoneInfo,
    clock: Callable[[], datetime],
    poll_interval_seconds: float,
    token_store: GcalTokenStore | None,
) -> CalendarPoller | None:
    """Construct a ``CalendarPoller`` iff client creds AND a stored token are both present
    (Q-61); ``None`` otherwise, with the SAME loud-skip logging ``_maybe_register_gcal``
    (below) has always emitted — a behavior-preserving extraction (TK-96) so
    ``build_brief_fetches`` shares this exact wired/unwired decision."""
    store: GcalTokenStore = token_store if token_store is not None else GcalKeyringTokenStore()
    if not _has_google_client_credentials(config):
        logger.warning(
            "gcal source not wired: GOOGLE_OAUTH_CLIENT_ID/GOOGLE_OAUTH_CLIENT_SECRET not "
            "configured — skipping calendar source (boot continues Google-less)"
        )
        return None
    if store.load() is None:
        logger.warning(
            "gcal source not wired: no stored credential — run "
            "`python -m wombat.integrations.gcal.auth` once to grant consent, then restart"
        )
        return None
    # Token is confirmed present BEFORE the session factory (and thus get_credentials()) is
    # ever called — this path never triggers interactive OAuth consent (Q-61).
    session = make_calendar_session(config, token_store=store)
    return CalendarPoller(
        session=session,
        tz=tz,
        poll_interval_seconds=poll_interval_seconds,
        clock=clock,
    )


def _build_gmail_poller(
    config: WombatConfig,
    *,
    clock: Callable[[], datetime],
    poll_interval_seconds: float,
    token_store: GmailTokenStore | None,
) -> GmailPoller | None:
    """Construct a ``GmailPoller`` iff client creds AND a stored token are both present
    (Q-67); ``None`` otherwise, with the SAME loud-skip logging ``_maybe_register_gmail``
    (below) has always emitted — a behavior-preserving extraction (TK-96) so
    ``build_brief_fetches`` shares this exact wired/unwired decision."""
    store: GmailTokenStore = (
        token_store
        if token_store is not None
        else GmailKeyringTokenStore(account=GMAIL_KEYRING_ACCOUNT)
    )
    if not _has_google_client_credentials(config):
        logger.warning(
            "gmail source not wired: GOOGLE_OAUTH_CLIENT_ID/GOOGLE_OAUTH_CLIENT_SECRET not "
            "configured — skipping gmail source (boot continues Google-less)"
        )
        return None
    if store.load() is None:
        logger.warning(
            "gmail source not wired: no stored credential — run "
            "`python -m wombat.integrations.gmail.auth` once to grant consent, then restart"
        )
        return None
    # Token is confirmed present BEFORE the session factory (and thus get_credentials()) is
    # ever called — this path never triggers interactive OAuth consent (Q-67).
    session = make_gmail_session(config, token_store=store)
    return GmailPoller(
        session=session,
        poll_interval_seconds=poll_interval_seconds,
        clock=clock,
    )


def _maybe_register_gcal(
    registry: SourceRegistry,
    config: WombatConfig,
    *,
    tz: ZoneInfo,
    clock: Callable[[], datetime],
    poll_interval_seconds: float,
    token_store: GcalTokenStore | None,
) -> None:
    poller = _build_gcal_poller(
        config,
        tz=tz,
        clock=clock,
        poll_interval_seconds=poll_interval_seconds,
        token_store=token_store,
    )
    if poller is not None:
        registry.register(poller)


def _maybe_register_gmail(
    registry: SourceRegistry,
    config: WombatConfig,
    *,
    clock: Callable[[], datetime],
    poll_interval_seconds: float,
    token_store: GmailTokenStore | None,
) -> None:
    poller = _build_gmail_poller(
        config,
        clock=clock,
        poll_interval_seconds=poll_interval_seconds,
        token_store=token_store,
    )
    if poller is not None:
        registry.register(poller)


def build_source_registry(
    config: WombatConfig,
    queue: Enqueuer,
    *,
    tz: ZoneInfo,
    clock: Callable[[], datetime] = _utc_now,
    gcal_poll_interval_seconds: float = DEFAULT_GCAL_POLL_INTERVAL_SECONDS,
    gmail_poll_interval_seconds: float = DEFAULT_GMAIL_POLL_INTERVAL_SECONDS,
    gcal_token_store: GcalTokenStore | None = None,
    gmail_token_store: GmailTokenStore | None = None,
) -> SourceRegistry:
    """Assemble a ``SourceRegistry`` over ``queue`` (ASMP-2: enqueue-only) and register EACH
    of the gcal/gmail sources INDEPENDENTLY when its client credentials AND stored token both
    exist (Q-61/Q-67). Never raises for missing/absent Google config or tokens — a loud log
    names what is missing and the source is skipped; the returned registry is always usable,
    with zero, one, or both sources registered.

    ``tz``/``clock`` are injected (no config field is read internally here beyond the Google
    OAuth client id/secret) — callers supply the wombat civil-local tz (DEC-21) and, in tests,
    a fake clock. ``gcal_token_store``/``gmail_token_store`` default to the real OS-keyring
    ``TokenStore`` adapters; tests inject in-memory fakes so this function never touches the
    real vault outside the live smokes.
    """
    registry = SourceRegistry(queue)
    _maybe_register_gcal(
        registry,
        config,
        tz=tz,
        clock=clock,
        poll_interval_seconds=gcal_poll_interval_seconds,
        token_store=gcal_token_store,
    )
    _maybe_register_gmail(
        registry,
        config,
        clock=clock,
        poll_interval_seconds=gmail_poll_interval_seconds,
        token_store=gmail_token_store,
    )
    return registry


def _raising_calendar_fetch() -> NoReturn:
    """The UNWIRED calendar placeholder (TK-96): raises so ``BriefGatherStage``'s per-source
    guarded ``except`` degrades this source to ``calendar_unavailable=True`` — never a crash."""
    raise ConfigurationError(
        "build_brief_fetches: gcal source not wired (see the loud skip log above); "
        "calendar is unavailable for the brief"
    )


def _raising_gmail_fetch() -> NoReturn:
    """The UNWIRED gmail placeholder (TK-96): raises so ``BriefGatherStage``'s per-source
    guarded ``except`` degrades this source to ``gmail_unavailable=True`` — never a crash."""
    raise ConfigurationError(
        "build_brief_fetches: gmail source not wired (see the loud skip log above); "
        "gmail is unavailable for the brief"
    )


def _resolve_calendar_fetch(
    gcal_poller: CalendarPoller | None,
) -> Callable[[], list[CalendarEvent]]:
    """A WIRED poller's ``fetch_window``, or the raising placeholder when unwired (TK-96)."""
    if gcal_poller is None:
        return _raising_calendar_fetch
    return gcal_poller.fetch_window


def _resolve_gmail_fetch(
    gmail_poller: GmailPoller | None,
) -> Callable[[], list[GmailMessageItem]]:
    """A WIRED poller's ``fetch_recent``, or the raising placeholder when unwired (TK-96)."""
    if gmail_poller is None:
        return _raising_gmail_fetch
    return gmail_poller.fetch_recent


@dataclass(frozen=True, slots=True)
class BriefFetches:
    """The two zero-arg read seams ``BriefGatherStage`` (TK-98) calls once each (TK-96).

    A WIRED source binds the poller's RAISING ``fetch_window``/``fetch_recent`` (the SAME
    composition-time window args ``build_source_registry`` configures its poller with); an
    UNWIRED source binds a RAISING ``ConfigurationError`` placeholder. Either way
    ``BriefGatherStage``'s per-source guarded ``except`` degrades that ONE source to an empty
    slice + its own ``*_unavailable=True`` flag — a Google-less boot still yields an honest
    degraded brief, never a crash.
    """

    fetch_calendar: Callable[[], list[CalendarEvent]]
    fetch_gmail: Callable[[], list[GmailMessageItem]]


def build_brief_fetches(
    config: WombatConfig,
    *,
    tz: ZoneInfo,
    clock: Callable[[], datetime] = _utc_now,
    gcal_poll_interval_seconds: float = DEFAULT_GCAL_POLL_INTERVAL_SECONDS,
    gmail_poll_interval_seconds: float = DEFAULT_GMAIL_POLL_INTERVAL_SECONDS,
    gcal_token_store: GcalTokenStore | None = None,
    gmail_token_store: GmailTokenStore | None = None,
) -> BriefFetches:
    """Assemble the morning brief's ``BriefFetches`` (TK-96) over the SAME wired/unwired
    decision ``build_source_registry`` makes (Q-61/Q-67, via the shared ``_build_gcal_poller``/
    ``_build_gmail_poller`` helpers) — never constructs a second poller, never triggers
    interactive OAuth consent, never raises here (a raising PLACEHOLDER is bound for an unwired
    source instead; the raise itself happens lazily, only if/when ``BriefGatherStage`` calls it).
    """
    gcal_poller = _build_gcal_poller(
        config,
        tz=tz,
        clock=clock,
        poll_interval_seconds=gcal_poll_interval_seconds,
        token_store=gcal_token_store,
    )
    gmail_poller = _build_gmail_poller(
        config,
        clock=clock,
        poll_interval_seconds=gmail_poll_interval_seconds,
        token_store=gmail_token_store,
    )
    return BriefFetches(
        fetch_calendar=_resolve_calendar_fetch(gcal_poller),
        fetch_gmail=_resolve_gmail_fetch(gmail_poller),
    )


__all__ = [
    "DEFAULT_GCAL_POLL_INTERVAL_SECONDS",
    "DEFAULT_GMAIL_POLL_INTERVAL_SECONDS",
    "BriefFetches",
    "build_brief_fetches",
    "build_source_registry",
]
