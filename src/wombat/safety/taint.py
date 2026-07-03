"""The structural taint-latch call site — wombat's wiring onto cog-worx's tier/taint machinery
(TK-148, the gmail-branch SAFETY KEYSTONE, S10/DEC-19/Q-64).

The "lethal trifecta" defense is STRUCTURAL, not content-based (DEC-19): once a drive has read
untrusted content (an email body), external-tier capabilities (e.g. sending email) become
STRUCTURALLY unavailable for the rest of that drive. cog-worx OWNS the taint machinery
(``cogworx.capability.policy.TaintState`` / ``ToolGate`` / ``cogworx.capability.router
.dispatch_one``) — this module adds NO new taint logic. It does exactly two things:

  1. Register the email-body read AS a tagged, gate-dispatched capability (``read_email_body``,
     tier="read", tags=("untrusted-source",)) so the read crosses ``ToolGate`` and structurally
     latches ``TaintState`` (Q-64 ruling #1). A plain Python string read would never call the
     gate and would silently NOT taint (CF-3.2-B) — that is exactly the untagged-read violation
     AC4 tests for. Tagging untrusted sources is the INTEGRATOR's obligation (wombat's, here);
     cog-worx never taints an untagged read-tier capability on its own.
  2. Catch ``TierViolation`` at the wombat invocation seam and record a ``blocked_by_taint``
     trail row via TK-146's AS-BUILT ``ActionTrailWriter.record_refusal`` (Q-64 ruling #3).

FROZEN CONTRACT for TK-75 (not yet built, recorded here so it is not lost): the enqueued Gmail
payload carries ``message_id`` with the body reachable ONLY through the injected
``body_provider`` behind ``read_email_body``. ALL drive-side email-body access MUST go through
this capability — any other path is the AC4 violation.

Non-goals (DEC-19): no content-filter / injection-detection logic — the latch is structural, it
never inspects body content. No modification to cog-worx's taint machinery — this module is call
sites only (a registration helper + an exception-catching wrapper). No web call site (TK-153 —
the P3 boundary is held here deliberately: this module defines exactly one capability-name
constant, ``READ_EMAIL_BODY_CAPABILITY``).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from typing import Any, Protocol

from cogworx.capability.base import Capability
from cogworx.capability.policy import TierViolation, ToolGate
from cogworx.capability.registry import Registry, function_capability
from cogworx.capability.router import dispatch_one

# The EXACT literal tag strings cog-worx's TaintState reads (Q-11, both directions). These are
# real framework literals (capability/policy.py) — spelled out here verbatim, never re-derived.
UNTRUSTED_SOURCE_TAG = "untrusted-source"
TRUSTED_OUTPUT_TAG = "trusted-output"

# The single capability name this ticket's email call site registers and dispatches. TK-153
# boundary (AC2): TK-148 wires ONLY the email call site — no web/browser capability name is
# defined in this module. The web call site is TK-153's, reusing this machinery + the
# both-direction tag convention, not absorbed here.
READ_EMAIL_BODY_CAPABILITY = "read_email_body"

BodyProvider = Callable[[str], Awaitable[str]]
"""Injected seam: ``message_id -> body_text``. Production wires the enqueued payload / body
store; tests wire fixture bodies, including adversarial injection payloads (the outcome must be
content-independent — that is the whole point of a STRUCTURAL latch)."""


def make_read_email_body_capability(body_provider: BodyProvider) -> Capability:
    """Build the ``read_email_body`` capability (tier="read") backed by an injected provider.

    The capability itself carries no tags — tags are assigned at REGISTRATION time
    (``Registry.register(..., tags=...)``, code-assigned, the model cannot inject or remove
    them). Use :func:`register_read_email_body` to register it with the required
    ``"untrusted-source"`` tag in one step.
    """

    async def _read_email_body(message_id: str) -> str:
        return await body_provider(message_id)

    return function_capability(
        _read_email_body,
        name=READ_EMAIL_BODY_CAPABILITY,
        tier="read",
    )


def register_read_email_body(registry: Registry, body_provider: BodyProvider) -> None:
    """Register the tagged ``read_email_body`` capability on ``registry`` (Q-64 ruling #1).

    This is HOW the processing drive accesses an email body. Registering it with the literal
    ``"untrusted-source"`` tag is what makes the read structurally latch ``TaintState`` on
    dispatch (``TaintState.update`` — cog-worx machinery, unmodified). Any drive-side body access
    NOT routed through this capability never crosses the gate and never taints — the exact
    integrator-obligation gap AC4's tests hold open.
    """
    registry.register(
        make_read_email_body_capability(body_provider),
        tags=(UNTRUSTED_SOURCE_TAG,),
    )


class RefusalWriter(Protocol):
    """The one method the refusal call site needs from TK-146's AS-BUILT ``ActionTrailWriter``.

    A structural Protocol (not an ABC) so unit tests can inject a recording fake without
    subclassing the real writer; the one DSN-gated integration test injects the real
    ``ActionTrailWriter``.
    """

    def record_refusal(
        self,
        *,
        action_id: str,
        human_summary: str,
        target: str,
        proposed_at: datetime,
    ) -> Any: ...


async def dispatch_or_refuse(
    gate: ToolGate,
    registry: Registry,
    name: str,
    args: Mapping[str, Any],
    *,
    writer: RefusalWriter,
    subject_item_idempotency_key: str,
    clock: Callable[[], datetime],
    approved: bool = False,
) -> Any:
    """The wombat invocation seam (Q-64 ruling #3): dispatch through the gate; on a structural
    ``TierViolation`` refusal, write a ``blocked_by_taint`` trail row via TK-146's
    ``record_refusal`` before re-raising.

    Delegates ALL security logic to ``cogworx.capability.router.dispatch_one`` (the single
    chokepoint, S10) — this function adds no tier/taint logic of its own, only the audit
    side-effect on refusal. ``TierViolation`` is re-raised after the trail write so the
    refusal stays LOUD (S10 — this is a structural access-denial, not a degrade-and-continue
    signal); callers still see the exception, this seam only adds the durable audit trail.

    ``action_id`` is stable across a Sweeper re-drive AND queue redelivery
    (``f"refusal:{subject_item_idempotency_key}:{name}"``) so ``record_refusal``'s
    ``ON CONFLICT (action_id) DO NOTHING`` absorbs replays and the row stays traceable to the
    exact item + capability that was refused. The timestamp is CALLER-SUPPLIED via ``clock()``
    (production: ``ctx.clock()``) — this module never reads a wall clock.
    """
    try:
        return await dispatch_one(gate, registry, name, dict(args), approved=approved)
    except TierViolation:
        action_id = f"refusal:{subject_item_idempotency_key}:{name}"
        writer.record_refusal(
            action_id=action_id,
            human_summary=f"blocked by taint latch: capability {name!r} refused (tier violation)",
            target=name,
            proposed_at=clock(),
        )
        raise


__all__ = [
    "READ_EMAIL_BODY_CAPABILITY",
    "TRUSTED_OUTPUT_TAG",
    "UNTRUSTED_SOURCE_TAG",
    "BodyProvider",
    "RefusalWriter",
    "dispatch_or_refuse",
    "make_read_email_body_capability",
    "register_read_email_body",
]
