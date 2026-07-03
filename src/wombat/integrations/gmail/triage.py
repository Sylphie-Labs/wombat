"""wombat.integrations.gmail.triage — GmailTriageEngine: deterministic, off-path priority
scoring for a ``GmailMessageItem`` (TK-76, EP-17, Q-66).

THE CRUX (Q-66 ruling 2, METADATA-ONLY): ``triage_message`` reads ``subject``, ``sender``, and
``received_at`` ONLY — it must NEVER access the raw message body field on ``GmailMessageItem``.
Body-deep signals are TK-77's territory (extraction, under the TK-148 taint latch). This module
is deliberately NOT on the Q-65 body-key guard's allowlist
(``tests/integrations/gmail/test_body_key_guard.py``): any reference to that guarded field name
added here fails that guard's build-time scan, so the metadata-only rule is enforced
structurally, not just by convention.

THE PURE FUNCTION: ``triage_message(item, rules)`` is a pure, deterministic function — no I/O,
no clock, no LLM call, no scheduling. Rules are INJECTED as a ``TriageRules`` value (loaded by
``load_triage_rules`` at composition time, e.g. by TK-98's brief-gather); the function itself
never loads a file.

OFF THE HOT PATH (Q-66 ruling 4, the S1 guarantee): nothing wires ``triage_message`` onto the
drain spine at this ticket — there is no scheduling machinery yet (dropped from this slice,
Q-66 ruling 1). The guarantee is enforced structurally by
``tests/integrations/gmail/test_triage.py``'s no-import guard, which asserts no drain-spine
module imports this module.

WIRE SHAPE (Q-66 ruling 5, Q-49 JSON-native): ``TriageResult`` is a frozen dataclass with
JSON-native ``to_payload``/``from_payload`` helpers, mirroring
``wombat.calendar.models.CalendarEvent``. ``matched_rules`` carries rule NAMES ONLY — never
message content or subject text — so a payload can never leak a fragment of the triaged
message.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from importlib import resources
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from wombat.config import ConfigurationError
from wombat.integrations.gmail.models import GmailMessageItem

# Bump in lock-step with triage_rules.yaml's ``version`` whenever the rule schema changes, so a
# persisted rules file can be reconciled against this loader's expectations.
TRIAGE_RULES_VERSION = 1

_RULES_FILENAME = "triage_rules.yaml"


class PriorityBand(StrEnum):
    """Closed enum — EXACTLY two bands (Q-66 ruling 5). No other value is valid."""

    HIGH = "high"
    NORMAL = "normal"


# --------------------------------------------------------------------------------------- rules


class SenderAllowlistRule(BaseModel):
    """A named rule: any message whose ``sender`` contains one of ``senders`` (case-insensitive
    substring match) contributes ``urgency_score``/``priority_band``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    senders: tuple[str, ...]
    urgency_score: float = Field(ge=0.0, le=1.0)
    priority_band: PriorityBand


class SubjectKeywordRule(BaseModel):
    """A named rule: any message whose ``subject`` contains one of ``keywords``
    (case-insensitive substring match) contributes ``urgency_score``/``priority_band``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    keywords: tuple[str, ...]
    urgency_score: float = Field(ge=0.0, le=1.0)
    priority_band: PriorityBand


class TriageRules(BaseModel):
    """Typed, frozen view of the versioned ``triage_rules.yaml`` rule set (Q-66 ruling 3).

    Its own data artifact — NOT a ``wombat.params.OperatingParams`` tunable. Every field is
    validated; an unversioned or malformed file fails loud via ``load_triage_rules`` rather than
    falling back to a silent default.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: int
    sender_allowlist_rules: tuple[SenderAllowlistRule, ...] = ()
    subject_keyword_rules: tuple[SubjectKeywordRule, ...] = ()


def _default_rules_path() -> Path:
    """Resolve the packaged ``triage_rules.yaml`` (works editable and from a wheel)."""
    return Path(str(resources.files("wombat.integrations.gmail").joinpath(_RULES_FILENAME)))


def load_triage_rules(path: Path | None = None) -> TriageRules:
    """Load + validate the triage rule set from the versioned YAML, or fail LOUD.

    Reads the packaged ``triage_rules.yaml`` unless an explicit ``path`` is given. A missing
    file, non-mapping content, invalid YAML, or a missing/mistyped/unversioned field raises
    ``ConfigurationError`` — never a silent default (Q-66 ruling 3).
    """
    src = path or _default_rules_path()
    try:
        text = src.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigurationError(f"triage rules file not readable: {src}") from exc

    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigurationError(f"triage rules file {src} is not valid YAML: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigurationError(
            f"triage rules file {src} must contain a YAML mapping, got {type(raw).__name__}"
        )

    try:
        return TriageRules.model_validate(raw)
    except ValidationError as exc:
        raise ConfigurationError(f"invalid triage rules file {src}: {exc}") from exc


# --------------------------------------------------------------------------------------- result


@dataclass(frozen=True, slots=True)
class TriageResult:
    """The outcome of triaging one ``GmailMessageItem`` (Q-66 ruling 5).

    ``matched_rules`` carries rule NAMES ONLY — never message content or subject text — so this
    result can be logged, journaled, or wired onward without ever re-exposing what the message
    said.
    """

    message_id: str
    urgency_score: float
    priority_band: PriorityBand
    matched_rules: tuple[str, ...]

    def __post_init__(self) -> None:
        if not (0.0 <= self.urgency_score <= 1.0):
            raise ValueError(
                f"TriageResult {self.message_id!r}: urgency_score must be in [0.0, 1.0], "
                f"got {self.urgency_score!r}"
            )

    def to_payload(self) -> dict[str, Any]:
        """JSON-native wire form (Q-49): ``priority_band`` as its string value. The one shape
        ``from_payload`` round-trips exactly."""
        return {
            "message_id": self.message_id,
            "urgency_score": self.urgency_score,
            "priority_band": self.priority_band.value,
            "matched_rules": list(self.matched_rules),
        }

    @staticmethod
    def from_payload(d: dict[str, Any]) -> TriageResult:
        """Inverse of ``to_payload`` — exact round-trip: ``from_payload(r.to_payload()) == r``."""
        return TriageResult(
            message_id=d["message_id"],
            urgency_score=d["urgency_score"],
            priority_band=PriorityBand(d["priority_band"]),
            matched_rules=tuple(d["matched_rules"]),
        )


# --------------------------------------------------------------------------------------- engine


def triage_message(item: GmailMessageItem, rules: TriageRules) -> TriageResult:
    """Score ``item`` against the injected ``rules`` — pure, deterministic, metadata-only.

    Reads ONLY ``item.subject``, ``item.sender``, and ``item.received_at``
    (``received_at`` is accepted for future recency-weighted rules but is not yet consumed by
    any rule kind). NEVER reads the raw message body field on ``item`` (Q-66 ruling 2). Matching
    a sender-allowlist rule checks ``item.sender`` (case-insensitive substring); matching a
    subject-keyword rule checks ``item.subject`` (case-insensitive substring). ``urgency_score``
    is the max score across every matched rule (0.0 — no match); ``priority_band`` is HIGH iff
    any matched rule is HIGH; ``matched_rules`` lists every matched rule's NAME, in rule-set
    order.

    ``item.received_at`` is part of the allowed metadata surface (Q-66 ruling 2) but no rule
    kind in this ticket's minimal schema consumes it yet — a future recency-weighted rule kind
    may.
    """
    matched_names: list[str] = []
    best_score = 0.0
    band = PriorityBand.NORMAL

    sender_lower = item.sender.lower()
    for sender_rule in rules.sender_allowlist_rules:
        if any(candidate.lower() in sender_lower for candidate in sender_rule.senders):
            matched_names.append(sender_rule.name)
            best_score = max(best_score, sender_rule.urgency_score)
            if sender_rule.priority_band is PriorityBand.HIGH:
                band = PriorityBand.HIGH

    subject_lower = item.subject.lower()
    for keyword_rule in rules.subject_keyword_rules:
        if any(keyword.lower() in subject_lower for keyword in keyword_rule.keywords):
            matched_names.append(keyword_rule.name)
            best_score = max(best_score, keyword_rule.urgency_score)
            if keyword_rule.priority_band is PriorityBand.HIGH:
                band = PriorityBand.HIGH

    return TriageResult(
        message_id=item.message_id,
        urgency_score=best_score,
        priority_band=band,
        matched_rules=tuple(matched_names),
    )


__all__ = [
    "PriorityBand",
    "SenderAllowlistRule",
    "SubjectKeywordRule",
    "TriageResult",
    "TriageRules",
    "load_triage_rules",
    "triage_message",
]
