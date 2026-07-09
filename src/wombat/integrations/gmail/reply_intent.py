"""wombat.integrations.gmail.reply_intent — ReplyIntentBuilder: sanitize a tainted
``GmailMessageItem`` + its ``TriageResult`` into a clean ``ReplyIntent`` (TK-80, EP-17, Q-91).

THE INGEST->OUTBOUND BRIDGE (Q-91 ruling, DEC-19): sanitization here is STRUCTURAL FIELD
EXCLUSION AND BOUNDING, never content filtering. ``ReplyIntent`` carries NO ``body_text`` field
at all — that omission IS the sanitization. There is no semantic injection detection anywhere in
this module; an adversarial body cannot make ``build`` emit anything beyond the allowlisted
fields below, no matter what it says (DEC-19).

PURE, NO I/O (Q-91 ruling): ``build`` is a deterministic function — no LLM call, no queue write,
no registry/gate/model access. It runs INSIDE the tainted ingest drive by design (the module
never touches ``cogworx``'s capability machinery at all, so there is nothing here for a taint
latch to protect or defeat) and hands its output to a FUTURE fresh, untainted outbound drive
(TK-78's ``DraftComposer``, TK-177's wire) — this ticket proves that crossing is safe via a real
``Registry``/``ToolGate`` in its own test, not by importing any of that machinery itself.

THE RECIPIENT SOURCE (the injection-defeating field): ``recipient`` is ALWAYS
``item.sender`` — a structured header ``GmailMessageItem`` carries directly, never parsed from
``body_text``. A body claiming a different send target (e.g. a fake "SYSTEM" directive naming an
attacker address) has no path to influence ``recipient`` — there is no code path here that reads
an address out of the body at all.

REPLY-WORTHY RULE (Q-91 ruling, the deterministic v1 rule): ``build`` returns a ``ReplyIntent``
iff ``triage.priority_band == PriorityBand.HIGH``; a ``NORMAL`` item returns ``None`` and no
artifact is produced. ``reply_kind`` carries ``triage.priority_band.value`` (always ``"high"``
today — the field exists so a future band expands the reply-worthy set without a wire-shape
change).

THE BODY BOUNDARY (Q-65 guard): this module is the SIXTH sanctioned reader of the guarded
``body_text`` payload key (``tests/integrations/gmail/test_body_key_guard.py``'s
``_SANCTIONED_PATHS``) — ``build`` reads ``item.body_text`` once, only to derive the bounded,
control-character-stripped ``quoted_excerpt``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from wombat.gate.models import ItemKind
from wombat.integrations.gmail.models import GmailMessageItem
from wombat.integrations.gmail.triage import PriorityBand, TriageResult

# The bound on quoted_excerpt (Q-91 ruling): a prefix, never the whole body, so a pathologically
# long or marker-laden body cannot ride the excerpt field unbounded.
EXCERPT_MAX_CHARS = 280


@dataclass(frozen=True, slots=True)
class ReplyIntent:
    """A structurally-sanitized reply intent (TK-80) — the ingest->outbound bridge artifact.

    Deliberately has NO ``body_text`` field: the allowlisted fields below are the ENTIRE surface
    a consumer ever sees, so nothing here can smuggle raw body content across the taint boundary.
    ``recipient`` is ``item.sender`` verbatim (never parsed from the body). ``quoted_excerpt`` is
    a bounded, control-character-stripped prefix of the body, not the body itself.
    """

    recipient: str
    subject_or_thread_ref: str
    reply_kind: str
    quoted_excerpt: str
    message_id: str
    matched_rules: tuple[str, ...]

    def to_payload(self) -> dict[str, Any]:
        """JSON-native wire form (Q-49), mirroring ``GmailMessageItem``/``TriageResult``.

        Additionally carries ``item_kind`` (``ItemKind.DRAFT.value``, the TK-21 canonical
        vocabulary) — a derived field, not part of the dataclass itself, so it round-trips as an
        extra key that ``from_payload`` ignores.
        """
        return {
            "recipient": self.recipient,
            "subject_or_thread_ref": self.subject_or_thread_ref,
            "reply_kind": self.reply_kind,
            "quoted_excerpt": self.quoted_excerpt,
            "message_id": self.message_id,
            "matched_rules": list(self.matched_rules),
            "item_kind": ItemKind.DRAFT.value,
        }

    @staticmethod
    def from_payload(d: dict[str, Any]) -> ReplyIntent:
        """Inverse of ``to_payload`` — exact round-trip: ``from_payload(r.to_payload()) == r``.

        ``item_kind`` is derived on write and not stored on the dataclass, so it is ignored here.
        """
        return ReplyIntent(
            recipient=d["recipient"],
            subject_or_thread_ref=d["subject_or_thread_ref"],
            reply_kind=d["reply_kind"],
            quoted_excerpt=d["quoted_excerpt"],
            message_id=d["message_id"],
            matched_rules=tuple(d["matched_rules"]),
        )


def _quoted_excerpt_of(body_text: str) -> str:
    """Strip control characters (``str.isprintable``-style filter), then bound to
    ``EXCERPT_MAX_CHARS`` characters. An empty body yields an empty excerpt."""
    printable = "".join(ch for ch in body_text if ch.isprintable())
    return printable[:EXCERPT_MAX_CHARS]


def build(item: GmailMessageItem, triage: TriageResult) -> ReplyIntent | None:
    """Build a ``ReplyIntent`` from ``item`` + its ``triage`` result, or ``None`` if the item is
    not reply-worthy.

    Reply-worthy iff ``triage.priority_band == PriorityBand.HIGH`` (the deterministic v1 rule) —
    a ``NORMAL`` item returns ``None``. ``recipient`` is ``item.sender`` ONLY (never parsed from
    ``item.body_text``). ``message_id`` is carried through unchanged for TK-177's idempotency-key
    derivation (``idempotency_key("gmail.reply_intent", f"reply:{message_id}")`` — NOT computed
    in this module).
    """
    if triage.priority_band != PriorityBand.HIGH:
        return None

    return ReplyIntent(
        recipient=item.sender,
        subject_or_thread_ref=item.subject,
        reply_kind=triage.priority_band.value,
        quoted_excerpt=_quoted_excerpt_of(item.body_text),
        message_id=item.message_id,
        matched_rules=triage.matched_rules,
    )


__all__ = ["EXCERPT_MAX_CHARS", "ReplyIntent", "build"]
