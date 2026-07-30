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

TK-176: ``_maybe_register_feedback`` registers TK-51's ``FeedbackInputSource`` under id
``"feedback"`` following the EXACT SAME loud-skip pattern as ``_maybe_register_gcal``/
``_maybe_register_gmail`` above — iff ``config.wombat_feedback_file`` is non-blank; otherwise ONE
loud log naming ``WOMBAT_FEEDBACK_FILE`` and the source is skipped (never raised). Registration-
not-rewrite (DEC-5/TK-161): ``SourceRegistry`` itself is untouched.

TK-177 (EP-18, Q-92): ``GmailWithReplyIntents`` is the live reply-intent EMISSION point — a thin
``InputSource`` wrapper that REPLACES the bare ``GmailPoller`` in ``_maybe_register_gmail`` (same
id ``"gmail"``, same Q-67 construction guards + loud-skip; ``build_brief_fetches`` is untouched —
it keeps reading the RAW, unwrapped poller via ``_build_gmail_poller``). Its ``poll()`` delegates
to the wrapped poller, then for EACH returned event: ``GmailMessageItem.from_payload`` ->
``triage_message`` (metadata-only, TK-76) -> ``reply_intent.build`` (TK-80; ``None`` for a
NORMAL-band message) -> a SECOND ``SourceEvent`` keyed ``f"reply:{message_id}"`` carrying the
sanitized ``ReplyIntent``'s payload (``item_kind="draft"``). No registry/queue change: ``sources.
registry.SourceRegistry`` already enqueues every polled event under
``idempotency_key(source.id, event.event_key)`` (TK-12), so the draft item's key
(``idempotency_key("gmail", "reply:<message_id>")``) is deterministic and distinct from the
message item's own key — a re-poll of the same message is ``ALREADY_QUEUED`` on both events. The
triage rule set is loaded ONCE (``load_triage_rules()``, at source-construction time in
``_maybe_register_gmail``), never per poll.

TK-162 (EP-29, Q-97): ``_maybe_register_asr`` registers ``ASRSource`` (``sources/asr.py``) under
id ``"asr"`` following the SAME loud-skip pattern as the sources above, but with TWO independent
skip conditions instead of one: an unset/blank ``config.wombat_asr_drop_dir`` (naming
``WOMBAT_ASR_DROP_DIR``, checked here), and separately no ``Transcriber`` being constructible at
all (TK-193: delegated to ``voice.select.build_transcriber``, which already logs LOUD naming the
exact gap — local faster-whisper absent, or a selected cloud provider's key/extra/voice_id gap
falling through to a local build that itself fails) — either skips the source, never raises.
Registration-not-rewrite (DEC-5/TK-161): ``SourceRegistry``/``sources/base.py`` are untouched.

TK-212 (EP-34, DEC-35 + DEC-37(f), Q-109(c)): ``make_persona_command_hook`` builds ``ASRSource``'s
optional ``command_hook`` seam — a matched persona voice command is intercepted AFTER
transcription and BEFORE enqueue, so it never enters the queue, is never gate-rated, and never
reaches a mouth. ``build_source_registry``/``_maybe_register_asr`` gain optional ``live_persona``/
``speak`` kwargs (both default ``None``, behavior-preserving); the hook is constructed and threaded
into ``ASRSource`` ONLY when ``live_persona`` is not ``None`` — a caller that doesn't wire a
``LivePersona`` gets today's ``ASRSource`` exactly, no interception at all.

TK-213 (EP-35, DEC-36/DEC-37(h)): ``make_persona_feedback_hook`` builds ``ASRSource``'s optional
``feedback_hook`` seam — a matched closed-lexicon feedback phrase is recorded (never consumed;
the utterance still enqueues normally) via an injected ``recorder`` closure over
``wombat.persona.feedback.detect_feedback_token``. ``build_source_registry``/
``_maybe_register_asr`` gain an optional ``persona_feedback_recorder`` kwarg (default ``None``,
behavior-preserving); the hook is constructed and threaded into ``ASRSource`` ONLY when it is not
``None`` — a caller that doesn't wire a recorder gets today's ``ASRSource`` exactly, no feedback
recording at all. This module only ever imports ``wombat.persona.feedback`` — it never imports
``wombat.behavior.event_log`` itself; the ``recorder`` closure (built by ``bootstrap.py``, over
the ONE shared ``RuntimeBundle.behavior_event_log`` instance) is handed in fully assembled.

TK-245 (DEC-45(c)/(d), ruling v2.68 r6): ``build_external_item_sink`` builds the ``SourceRegistry``
``sink`` seam — an explicit, per-source WHITELIST projection into ``wombat_external_items``. Only
``gcal``/``gmail`` events are ever projected; any other source id is silently ignored (no
dict-copy-of-whatever-shows-up). ``gcal`` rows store the ``SourceEvent`` payload verbatim
(``CalendarEvent.to_payload`` is body-free by construction). ``gmail`` rows store EXACTLY five
explicit keys (``message_id``/``subject``/``sender``/``received_at``/``priority_band``) — NEVER a
dict-copy-minus-body, since the raw payload carries the raw message body under its own guarded
key (Q-65, see ``tests/integrations/gmail/test_body_key_guard.py``; this module never references
it, staying outside that guard's sanctioned allowlist) — with ``priority_band``
recomputed via a pure ``triage_message`` call over the ``TriageRules`` ``_build_gmail_source``
loaded ONCE at composition (never reloaded here, never per-poll); a ``reply:``-prefixed
``event_key`` (``GmailWithReplyIntents``'s derived draft-item event) is skipped — a ``ReplyIntent``
payload never lands in this table. ``build_source_registry`` threads the sink into the
``SourceRegistry`` constructor only when an ``external_item_store`` is supplied; the default
(``None``) leaves poll behavior byte-unchanged (AC3).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn
from zoneinfo import ZoneInfo

from wombat.calendar.models import CalendarEvent
from wombat.chat_turns import ChatTurnStore
from wombat.config import ConfigurationError, WombatConfig
from wombat.external_store import ExternalItem, ExternalItemStore
from wombat.integrations.gcal.poller import CalendarPoller
from wombat.integrations.gcal.session import make_calendar_session
from wombat.integrations.gcal.token_store import KeyringTokenStore as GcalKeyringTokenStore
from wombat.integrations.gcal.token_store import TokenStore as GcalTokenStore
from wombat.integrations.gmail import reply_intent
from wombat.integrations.gmail.models import GmailMessageItem
from wombat.integrations.gmail.poller import GmailPoller
from wombat.integrations.gmail.session import make_gmail_session
from wombat.integrations.gmail.token_store import GMAIL_KEYRING_ACCOUNT
from wombat.integrations.gmail.token_store import KeyringTokenStore as GmailKeyringTokenStore
from wombat.integrations.gmail.token_store import TokenStore as GmailTokenStore
from wombat.integrations.gmail.triage import TriageRules, load_triage_rules, triage_message
from wombat.persona.commands import apply, parse_persona_command
from wombat.persona.feedback import FeedbackToken, detect_feedback_token
from wombat.persona.live import LivePersona
from wombat.sources.asr import ASRSource
from wombat.sources.base import InputSource, SourceEvent
from wombat.sources.registry import Enqueuer, Sink, SourceRegistry
from wombat.user_model.feedback_source import FeedbackInputSource
from wombat.voice.select import build_transcriber

logger = logging.getLogger(__name__)

# Sane composition-root defaults (TK-13 tunables are NOT invented here — no ticket asked for a
# config field, so these are plain constructor defaults, overridable by an explicit caller arg).
DEFAULT_GCAL_POLL_INTERVAL_SECONDS = 300.0
DEFAULT_GMAIL_POLL_INTERVAL_SECONDS = 300.0
DEFAULT_FEEDBACK_POLL_INTERVAL_SECONDS = 300.0
DEFAULT_ASR_POLL_INTERVAL_SECONDS = 2.0


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
    # ever called — this path never triggers interactive OAuth consent (Q-61). But a stored
    # token can still be expired/revoked: TK-253 (DEC-49, CRF-6 precedent) — a bad stored
    # credential degrades exactly like the no-stored-credential branch above, not a boot crash.
    try:
        session = make_calendar_session(config, token_store=store)
    except Exception:
        logger.warning(
            "gcal source not wired: stored credential failed to refresh — run "
            "`python -m wombat.integrations.gcal.auth` once to re-consent, then restart",
            exc_info=True,
        )
        return None
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
    # ever called — this path never triggers interactive OAuth consent (Q-67). But a stored
    # token can still be expired/revoked: TK-253 (DEC-49, CRF-6 precedent) — a bad stored
    # credential degrades exactly like the no-stored-credential branch above, not a boot crash.
    try:
        session = make_gmail_session(config, token_store=store)
    except Exception:
        logger.warning(
            "gmail source not wired: stored credential failed to refresh — run "
            "`python -m wombat.integrations.gmail.auth` once to re-consent, then restart",
            exc_info=True,
        )
        return None
    return GmailPoller(
        session=session,
        poll_interval_seconds=poll_interval_seconds,
        clock=clock,
    )


class GmailWithReplyIntents:
    """Wraps an ``InputSource`` (production: a wired ``GmailPoller``) and additionally emits a
    sanitized ``ReplyIntent`` draft-item ``SourceEvent`` for each HIGH-triage message (TK-177,
    EP-18, Q-92) — the live reply-intent EMISSION point at the Gmail source seam. See the module
    docstring for the full design. ``id``/``poll_interval_seconds`` mirror the wrapped source
    exactly, so this is a drop-in replacement wherever a bare ``GmailPoller`` was registered.
    """

    id: str = "gmail"

    def __init__(self, *, wrapped: InputSource, rules: TriageRules) -> None:
        self._wrapped = wrapped
        self._rules = rules
        self.poll_interval_seconds = wrapped.poll_interval_seconds

    async def start(self) -> None:
        await self._wrapped.start()

    async def stop(self) -> None:
        await self._wrapped.stop()

    async def poll(self) -> list[SourceEvent]:
        """Delegate to the wrapped source, then append ONE additional draft-item ``SourceEvent``
        per HIGH-triage message (``reply_intent.build`` returns ``None`` for a NORMAL-band
        message, emitting nothing for it)."""
        events = await self._wrapped.poll()
        emitted = list(events)
        for event in events:
            item = GmailMessageItem.from_payload(event.payload)
            triage = triage_message(item, self._rules)
            intent = reply_intent.build(item, triage)
            if intent is not None:
                emitted.append(
                    SourceEvent(
                        event_key=f"reply:{item.message_id}", payload=intent.to_payload()
                    )
                )
        return emitted


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


def _build_gmail_source(
    config: WombatConfig,
    *,
    clock: Callable[[], datetime],
    poll_interval_seconds: float,
    token_store: GmailTokenStore | None,
) -> tuple[InputSource | None, TriageRules | None]:
    """Build the wired ``GmailWithReplyIntents`` source (TK-177) plus the ``TriageRules`` it
    loaded, or ``(None, None)`` when gmail is unwired — the SAME wired/unwired decision
    ``_build_gmail_poller`` makes. TK-245: the returned ``TriageRules`` is reused, never reloaded,
    by ``build_external_item_sink``'s gmail projection — ``load_triage_rules()`` runs at most
    ONCE per ``build_source_registry`` call."""
    poller = _build_gmail_poller(
        config,
        clock=clock,
        poll_interval_seconds=poll_interval_seconds,
        token_store=token_store,
    )
    if poller is None:
        return None, None
    # TK-177: wrap the wired poller so reply-intent emission rides the SAME registration — never
    # a second poll loop, never a registry/queue change (Q-92).
    rules = load_triage_rules()
    return GmailWithReplyIntents(wrapped=poller, rules=rules), rules


def build_external_item_sink(
    store: ExternalItemStore,
    *,
    gmail_rules: TriageRules | None,
    clock: Callable[[], datetime] = _utc_now,
) -> Callable[[str, list[SourceEvent]], None]:
    """TK-245 (DEC-45(c)/(d), ruling v2.68 r6): the ``SourceRegistry`` sink — see the module
    docstring for the full whitelist/projection design. ``gmail_rules`` is ``None`` only when
    gmail itself is unwired, in which case no ``"gmail"``-sourced events are ever produced by the
    registry, so that branch degrades to a silent skip rather than raising (defensive, never
    reached in practice)."""

    def sink(source_id: str, events: list[SourceEvent]) -> None:
        items: list[ExternalItem]
        if source_id == "gcal":
            items = [
                ExternalItem(
                    item_key=event.event_key,
                    payload=event.payload,
                    occurs_at=datetime.fromisoformat(event.payload["start"]),
                )
                for event in events
            ]
        elif source_id == "gmail":
            if gmail_rules is None:
                return
            items = []
            for event in events:
                if event.event_key.startswith("reply:"):
                    continue  # ruling r1: the derived reply-intent event never lands here
                message = GmailMessageItem.from_payload(event.payload)
                triage = triage_message(message, gmail_rules)
                payload = {
                    "message_id": message.message_id,
                    "subject": message.subject,
                    "sender": message.sender,
                    "received_at": message.received_at.isoformat(),
                    "priority_band": triage.priority_band.value,
                }
                items.append(
                    ExternalItem(
                        item_key=event.event_key,
                        payload=payload,
                        occurs_at=message.received_at,
                    )
                )
        else:
            return  # whitelist: only gcal/gmail are ever projected
        if not items:
            return
        store.upsert_many(source_id, items, fetched_at=clock())

    return sink


def build_chat_turn_sink(
    store: ChatTurnStore,
    *,
    clock: Callable[[], datetime] = _utc_now,
) -> Sink:
    """TK-295 (DEC-65e, ruling v2.159 r2): the ``SourceRegistry`` sink tap that writes the user's
    OWN chat/voice utterances into ``wombat_chat_turns`` — the dream extractor's ONLY input
    (never rendered into any prompt, never a conversation-history window, DEC-64's rejection
    stands). Covers BOTH the typed ``ChatSource`` and voice ``ASRSource`` payload shapes with one
    tap: any event whose payload carries ``item_kind == 'chat'`` is recorded; every other source
    id/payload shape is silently ignored (the SAME explicit-whitelist posture as
    ``build_external_item_sink``).

    Field projection, per payload shape (``sources.chat_source``/``chat.surface`` for typed chat,
    ``sources.asr`` for voice):
      - ``text``: ``payload['text']`` (typed chat) or ``payload['transcript']`` (voice) —
        whichever key is present.
      - ``voice``: ``True`` iff the payload carries a ``voice_turn`` key at all (voice ASR turns
        stamp it; typed chat never does).
      - ``captured_at``: ``payload['received_at']`` (typed chat) or ``payload['captured_at']``
        (voice) — whichever key is present, parsed via ``datetime.fromisoformat``; ``clock()`` if
        somehow neither key is present (defensive — never reached by either real payload shape).

    A ``store.record_turn`` raise is caught PER EVENT and logged as ONE WARNING naming the source
    id — the ledger can never block a turn (CON-3-adjacent additive posture): the event still
    enqueues via the registry's own separate enqueue arm, byte-unaffected by this tap.
    """

    def sink(source_id: str, events: list[SourceEvent]) -> None:
        for event in events:
            payload = event.payload
            if payload.get("item_kind") != "chat":
                continue
            text = payload.get("text") or payload.get("transcript")
            if not text:
                continue
            voice = "voice_turn" in payload
            captured_raw = payload.get("received_at") or payload.get("captured_at")
            captured_at = (
                datetime.fromisoformat(captured_raw) if captured_raw else clock()
            )
            try:
                store.record_turn(str(text), voice, captured_at)
            except Exception:
                logger.warning(
                    "source %s: ChatTurnStore.record_turn raised — this turn is dropped from "
                    "the ledger, the event still enqueues unaffected",
                    source_id,
                    exc_info=True,
                )

    return sink


def _compose_sinks(external_sink: Sink | None, chat_sink: Sink | None) -> Sink | None:
    """TK-295 (ruling v2.159 r2): ``SourceRegistry`` takes exactly ONE sink callable — this
    composes the (optional) TK-245 external-item sink WITH the (optional) TK-295 chat-turn sink
    into ONE callable, both run per poll batch. Either half absent degrades to the OTHER half
    exactly (byte-identical — the same function object, not a wrapper); both absent is ``None``
    (today's no-sink behavior exactly)."""
    if external_sink is None:
        return chat_sink
    if chat_sink is None:
        return external_sink

    def composed(source_id: str, events: list[SourceEvent]) -> None:
        external_sink(source_id, events)
        chat_sink(source_id, events)

    return composed


def _maybe_register_feedback(
    registry: SourceRegistry,
    config: WombatConfig,
    *,
    poll_interval_seconds: float,
) -> None:
    """TK-176: register the explicit-feedback source (``FeedbackInputSource``, TK-51) iff
    ``config.wombat_feedback_file`` is non-blank — the SAME loud-skip pattern as
    ``_maybe_register_gcal``/``_maybe_register_gmail`` above. A missing/blank path only disables
    the v1 file channel; the push channel (the future ASR TK-162 entry) is unaffected either way,
    so this never raises."""
    raw_path = (config.wombat_feedback_file or "").strip()
    if not raw_path:
        logger.warning(
            "feedback source not wired: WOMBAT_FEEDBACK_FILE not configured — skipping the "
            "feedback file channel (boot continues without it)"
        )
        return
    registry.register(
        FeedbackInputSource(
            poll_interval_seconds=poll_interval_seconds, feedback_file=Path(raw_path)
        )
    )


def make_persona_command_hook(
    live_persona: LivePersona, speak: Callable[[str], None] | None
) -> Callable[[str], bool]:
    """TK-212 (EP-34, DEC-35 + DEC-37(f), Q-109(c)): build ``ASRSource``'s pre-queue persona-
    command interception hook. On a match (``parse_persona_command`` — TK-211's closed grammar,
    exact-match only) the returned closure:

    (a) logs ONE human-readable ``logger.info`` trail line, BEFORE the apply, naming the matched
    phrase, axis, and old -> new level (this IS the CON-4 trail per Q-107(d) — the pg
    ``ActionTrail`` stays scoped to external-tier dispatches; this hook never touches
    ``trail/writer.py``);
    (b) calls ``live_persona.set(...)`` inside a guard — a raise there is caught, logged as ONE
    loud WARNING, and never propagated (the command is still consumed, never enqueued as
    garbage);
    (c) delivers a fixed, deterministic spoken ack via ``speak`` (zero model calls): either
    ``"<Axis> is now <level>."`` or, for a reset, ``"Persona reset to defaults."``. A ``None``
    ``speak``, or one that raises, degrades to ONE loud log line and never blocks — the persona
    change is already applied regardless.

    Returns ``True`` (consumed) for a match, ``False`` for anything else. The whole hook NEVER
    raises."""

    def hook(transcript: str) -> bool:
        command = parse_persona_command(transcript)
        if command is None:
            return False

        current = live_persona.matrix
        new_matrix = apply(current, command)
        if command.reset:
            logger.info("asr persona command matched %r: reset persona -> defaults", transcript)
            ack = "Persona reset to defaults."
        else:
            axis = command.axis
            assert axis is not None  # PersonaCommand.__post_init__ guarantees this for non-reset
            old_level = getattr(current, axis)
            new_level = getattr(new_matrix, axis)
            logger.info(
                "asr persona command matched %r: axis=%s %s -> %s",
                transcript,
                axis,
                old_level,
                new_level,
            )
            ack = f"{axis.capitalize()} is now {new_level}."

        try:
            live_persona.set(new_matrix)
        except Exception:
            logger.warning(
                "asr persona command hook: LivePersona.set raised applying %r — the command is "
                "still consumed (never enqueued)",
                transcript,
                exc_info=True,
            )

        if speak is not None:
            try:
                speak(ack)
            except Exception:
                logger.warning(
                    "asr persona command hook: speak raised delivering the ack %r — degrading "
                    "silently (the persona change is already applied)",
                    ack,
                    exc_info=True,
                )

        return True

    return hook


def make_persona_feedback_hook(
    recorder: Callable[[FeedbackToken, str, datetime], None],
    clock: Callable[[], datetime] = _utc_now,
) -> Callable[[str, str], None]:
    """TK-213 (EP-35, DEC-36/DEC-37(h)): build ``ASRSource``'s side-channel persona-feedback
    recording hook, mirroring ``make_persona_command_hook``'s shape. On a lexicon match
    (``detect_feedback_token`` — the closed, exact-match-only table) the returned closure calls
    ``recorder(token, event_key, clock())`` inside a guard: a raise there is caught, logged as
    ONE loud WARNING, and never propagated — the utterance still enqueues normally regardless
    (this hook never consumes). No match is a silent no-op. The whole hook NEVER raises."""

    def hook(transcript: str, event_key: str) -> None:
        token = detect_feedback_token(transcript)
        if token is None:
            return
        try:
            recorder(token, event_key, clock())
        except Exception:
            logger.warning(
                "asr persona feedback hook: recorder raised recording %r — the utterance still "
                "enqueues normally (this hook never consumes)",
                token.phrase,
                exc_info=True,
            )

    return hook


def _maybe_register_asr(
    registry: SourceRegistry,
    config: WombatConfig,
    *,
    poll_interval_seconds: float,
    live_persona: LivePersona | None = None,
    speak: Callable[[str], None] | None = None,
    persona_feedback_recorder: Callable[[FeedbackToken, str, datetime], None] | None = None,
    turn_hook: Callable[[str, str, str], None] | None = None,
    context_hook: Callable[[], Mapping[str, str]] | None = None,
) -> None:
    """TK-162 (Q-97), rerouted by TK-193: register the ASR drop-directory source (``ASRSource``)
    iff ``config.wombat_asr_drop_dir`` is non-blank AND a ``Transcriber`` is constructible — the
    SAME loud-skip pattern as ``_maybe_register_gcal``/``_maybe_register_gmail``/
    ``_maybe_register_feedback`` above, with two independent skip conditions. Neither missing
    piece ever raises: voice is additive (CON-3), so a checkout without the ``[voice]`` extra
    (or an absent/blocked cloud key/extra falling through to that same local gap), or one with no
    drop directory configured, still boots clean with every other source intact. Transcriber
    construction is delegated to ``voice.select.build_transcriber`` (TK-193), which already logs
    LOUD naming the exact gap on any skip path — nothing further to log here.

    TK-212: ``command_hook`` is ``make_persona_command_hook(live_persona, speak)`` ONLY when
    ``live_persona`` is not ``None``; otherwise ``None`` — a caller that doesn't wire a
    ``LivePersona`` gets today's ``ASRSource`` exactly, no interception at all.

    TK-213: ``feedback_hook`` is ``make_persona_feedback_hook(persona_feedback_recorder)`` ONLY
    when ``persona_feedback_recorder`` is not ``None``; otherwise ``None`` — a caller that doesn't
    wire a recorder gets today's ``ASRSource`` exactly, no feedback recording at all.

    TK-280 (DEC-60c server half): ``turn_hook`` passes straight through to ``ASRSource`` — the
    composition root (``wombat.bootstrap.assemble_runtime``) builds it (or leaves it ``None``)
    and this function does no branching of its own on it.

    TK-289 (DEC-64 gap A, half 2): ``context_hook`` passes straight through to ``ASRSource`` —
    the SAME pass-through shape as ``turn_hook`` above; this function does no branching of its
    own on it either."""
    raw_dir = (config.wombat_asr_drop_dir or "").strip()
    if not raw_dir:
        logger.warning(
            "asr source not wired: WOMBAT_ASR_DROP_DIR not configured — skipping the local "
            "voice drop-directory channel (boot continues without it)"
        )
        return
    transcriber = build_transcriber(config)
    if transcriber is None:
        return
    command_hook = (
        make_persona_command_hook(live_persona, speak) if live_persona is not None else None
    )
    feedback_hook = (
        make_persona_feedback_hook(persona_feedback_recorder)
        if persona_feedback_recorder is not None
        else None
    )
    registry.register(
        ASRSource(
            drop_dir=Path(raw_dir),
            transcriber=transcriber,
            poll_interval_seconds=poll_interval_seconds,
            command_hook=command_hook,
            feedback_hook=feedback_hook,
            turn_hook=turn_hook,
            context_hook=context_hook,
        )
    )


def build_source_registry(
    config: WombatConfig,
    queue: Enqueuer,
    *,
    tz: ZoneInfo,
    clock: Callable[[], datetime] = _utc_now,
    gcal_poll_interval_seconds: float = DEFAULT_GCAL_POLL_INTERVAL_SECONDS,
    gmail_poll_interval_seconds: float = DEFAULT_GMAIL_POLL_INTERVAL_SECONDS,
    feedback_poll_interval_seconds: float = DEFAULT_FEEDBACK_POLL_INTERVAL_SECONDS,
    asr_poll_interval_seconds: float = DEFAULT_ASR_POLL_INTERVAL_SECONDS,
    gcal_token_store: GcalTokenStore | None = None,
    gmail_token_store: GmailTokenStore | None = None,
    live_persona: LivePersona | None = None,
    speak: Callable[[str], None] | None = None,
    persona_feedback_recorder: Callable[[FeedbackToken, str, datetime], None] | None = None,
    external_item_store: ExternalItemStore | None = None,
    chat_turn_store: ChatTurnStore | None = None,
    turn_hook: Callable[[str, str, str], None] | None = None,
    context_hook: Callable[[], Mapping[str, str]] | None = None,
) -> SourceRegistry:
    """Assemble a ``SourceRegistry`` over ``queue`` (ASMP-2: enqueue-only) and register EACH
    of the gcal/gmail/feedback/asr sources INDEPENDENTLY when its own configuration is present
    (Q-61/Q-67 for gcal/gmail; TK-176 for feedback; TK-162/Q-97 for asr). Never raises for
    missing/absent config, tokens, or the optional [voice] extra — a loud log names what is
    missing and the source is skipped; the returned registry is always usable, with zero or
    more sources registered.

    ``tz``/``clock`` are injected (no config field is read internally here beyond the Google
    OAuth client id/secret, ``wombat_feedback_file``, and ``wombat_asr_drop_dir``/
    ``wombat_asr_model``) — callers supply the wombat civil-local tz (DEC-21) and, in tests, a
    fake clock. ``gcal_token_store``/``gmail_token_store`` default to the real OS-keyring
    ``TokenStore`` adapters; tests inject in-memory fakes so this function never touches the
    real vault outside the live smokes.

    ``live_persona``/``speak`` (TK-212) thread into ``_maybe_register_asr`` ONLY, to build the
    ASR pre-queue persona-command interception hook (``make_persona_command_hook``); both default
    ``None``, which constructs today's ``ASRSource`` exactly, no interception.

    ``persona_feedback_recorder`` (TK-213) also threads into ``_maybe_register_asr`` ONLY, to
    build the ASR side-channel persona-feedback recording hook (``make_persona_feedback_hook``);
    defaults ``None``, which constructs today's ``ASRSource`` exactly, no feedback recording.

    ``external_item_store`` (TK-245) builds the registry's optional store ``sink``
    (``build_external_item_sink``) ONLY when supplied; defaults ``None``, which constructs the
    ``SourceRegistry`` with no sink — today's poll behavior exactly (AC3).

    ``chat_turn_store`` (TK-295, DEC-65e) builds the registry's optional chat-turn ledger tap
    (``build_chat_turn_sink``) ONLY when supplied, COMPOSED with the ``external_item_store``
    sink above (``_compose_sinks``) into the ONE callable ``SourceRegistry`` accepts; defaults
    ``None``, which leaves ``external_item_store``'s sink (or the no-sink default) byte-
    unchanged.

    ``turn_hook`` (TK-280, DEC-60c) threads into ``_maybe_register_asr`` ONLY, straight through
    to ``ASRSource``; defaults ``None``, which constructs today's ``ASRSource`` exactly.

    ``context_hook`` (TK-289, DEC-64 gap A half 2) threads into ``_maybe_register_asr`` ONLY,
    straight through to ``ASRSource``; defaults ``None``, which constructs today's ``ASRSource``
    exactly.
    """
    # Built BEFORE the registry itself so the sink (which needs the SAME TriageRules instance,
    # loaded at most once) can be threaded into the SourceRegistry constructor (TK-245).
    gmail_source, gmail_rules = _build_gmail_source(
        config,
        clock=clock,
        poll_interval_seconds=gmail_poll_interval_seconds,
        token_store=gmail_token_store,
    )
    external_sink = (
        build_external_item_sink(external_item_store, gmail_rules=gmail_rules, clock=clock)
        if external_item_store is not None
        else None
    )
    chat_sink = (
        build_chat_turn_sink(chat_turn_store, clock=clock)
        if chat_turn_store is not None
        else None
    )
    sink = _compose_sinks(external_sink, chat_sink)
    registry = SourceRegistry(queue, sink=sink)
    _maybe_register_gcal(
        registry,
        config,
        tz=tz,
        clock=clock,
        poll_interval_seconds=gcal_poll_interval_seconds,
        token_store=gcal_token_store,
    )
    if gmail_source is not None:
        registry.register(gmail_source)
    _maybe_register_feedback(
        registry,
        config,
        poll_interval_seconds=feedback_poll_interval_seconds,
    )
    _maybe_register_asr(
        registry,
        config,
        poll_interval_seconds=asr_poll_interval_seconds,
        live_persona=live_persona,
        speak=speak,
        persona_feedback_recorder=persona_feedback_recorder,
        turn_hook=turn_hook,
        context_hook=context_hook,
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
    "DEFAULT_ASR_POLL_INTERVAL_SECONDS",
    "DEFAULT_FEEDBACK_POLL_INTERVAL_SECONDS",
    "DEFAULT_GCAL_POLL_INTERVAL_SECONDS",
    "DEFAULT_GMAIL_POLL_INTERVAL_SECONDS",
    "BriefFetches",
    "GmailWithReplyIntents",
    "build_brief_fetches",
    "build_external_item_sink",
    "build_source_registry",
    "make_persona_command_hook",
    "make_persona_feedback_hook",
]
