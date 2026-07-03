"""The inter-stage Artifact convention (TK-5/TK-6, Q-47/Q-48) — defined ONCE, spine-wide.

Stages in the drain pathway hand data downstream ONLY via the journaled ``StageResult.output``
Artifact — never a side channel. ``kind`` is a namespaced dotted string ``wombat.<snake_noun>``
(``wombat.drained_batch`` / ``wombat.drain_heartbeat`` / ``wombat.gate_decisions``);
``produced_by`` is the producing stage's canonical snake_case ``name`` verbatim, so a downstream
stage can pull ``ctx.last_output(produced_by)``.

``queue_items_to_artifact_data`` / ``queue_items_from_artifact_data`` are the ONLY serialization
path for a drained batch between stages — the gate stage deserializes a batch through the inverse
helper, never a hand-rolled dict-unpack, so read and write cannot drift (Q-41 wire-helper
principle). ``gate_decisions_to_artifact_data`` / ``gate_decisions_from_artifact_data`` are the
same convention applied to the gate's own output (Q-48): each entry carries the ORIGINAL Q-47
queue-item dict alongside the decision so TK-7 can ack holds without a second lookup.

GENERALIZATION (Q-55 rider, demo-harness assembly): ``GateDecision.items`` widened from an
always-exactly-one invariant (the TK-6 stub) to ZERO-OR-MANY once the production ``Gate``
(TK-27, ``gate/pipeline.py``) landed — its HOLD carries an EMPTY ``items`` tuple (the score was
discarded once the item entered the pending set) and its SURFACE_FLUSH carries the WHOLE flushed
pending set (possibly many). The wire key is therefore ``"scored_items"`` (a LIST, never a bare
``"scored_item"``) so both cardinalities round-trip losslessly; there is no per-entry "exactly
one" assertion any more.

``compose_request_to_artifact_data`` / ``compose_request_from_artifact_data`` (TK-8, Q-50) define
the ``ComposeStage`` input wire NOW so TK-10 (the not-yet-built ``compose_dispatch`` stage) can
produce it later with zero rework. The wire is deliberately narrow: ``item_id``, ``item_kind``
(as its ``.value``), and the user-facing ``payload`` dict ONLY — no scores, no ``GateAction``, no
queue internals may cross it. This is a structural enforcement of the "the model may only ever
see user-facing content" non_goal, not just prompt-construction discipline.
``composed_output_to_artifact_data`` / ``composed_output_from_artifact_data`` are the same
convention applied to ``ComposeStage``'s own terminal output. ``composed_output_to_artifact_data``
gains an ADDITIVE optional ``tokens_spent: int | None = None`` field (TK-9, Q-68) so a successful,
non-degraded compose call's token spend rides the artifact and lands in the journal via the wire
(no direct journal write). The PRIMARY inverse, ``composed_output_from_artifact_data``, keeps its
existing 4-tuple shape unchanged (TK-8 regression guard — its round-trip callers are untouched);
``composed_output_tokens_spent_from_artifact_data`` is the small additive accessor that reads the
new field back.

``surfaced_item_to_artifact_data`` / ``surfaced_item_from_artifact_data`` (TK-10, Q-51) define the
NEW single-item ``wombat.surfaced_item`` wire — ``review_or_speak`` (TK-7) single-izes a mixed
hold/surface ``wombat.gate_decisions`` batch down to ONE forwarded ``{action, scored_item,
queue_item}`` entry (the same shape as one ``GateDecisionEntry`` element, plus its action) and
will produce this wire later through these same helpers (the TK-8/TK-10 pre-definition pattern);
``ComposeDispatchRouter`` (TK-10) is the first consumer, reading it via
``ctx.last_output("review_or_speak")``. JSON-native + ``json.dumps`` round-trip per Q-49.

``hold_report_to_artifact_data`` / ``hold_report_from_artifact_data`` (TK-7, Q-52) define
``ReviewOrSpeakStage``'s OTHER terminal wire, ``wombat.hold_report`` — ``{"holds": [{"item_id",
"item_kind", "reason", "urgency", "load"}, ...]}``. Each hold dict is already plain-JSON-native
(the stage builds it directly from a ``ScoredItem`` + its stub reason string), so the helpers are a
thin, lossless identity-shaped pair — kept for the same reason every other wire has one: a single
named seam future callers can mock/round-trip against instead of hand-parsing ``Artifact.data``.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from wombat.gate.models import GateAction, GateDecision, ItemKind, ScoredItem
from wombat.queue import QueueItem

DRAINED_BATCH = "wombat.drained_batch"
DRAIN_HEARTBEAT = "wombat.drain_heartbeat"
GATE_DECISIONS = "wombat.gate_decisions"
COMPOSE_REQUEST = "wombat.compose_request"
COMPOSED_OUTPUT = "wombat.composed_output"
SURFACED_ITEM = "wombat.surfaced_item"
HOLD_REPORT = "wombat.hold_report"
# TK-98, EP-30-ish morning-brief cluster: BriefGatherStage's terminal wire kind (Q-74).
BRIEF_PAYLOAD = "wombat.brief_payload"
# TK-99: BriefForceFlushStage's terminal wire kind (Q-75) — a sealed BriefDecisionArtifact.
BRIEF_DECISION = "wombat.brief_decision"
# TK-100: BriefComposeStage's terminal wire kind (Q-77) — the rendered BriefText.
BRIEF_TEXT = "wombat.brief_text"
# TK-101: BriefDeliverStage's terminal wire kind (Q-78) — delivery record, the FINAL stage of the
# morning-brief cluster.
BRIEF_DELIVERED = "wombat.brief_delivered"

# One gate decision paired with the original queue item it was derived from (TK-7 acks holds off
# the carried queue_item dict, so the pairing travels together through the wire helpers).
GateDecisionEntry = tuple[GateDecision, QueueItem]


def queue_items_to_artifact_data(items: list[QueueItem]) -> dict[str, Any]:
    """Serialize drained ``QueueItem``s into an Artifact ``data`` payload: ``{"items": [...]}``."""
    return {"items": [asdict(item) for item in items]}


def queue_items_from_artifact_data(data: dict[str, Any]) -> list[QueueItem]:
    """The inverse of ``queue_items_to_artifact_data`` — the ONLY path back to ``QueueItem``s."""
    return [QueueItem(**raw) for raw in data["items"]]


def gate_decisions_to_artifact_data(entries: list[GateDecisionEntry]) -> dict[str, Any]:
    """Serialize gate decisions into an Artifact ``data`` payload (Q-48, widened Q-55).

    ``{"decisions": [{"action": <GateAction value str>, "scored_items": [<ScoredItem asdict>,
    ...], "queue_item": <the original Q-47 queue-item dict>}, ...]}`` — one entry per queue item,
    each carrying its original queue-item dict so TK-7 can ack holds. ``scored_items`` is a LIST
    (zero-or-many, Q-55): the production ``Gate``'s HOLD carries no items and its SURFACE_FLUSH
    can carry many; the TK-6 stub's always-exactly-one shape round-trips as a one-element list.
    """
    decisions: list[dict[str, Any]] = []
    for decision, queue_item in entries:
        scored_items: list[dict[str, Any]] = []
        for scored_item in decision.items:
            # Artifact.data must be plain-JSON-native (Q-49): enums go on the wire as their
            # .value, never as the enum MEMBER. asdict() leaves item_kind as the ItemKind member,
            # so overwrite it with .value — mirroring how "action" already stores GateAction.value.
            scored_dict = asdict(scored_item)
            scored_dict["item_kind"] = scored_item.item_kind.value
            scored_items.append(scored_dict)
        decisions.append(
            {
                "action": decision.action.value,
                "scored_items": scored_items,
                "queue_item": asdict(queue_item),
            }
        )
    return {"decisions": decisions}


def gate_decisions_from_artifact_data(data: dict[str, Any]) -> list[GateDecisionEntry]:
    """The inverse of ``gate_decisions_to_artifact_data`` — the ONLY path back (Q-48/Q-55)."""
    entries: list[GateDecisionEntry] = []
    for raw in data["decisions"]:
        scored_items = tuple(
            ScoredItem(
                item_id=scored_raw["item_id"],
                item_kind=ItemKind(scored_raw["item_kind"]),
                urgency=scored_raw["urgency"],
                load=scored_raw["load"],
            )
            for scored_raw in raw["scored_items"]
        )
        decision = GateDecision(action=GateAction(raw["action"]), items=scored_items)
        queue_item = QueueItem(**raw["queue_item"])
        entries.append((decision, queue_item))
    return entries


def compose_request_to_artifact_data(
    item_id: str, item_kind: ItemKind, payload: dict[str, Any]
) -> dict[str, Any]:
    """Serialize a single compose request into an Artifact ``data`` payload (TK-8, Q-50).

    ``{"item_id", "item_kind": <ItemKind .value string>, "payload"}`` — ONE surfaced item per
    compose invocation. NO scores, NO ``GateAction``, NO queue internals: the model may only ever
    see the fields that cross this wire.
    """
    return {"item_id": item_id, "item_kind": item_kind.value, "payload": payload}


def compose_request_from_artifact_data(
    data: dict[str, Any],
) -> tuple[str, ItemKind, dict[str, Any]]:
    """The inverse of ``compose_request_to_artifact_data`` — the ONLY path back (TK-8, Q-50)."""
    return data["item_id"], ItemKind(data["item_kind"]), data["payload"]


def composed_output_to_artifact_data(
    text: str,
    item_id: str,
    item_kind: ItemKind,
    degraded: bool,
    tokens_spent: int | None = None,
) -> dict[str, Any]:
    """Serialize ``ComposeStage``'s terminal output into an Artifact ``data`` payload (TK-8, Q-50).

    ``{"text", "item_id", "item_kind": <ItemKind .value string>, "degraded", "tokens_spent"}``.
    ``tokens_spent`` is ADDITIVE (TK-9, Q-68) — ``None`` for a degraded call (no successful,
    accounted model call happened) or for any caller that predates TK-9; a successful,
    non-degraded call passes ``response.usage.prompt_tokens + completion_tokens``.
    """
    return {
        "text": text,
        "item_id": item_id,
        "item_kind": item_kind.value,
        "degraded": degraded,
        "tokens_spent": tokens_spent,
    }


def composed_output_from_artifact_data(data: dict[str, Any]) -> tuple[str, str, ItemKind, bool]:
    """The inverse of ``composed_output_to_artifact_data`` — the ONLY path back (TK-8, Q-50).

    Kept to its original 4-tuple shape (text, item_id, item_kind, degraded) so every existing
    caller/round-trip keeps working unchanged (TK-8 regression guard); use
    ``composed_output_tokens_spent_from_artifact_data`` for the additive TK-9 field.
    """
    return data["text"], data["item_id"], ItemKind(data["item_kind"]), data["degraded"]


def composed_output_tokens_spent_from_artifact_data(data: dict[str, Any]) -> int | None:
    """Read the ADDITIVE optional ``tokens_spent`` field back off a composed-output wire (TK-9).

    Absent on data written before TK-9 (or by a degraded call) -> ``None``, never a KeyError.
    """
    tokens_spent = data.get("tokens_spent")
    return None if tokens_spent is None else int(tokens_spent)


def surfaced_item_to_artifact_data(
    action: GateAction, scored_item: ScoredItem, queue_item: QueueItem
) -> dict[str, Any]:
    """Serialize ONE surfaced gate entry into an Artifact ``data`` payload (TK-10, Q-51).

    ``{"action": <GateAction .value>, "scored_item": <ScoredItem asdict, item_kind as .value>,
    "queue_item": <QueueItem asdict>}`` — the single-item wire ``review_or_speak`` (TK-7) produces
    once it single-izes a gate-decisions batch; ``ComposeDispatchRouter`` (TK-10) is the first
    consumer. Mirrors the ``gate_decisions_to_artifact_data`` per-entry shape exactly.
    """
    scored_dict = asdict(scored_item)
    scored_dict["item_kind"] = scored_item.item_kind.value
    return {
        "action": action.value,
        "scored_item": scored_dict,
        "queue_item": asdict(queue_item),
    }


def surfaced_item_from_artifact_data(
    data: dict[str, Any],
) -> tuple[GateAction, ScoredItem, QueueItem]:
    """The inverse of ``surfaced_item_to_artifact_data`` — the ONLY path back (TK-10, Q-51)."""
    scored_raw = data["scored_item"]
    scored_item = ScoredItem(
        item_id=scored_raw["item_id"],
        item_kind=ItemKind(scored_raw["item_kind"]),
        urgency=scored_raw["urgency"],
        load=scored_raw["load"],
    )
    queue_item = QueueItem(**data["queue_item"])
    return GateAction(data["action"]), scored_item, queue_item


def hold_report_to_artifact_data(holds: list[dict[str, Any]]) -> dict[str, Any]:
    """Serialize a batch of hold records into an Artifact ``data`` payload (TK-7, Q-52).

    ``{"holds": [{"item_id", "item_kind", "reason", "urgency", "load"}, ...]}`` — each dict is
    ALREADY plain-JSON-native (built directly by ``ReviewOrSpeakStage`` from a ``ScoredItem`` +
    its stub reason string), so this is a thin identity wrapper kept for wire-helper symmetry
    with every other stage boundary (Q-47 principle: never hand-parse ``Artifact.data``).
    """
    return {"holds": holds}


def hold_report_from_artifact_data(data: dict[str, Any]) -> list[dict[str, Any]]:
    """The inverse of ``hold_report_to_artifact_data`` — the ONLY path back (TK-7, Q-52)."""
    return list(data["holds"])


def brief_text_to_artifact_data(text: str, degraded: bool, tokens_spent: int) -> dict[str, Any]:
    """Serialize ``BriefComposeStage``'s terminal output into an Artifact ``data`` payload
    (TK-100, Q-77).

    ``{"text", "degraded", "tokens_spent"}`` — ``tokens_spent`` is always an ``int`` (unlike
    ``ComposeStage``'s optional field): ``0`` on any degrade path (a fallback-without-a-call, or
    a call whose spend was never accounted), the real prompt+completion token count on a
    successful, non-degraded call.
    """
    return {"text": text, "degraded": degraded, "tokens_spent": tokens_spent}


def brief_text_from_artifact_data(data: dict[str, Any]) -> tuple[str, bool, int]:
    """The inverse of ``brief_text_to_artifact_data`` — the ONLY path back (TK-100, Q-77)."""
    return data["text"], data["degraded"], data["tokens_spent"]


def brief_delivered_to_artifact_data(
    delivered_at: str, voice_spoken: bool, replay: bool
) -> dict[str, Any]:
    """Serialize ``BriefDeliverStage``'s terminal output into an Artifact ``data`` payload
    (TK-101, Q-78).

    ``{"delivered_at", "voice_spoken", "replay"}`` — ``delivered_at`` is the tz-local (DEC-21
    canonical tz) ISO timestamp string embedded in the appended sink header; ``voice_spoken`` is
    ``True`` only when ``speak()`` was actually called and succeeded THIS run; ``replay`` is
    ``True`` when the run-id marker was already present in the sink (intra-run crash-replay,
    AC4) — the append/echo/speak were all skipped.
    """
    return {"delivered_at": delivered_at, "voice_spoken": voice_spoken, "replay": replay}


def brief_delivered_from_artifact_data(data: dict[str, Any]) -> tuple[str, bool, bool]:
    """The inverse of ``brief_delivered_to_artifact_data`` — the ONLY path back (TK-101, Q-78)."""
    return data["delivered_at"], data["voice_spoken"], data["replay"]


__all__ = [
    "BRIEF_DECISION",
    "BRIEF_DELIVERED",
    "BRIEF_PAYLOAD",
    "BRIEF_TEXT",
    "COMPOSED_OUTPUT",
    "COMPOSE_REQUEST",
    "DRAINED_BATCH",
    "DRAIN_HEARTBEAT",
    "GATE_DECISIONS",
    "HOLD_REPORT",
    "SURFACED_ITEM",
    "GateDecisionEntry",
    "brief_delivered_from_artifact_data",
    "brief_delivered_to_artifact_data",
    "brief_text_from_artifact_data",
    "brief_text_to_artifact_data",
    "compose_request_from_artifact_data",
    "compose_request_to_artifact_data",
    "composed_output_from_artifact_data",
    "composed_output_to_artifact_data",
    "composed_output_tokens_spent_from_artifact_data",
    "gate_decisions_from_artifact_data",
    "gate_decisions_to_artifact_data",
    "hold_report_from_artifact_data",
    "hold_report_to_artifact_data",
    "queue_items_from_artifact_data",
    "queue_items_to_artifact_data",
    "surfaced_item_from_artifact_data",
    "surfaced_item_to_artifact_data",
]
