"""BriefGatherStage — first stage of the morning-brief cluster (TK-98, Q-74).

Reads the calendar and Gmail RAISING fetch seams (``CalendarPoller.fetch_window`` /
``GmailPoller.fetch_recent``, this same ticket) exactly once each, triages Gmail metadata
(``wombat.integrations.gmail.triage.triage_message``), and packs the result into ONE JSON-native
``BriefPayload`` (``wombat.domain.brief_payload``) — no LLM, no filtering, no conflict logic
(downstream's job, TK-99+).

Each source read is independently guarded (Q-74 AC2, broad ``except Exception`` — a token expiry
surfaces as google-auth's ``RefreshError``, not a ``requests`` error, so broad is the correct
posture here): a raising fetch degrades that ONE source to an empty tuple + its own
``*_unavailable=True`` flag, logged as a WARNING naming the source. Triage runs INSIDE the gmail
guarded block, so a triage failure also degrades gmail cleanly. Both sources failing still
returns a flagged payload — this stage NEVER raises on a single- or both-source failure.

The stage does ZERO time arithmetic of its own (the pollers own all time math — their fetch
callables are bound to their window at composition); items are stored frozen/verbatim, no
dedup/mutation.

``run(ctx)`` touches ONLY ``ctx.clock()`` (Provenance timestamp only, mirroring ``GateStage``'s
Q-48 pattern) — it NEVER touches ``ctx.journal`` (the engine journals the returned Artifact by
(run_id, step_index)) and never touches ``ctx.last_output`` (this is the first stage of the
cluster, no upstream wire to pull).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime

from cogworx.claims.provenance import Artifact, Provenance
from cogworx.loop.result import StageResult, Transition
from cogworx.loop.stage import StageContext

from wombat.calendar.models import CalendarEvent
from wombat.domain.brief_payload import BriefPayload, GmailBriefItem
from wombat.integrations.gmail.models import GmailMessageItem
from wombat.integrations.gmail.triage import TriageRules, triage_message
from wombat.stages.artifacts import BRIEF_PAYLOAD

logger = logging.getLogger(__name__)


class BriefGatherStage:
    """Collects one calendar slice + one Gmail slice into a structured ``BriefPayload``."""

    name: str = "brief_gather"
    # Declare-ahead (TK-6 -> TK-7 precedent): TK-99's stage takes this name; it is not built
    # yet, so this is a forward string reference only.
    transitions: tuple[str, ...] = ("brief_force_flush",)

    def __init__(
        self,
        *,
        fetch_calendar: Callable[[], list[CalendarEvent]],
        fetch_gmail: Callable[[], list[GmailMessageItem]],
        triage_rules: TriageRules,
        clock: Callable[[], datetime],
    ) -> None:
        self._fetch_calendar = fetch_calendar
        self._fetch_gmail = fetch_gmail
        self._triage_rules = triage_rules
        self._clock = clock

    async def run(self, ctx: StageContext) -> StageResult:
        calendar_events: tuple[CalendarEvent, ...] = ()
        calendar_unavailable = False
        try:
            calendar_events = tuple(self._fetch_calendar())
        except Exception:
            logger.warning(
                "brief_gather: calendar source unavailable; degrading to an empty slice",
                exc_info=True,
            )
            calendar_unavailable = True

        gmail_items: tuple[GmailBriefItem, ...] = ()
        gmail_unavailable = False
        try:
            raw_messages = self._fetch_gmail()
            triaged: list[GmailBriefItem] = []
            for message in raw_messages:
                result = triage_message(message, self._triage_rules)
                triaged.append(
                    GmailBriefItem(
                        message_id=message.message_id,
                        subject=message.subject,
                        sender=message.sender,
                        received_at=message.received_at,
                        urgency_score=result.urgency_score,
                        priority_band=result.priority_band,
                        matched_rules=result.matched_rules,
                    )
                )
            gmail_items = tuple(triaged)
        except Exception:
            logger.warning(
                "brief_gather: gmail source unavailable; degrading to an empty slice",
                exc_info=True,
            )
            gmail_unavailable = True

        payload = BriefPayload(
            generated_at=self._clock(),
            calendar_events=calendar_events,
            gmail_items=gmail_items,
            calendar_unavailable=calendar_unavailable,
            gmail_unavailable=gmail_unavailable,
        )

        return Transition(
            to="brief_force_flush",
            output=Artifact(
                kind=BRIEF_PAYLOAD,
                produced_by=self.name,
                provenance=Provenance(source="system", confidence=1.0, recorded_at=ctx.clock()),
                data=payload.to_payload(),
            ),
        )


__all__ = ["BriefGatherStage"]
