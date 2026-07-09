"""wombat.integrations.gmail.task_extractor — GmailTaskExtractor: deterministic, off-path task
extraction from a ``GmailMessageItem`` body (TK-77, EP-17, Q-84).

THE CRUX (Q-84 ruling 1, the last Q-56 epic-dep over-pull discharged): ``extract_tasks`` is a
PURE, deterministic function — regex + keyword heuristics only, NO LLM anywhere in this path (a
build-time structural guard proves it, see ``tests/integrations/gmail/test_task_extractor.py``'s
no-import scan). It owns no drive and does no I/O. Its invocation site belongs to a FUTURE
dream-pathway consumer (TK-47's successors) — exactly as ``triage_message``'s invocation site
belongs to TK-98 (Q-66 precedent). This ticket ships the pure function + wire type only; no
enqueueing, no drive wiring.

THE TAINT LATCH (Q-84 ruling 2): the structural taint latch (TK-148, DEC-19) is a property of the
DRIVE that reads an email body through the tagged ``read_email_body`` capability — NOT a property
of this pure function, which never touches ``cogworx``'s gate/registry/taint machinery at all. The
sanctioned access path (``wombat.safety.taint.register_read_email_body`` dispatched through a real
``ToolGate``) is what taints; this module adds no second body-access path, so there is nothing here
for a latch to protect. ``tests/integrations/gmail/test_task_extractor.py`` proves this by
dispatching the REAL capability through a REAL ``Registry``/``ToolGate`` and only THEN running
``extract_tasks`` on the fetched body — content-independent (DEC-19), run twice (benign +
injection-shaped bodies).

THE BODY BOUNDARY (Q-84 ruling 4): this module is the FIFTH sanctioned reader of the guarded
``body_text`` payload key (``tests/integrations/gmail/test_body_key_guard.py``'s
``_SANCTIONED_PATHS`` — poller.py, models.py, safety/taint.py, stages/ingest_email_body.py, and
now this file). ``extract_tasks`` reads ``item.body_text`` directly — that reference is the whole
reason this ticket exists on the allowlist.

IDENTITY RIDER (the standing v0.60 arc note, TK-12): a ``TaskItem``'s identity derives via
``wombat.domain.item_identity.derive_task_natural_id(parent_source_natural_id, task_local_id)``,
with the parent id being the originating message's ``message_id`` (matching
``GmailPoller.id == "gmail"``'s natural-id convention) and ``task_local_id`` the REPLAY-STABLE
ordinal of the match in body order (``"task-0"``, ``"task-1"``, ...) — re-extracting the same body
yields byte-identical ``TaskItem`` ids. ``ItemKind`` stays GENERIC (a dedicated TASK kind is a
TK-41 vocabulary-version-bump concern, TK-72 precedent — out of scope here).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from wombat.domain.item_identity import ItemRef, derive_task_natural_id
from wombat.integrations.gmail.models import GmailMessageItem

# The source_id GmailPoller registers under (GmailPoller.id == "gmail", poller.py). A TaskItem
# is still a gmail-sourced item — only its natural id differs from its parent message's (the
# derive_task_natural_id link) — so it carries the SAME source_id.
_GMAIL_SOURCE_ID = "gmail"

# Bound on TaskItem.description (the matched actionable sentence) so a pathologically long "run
# of text with no sentence terminators" body cannot produce an unbounded field.
_MAX_DESCRIPTION_LENGTH = 500

# Sentence-boundary split: newline, or a '.'/'!'/'?' followed by whitespace (or end of string).
# Deterministic, no NLP dependency.
_SENTENCE_SPLIT_RE = re.compile(r"[\r\n]+|(?<=[.!?])\s+")

# The closed set of action-verb/request patterns (Q-84 briefing, mirrors triage.py's
# module-constant rule-table convention). A sentence is "actionable" iff it matches ANY of
# these, case-insensitive. Kept as a single module constant — no code path outside this tuple
# decides actionability.
_ACTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bplease\b", re.IGNORECASE),
    re.compile(r"\breview\b", re.IGNORECASE),
    re.compile(r"\bsend\b", re.IGNORECASE),
    re.compile(r"\bsubmit\b", re.IGNORECASE),
    re.compile(r"\bdue\b", re.IGNORECASE),
    re.compile(r"\bdeadline\b", re.IGNORECASE),
    re.compile(
        r"\bby\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday"
        r"|\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?)\b",
        re.IGNORECASE,
    ),
)

# Deterministic deadline-signal extraction: weekday names, an explicit "due"/"deadline" keyword,
# or a bare numeric date pattern. Keyword/date-PATTERN matching only — no date arithmetic (Q-84
# briefing: "no date arithmetic required"). Order matters: the first pattern to match in the
# sentence wins, tried in this fixed order for determinism.
_DEADLINE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", re.IGNORECASE
    ),
    re.compile(r"\b(deadline)\b", re.IGNORECASE),
    re.compile(r"\b(due)\b", re.IGNORECASE),
    re.compile(r"\b(\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?)\b"),
)


@dataclass(frozen=True, slots=True)
class TaskItem:
    """One task extracted from a Gmail message body (TK-77).

    ``ref`` carries the extracted-task identity (distinct from, but linkable to, its parent
    message's ``ItemRef`` — the identity rider, TK-12). ``description`` is the matched
    actionable sentence, bounded to ``_MAX_DESCRIPTION_LENGTH`` characters. ``deadline_signal``
    is the matched deadline token (e.g. ``"friday"``) or ``None`` if the sentence carried no
    recognizable deadline signal. ``source_message_id`` is the originating message's raw
    ``message_id``, carried alongside ``ref`` for convenience (``ref`` already encodes it, but
    callers should not have to parse it back out via ``parent_natural_id_of_task``).
    """

    ref: ItemRef
    description: str
    deadline_signal: str | None
    source_message_id: str

    def to_payload(self) -> dict[str, Any]:
        """JSON-native wire form (Q-49), mirroring ``GmailMessageItem``/``TriageResult``."""
        return {
            "ref": {
                "source_id": self.ref.source_id,
                "source_natural_id": self.ref.source_natural_id,
            },
            "description": self.description,
            "deadline_signal": self.deadline_signal,
            "source_message_id": self.source_message_id,
        }

    @staticmethod
    def from_payload(d: dict[str, Any]) -> TaskItem:
        """Inverse of ``to_payload`` — exact round-trip: ``from_payload(t.to_payload()) == t``."""
        ref_payload = d["ref"]
        return TaskItem(
            ref=ItemRef(
                source_id=ref_payload["source_id"],
                source_natural_id=ref_payload["source_natural_id"],
            ),
            description=d["description"],
            deadline_signal=d["deadline_signal"],
            source_message_id=d["source_message_id"],
        )


def _split_sentences(body_text: str) -> list[str]:
    """Deterministic, replay-stable sentence split (module docstring). Empty/whitespace-only
    chunks are dropped; order is preserved."""
    return [chunk.strip() for chunk in _SENTENCE_SPLIT_RE.split(body_text) if chunk.strip()]


def _is_actionable(sentence: str) -> bool:
    return any(pattern.search(sentence) for pattern in _ACTION_PATTERNS)


def _deadline_signal_of(sentence: str) -> str | None:
    for pattern in _DEADLINE_PATTERNS:
        match = pattern.search(sentence)
        if match:
            return match.group(1).lower()
    return None


def extract_tasks(item: GmailMessageItem) -> list[TaskItem]:
    """Extract zero or more ``TaskItem`` records from ``item.body_text`` — pure, deterministic,
    regex/keyword heuristics only, NO LLM call anywhere in this function (Q-84 ruling 1).

    Splits the body into sentences (deterministic, order-preserving), keeps the ones matching
    the closed ``_ACTION_PATTERNS`` rule table, and for each builds a ``TaskItem`` whose identity
    is ``derive_task_natural_id(item.message_id, f"task-{ordinal}")`` — the ordinal is the
    REPLAY-STABLE position of the match in body order, so re-extracting the same body yields
    byte-identical ids (dedup-safe). A non-actionable body returns ``[]``.
    """
    tasks: list[TaskItem] = []
    ordinal = 0
    for sentence in _split_sentences(item.body_text):
        if not _is_actionable(sentence):
            continue
        task_local_id = f"task-{ordinal}"
        ordinal += 1
        ref = ItemRef(
            source_id=_GMAIL_SOURCE_ID,
            source_natural_id=derive_task_natural_id(item.message_id, task_local_id),
        )
        tasks.append(
            TaskItem(
                ref=ref,
                description=sentence[:_MAX_DESCRIPTION_LENGTH],
                deadline_signal=_deadline_signal_of(sentence),
                source_message_id=item.message_id,
            )
        )
    return tasks


__all__ = ["TaskItem", "extract_tasks"]
