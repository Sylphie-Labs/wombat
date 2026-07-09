"""wombat.integrations.gmail.draft_composer — DraftComposer: phrase a reply in a fresh,
untainted drive; journal the proposal BEFORE any Gmail call; create the draft; return
``AwaitHuman`` (TK-78, EP-18, Q-92).

STANDALONE STAGE, a ``compose.py``-parity sibling — NOT a ``ProposeDispatchStage`` (TK-149)
subclass (Q-92 ruling): ``ProposeDispatchStage.run()`` is non-overridable and journals the
proposal AFTER ``build_proposal`` returns, with no pre-park dispatch hook. This stage MUST
dispatch ``gmail.drafts.create`` itself, between the journal write and the park — subclassing
the TK-149 base would invert that ordering. It REUSES ``ProposalWriter`` (the trail seam) and
the ``action_id = f"{run_id}:{name}"`` convention from :mod:`wombat.stages.dispatch_base`.

THE TAINT-ORDER PROOF (Q-92, the AC1 ruling): ``gate.taint.tainted`` is ``False`` at the moment
this stage BEGINS dispatching — that is the ISS-3 fresh-drive premise this ticket proves. The
``gmail.drafts.create`` dispatch itself is what latches taint (it is untagged external-tier, per
``cogworx.capability.policy.TaintState.update`` — external + no ``trusted-output`` tag always
taints), and that latch fires structurally BEFORE ``cap.invoke`` inside ``dispatch_one`` (steps
①-③, ``capability/router.py``). By the time this stage returns ``AwaitHuman``, the drive IS
tainted — but harmlessly: ``record_proposal`` already landed durably, the ONE ``drafts.create``
call already happened, and any FURTHER external dispatch on this drive is refused by the very
latch this call just set (proven by AC1(e)'s post-condition probe). "Fresh, untainted drive" is
therefore a PRE-dispatch property, not a post-dispatch one — the park is what makes the resulting
taint harmless, not an absence of taint.

REPLY-BODY SANITIZATION BOUNDARY (Q-91/TK-80): the ONLY upstream data this stage ever reads is a
``ReplyIntent`` (via the shared ``wombat.compose_request`` wire, the same seam ``ComposeStage``
reads — ``ctx.last_output(upstream_stage_name)`` then ``compose_request_from_artifact_data``).
``ReplyIntent`` structurally has NO raw-email-body field (TK-80) — there is no field on this
stage's input a raw email body could ride in on, adversarial or not.

``make_drafts_create_capability`` builds the ``gmail.drafts.create`` capability (tier="external",
NO TAGS — Q-91/Q-92 ruled the ``trusted-output`` exemption unearned here; the taint latch firing
on this call is the accepted consequence, not a defect). Its live HTTP path is exercised only by
TK-177's operator smoke; every test in this module dispatches a fake capability instead.
``gmail.messages.send`` is NEVER registered anywhere in this codebase — a structural never-send
guarantee (CON-5/DEC-19/NG-5): there is no send capability for any drive to ever reach for.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from collections.abc import Callable
from datetime import datetime
from email.message import EmailMessage
from typing import Any, Protocol

import requests
from cogworx.capability.base import Capability
from cogworx.capability.policy import StageToolPolicy, TierViolation
from cogworx.capability.registry import function_capability
from cogworx.claims.provenance import Artifact, Provenance
from cogworx.loop.result import AwaitHuman, StageResult
from cogworx.loop.stage import StageContext
from cogworx.model.base import ChatMessage

from wombat.integrations.gmail.reply_intent import ReplyIntent
from wombat.safety.tier_policy import bind_external_tier
from wombat.stages.artifacts import compose_request_from_artifact_data
from wombat.stages.dispatch_base import ProposalWriter
from wombat.trail.schema import ActionType

logger = logging.getLogger(__name__)

# This stage's own committed output kind — carries the created draft's details on the AwaitHuman
# Artifact so TK-79's real draft_dispatch stage can pull them back via ctx.last_output(self.name).
DRAFT_PROPOSAL = "wombat.draft_proposal"

# The ONE capability this stage ever dispatches (Q-91/Q-92) — never gmail.messages.send.
DRAFT_CREATE_CAPABILITY = "gmail.drafts.create"

# AC-fixed (mirrors compose.py's Q-50 fixed default) — not a TK-13 tunable.
_DEFAULT_TIMEOUT_SECONDS = 2.0

# A fixed, terse steward instruction (mirrors compose.py's _SYSTEM_INSTRUCTION) — no prompt
# iteration (v1, no ticket asked for one). The prompt is built ONLY from ReplyIntent fields
# (Q-91: no KB hints in v1).
_SYSTEM_INSTRUCTION = (
    "You are a quiet steward drafting a reply on the user's behalf. Phrase one terse, calm "
    "reply body responding to the quoted excerpt. No preamble, no signature."
)

_DRAFTS_CREATE_URL = "https://gmail.googleapis.com/gmail/v1/users/me/drafts"
# A conservative fixed request timeout (mirrors poller.py's _REQUEST_TIMEOUT_S) — not a TK-13
# tunable, just a guard against an authorized session hanging forever on a dead connection.
_REQUEST_TIMEOUT_S = 30.0


class DraftTrailWriter(ProposalWriter, Protocol):
    """``ProposalWriter`` (the reused TK-149 trail seam) plus ``record_refusal`` — the exact
    ``ActionTrailWriter`` surface this stage needs (mirrors ``dispatch_approved.py``'s
    ``ApprovalTrailWriter`` / ``safety/taint.py``'s ``RefusalWriter``: each caller declares only
    the subset of methods it actually calls). Tests inject a recording fake; production injects
    the real ``ActionTrailWriter``, which already satisfies both method shapes."""

    def record_refusal(
        self,
        *,
        action_id: str,
        human_summary: str,
        target: str,
        proposed_at: datetime,
    ) -> object: ...


class _GmailDraftSession(Protocol):
    """The ONE HTTP method ``make_drafts_create_capability`` needs (mirrors ``GmailPoller``'s
    read-only ``_GmailSession``, inverted to the ONE write method this capability performs)."""

    def post(self, url: str, *, json: dict[str, Any], timeout: float) -> requests.Response: ...


def _raw_message(to: str, subject: str, body: str) -> str:
    """Build a base64url-encoded RFC 5322 message for the Gmail v1 ``drafts.create`` body."""
    msg = EmailMessage()
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    return base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")


def make_drafts_create_capability(session: _GmailDraftSession) -> Capability:
    """Build the ``gmail.drafts.create`` capability (tier="external", NO TAGS).

    Posts the draft to the Gmail API via the injected ``session`` seam — the ONE place this
    capability performs I/O. The live path is exercised only by TK-177's operator smoke; every
    test in this module dispatches a fake capability instead of this one.
    """

    async def _drafts_create(to: str, subject: str, body: str) -> str:
        response = session.post(
            _DRAFTS_CREATE_URL,
            json={"message": {"raw": _raw_message(to, subject, body)}},
            timeout=_REQUEST_TIMEOUT_S,
        )
        response.raise_for_status()
        data: dict[str, Any] = response.json()
        return str(data["id"])

    return function_capability(_drafts_create, name=DRAFT_CREATE_CAPABILITY, tier="external")


def _template_body(reply_intent: ReplyIntent) -> str:
    """The terse, deterministic mouth-down degrade body (S8) — built ONLY from ``ReplyIntent``
    fields, no model call, pure and deterministic (mirrors compose.py's TemplateComposer
    discipline)."""
    return (
        f"Thanks for your message about {reply_intent.subject_or_thread_ref}. "
        "I'll follow up shortly."
    )


class DraftComposer:
    """Phrases ONE reply via the mouth, journals the proposal, creates the Gmail draft, and
    parks ``AwaitHuman`` (TK-78). See module docstring for the taint-order proof (Q-92)."""

    name: str = "draft_composer"
    transitions: tuple[str, ...] = ("draft_dispatch",)
    # Bound by bind_external_tier in __init__ (TK-151/DEC-22 — the ONE sanctioned admission call
    # site); declared here so mypy strict knows the attribute exists without a getattr/ignore.
    tool_policy: StageToolPolicy

    def __init__(
        self,
        *,
        writer: DraftTrailWriter,
        clock: Callable[[], datetime],
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        upstream_stage_name: str = "compose_dispatch",
    ) -> None:
        self._writer = writer
        self._clock = clock
        self._timeout_seconds = timeout_seconds
        self._upstream_stage_name = upstream_stage_name
        # The ONE sanctioned admission call site (TK-151/DEC-22) — mirrors dispatch_approved.py's
        # pattern; scoped to this stage instance only, the engine rebinds the gate fresh before
        # every stage, so this never leaks.
        bind_external_tier(self)

    async def run(self, ctx: StageContext) -> StageResult:
        art = await ctx.last_output(self._upstream_stage_name)
        if art is None:
            msg = f"{self.name}: no {self._upstream_stage_name!r} output available yet"
            raise RuntimeError(msg)
        _item_id, _item_kind, payload = compose_request_from_artifact_data(art.data)
        reply_intent = ReplyIntent.from_payload(payload)

        messages = [
            ChatMessage(role="system", content=_SYSTEM_INSTRUCTION),
            ChatMessage(
                role="user",
                content=(
                    f"recipient: {reply_intent.recipient}\n"
                    f"subject: {reply_intent.subject_or_thread_ref}\n"
                    f"reply_kind: {reply_intent.reply_kind}\n"
                    f"quoted_excerpt: {reply_intent.quoted_excerpt}"
                ),
            ),
        ]

        degraded = False
        body: str | None = None
        try:
            response = await asyncio.wait_for(
                ctx.model.complete(messages=messages), timeout=self._timeout_seconds
            )
            body = response.text
        except asyncio.CancelledError:
            # Never swallow cancellation — only the mouth's own failures degrade (S8 parity).
            raise
        except Exception:
            # Timeout, provider/connection/HTTP-5xx errors: all degrade to the template, never
            # break this stage's own flow.
            logger.warning(
                "draft_composer: mouth call failed; degrading to a terse template body",
                exc_info=True,
            )
            degraded = True

        if not degraded and (body is None or not body.strip()):
            degraded = True

        if degraded:
            body = _template_body(reply_intent)

        assert body is not None  # either the model's text or the template's render, always a str

        recipient = reply_intent.recipient
        subject = f"Re: {reply_intent.subject_or_thread_ref}"
        human_summary = f"Draft a reply to {recipient} — {subject}: {body}"

        now = self._clock()
        action_id = f"{ctx.run_id}:{self.name}"

        # JOURNAL BEFORE ANY GMAIL CALL — a kill between this write and the dispatch below still
        # leaves the proposal row behind.
        self._writer.record_proposal(
            action_id=action_id,
            action_type=ActionType.DRAFT_EMAIL,
            human_summary=human_summary,
            target=recipient,
            proposed_at=now,
        )

        try:
            await ctx.dispatch(
                DRAFT_CREATE_CAPABILITY,
                {"to": recipient, "subject": subject, "body": body},
            )
        except TierViolation:
            self._writer.record_refusal(
                action_id=action_id,
                human_summary=human_summary,
                target=DRAFT_CREATE_CAPABILITY,
                proposed_at=now,
            )
            raise

        return AwaitHuman(
            question=human_summary,
            to="draft_dispatch",
            output=Artifact(
                kind=DRAFT_PROPOSAL,
                produced_by=self.name,
                provenance=Provenance(source="system", confidence=1.0, recorded_at=now),
                data={
                    "action_id": action_id,
                    "recipient": recipient,
                    "message_id": reply_intent.message_id,
                    "subject": subject,
                    "body": body,
                    "degraded": degraded,
                },
            ),
        )


__all__ = [
    "DRAFT_CREATE_CAPABILITY",
    "DRAFT_PROPOSAL",
    "DraftComposer",
    "DraftTrailWriter",
    "make_drafts_create_capability",
]
