"""WriteWindowSummariesStage — the nightly ``dream_window`` stage (TK-112, EP-21, Q-99e).

Keyword-injected collaborators only (mirrors ``DreamBehaviorLogStage``/``DreamTuneStage``
precedent, ``pathways/dream_pathway.py``): ``store`` is ``wombat.behavior.event_log.
BehaviorEventLog`` (the SAME shared instance bootstrap constructs — this stage never constructs
one); ``writer`` is ``wombat.user_model.observation_writer.ObservationWriter`` (EP-11's ONE write
seam into the user scope); ``tz`` is the SAME configured ``ZoneInfo`` bootstrap threads everywhere
(DEC-21).

READ (Q-99e): ``store.events_between(now - 14 days, now)`` — a fixed 14-day trailing window over
the already-written ``wombat_behavior_events`` rows.

DETECT: the pure, off-path ``wombat.behavior.window_detector.detect_productivity_windows``
(TK-112, Q-99c). No motive, no model.

WRITE (Q-99c/d): when the detector returns at least one ``WindowSummary``, ONE ``Claim`` is
written via ``writer.record`` — ``subject=f"productivity_window:{wombat_today(ctx.clock(), tz).
isoformat()}"`` (``wombat_today``, DEC-21), ``predicate=ClaimPredicate.PRODUCTIVITY_WINDOW``,
``value=json.dumps(...)`` of the JSON-native summary list (Q-49 convention, via ``window_summary_
to_dict``), ``event_id=None``, ``observed_at=ctx.clock()``. An EMPTY detector result never writes
a claim (skip-on-empty — an honest "no windows tonight" rather than a claim with an empty list).

NEVER touches ``ctx.journal`` and makes NO model call (mirrors ``DreamBehaviorLogStage``'s own
off-path posture). A detector or write failure is caught, logged LOUD, and the stage STILL
transitions onward to ``dream_pattern`` (TK-113, Q-99e — this stage's downstream neighbor since the
pattern-detection pass was inserted between the window-detect pass and the terminal; mirrors
``DreamTuneStage``/``DreamBehaviorLogStage``'s own never-block-the-terminal posture) — one bad
night's window pass must never block the reachable terminal.

NO DASHBOARD/SURFACE (NG-3): this stage only reads, detects, and writes a claim; it has no
render/surface/dashboard call anywhere (enforced by ``tests/behavior/stages/
test_write_window_summaries.py``'s structural scan).

TK-213 (EP-35, DEC-36/DEC-37(h)): rows with ``event_type == 'persona_feedback'`` (the bootstrap
persona-feedback recorder's writes, ``wombat.bootstrap``) are EXCLUDED before
``detect_productivity_windows`` ever sees the corpus — a persona-feedback utterance is not a
behavioral productivity event, and mixing it in would distort ``switch_rate``/window metrics
that mean something else entirely (writer-owns-honesty).
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from zoneinfo import ZoneInfo

from cogworx.claims.provenance import Artifact, Provenance
from cogworx.loop.result import StageResult, Transition
from cogworx.loop.stage import StageContext

from wombat.behavior.event_log import BehaviorEventLog
from wombat.behavior.window_detector import detect_productivity_windows, window_summary_to_dict
from wombat.domain.daily_ledger import wombat_today
from wombat.user_model.claims import Claim, ClaimPredicate
from wombat.user_model.observation_writer import ObservationWriter

logger = logging.getLogger(__name__)

# WriteWindowSummariesStage's committed output kind (TK-112) — a contentless, system-provenance
# count artifact mirroring dream_pathway.py's own DREAM_*_REPORT_KIND idiom: no WindowSummary
# payloads ride this artifact, only counts — the durable record is the productivity_window claim
# the stage wrote (or didn't, on a windowless night).
DREAM_WINDOW_REPORT_KIND = "wombat.dream_window_report"

# The trailing read window (Q-99e): a fixed 14-day lookback over wombat_behavior_events — not a
# tunable, a module constant.
_LOOKBACK_DAYS = 14


class WriteWindowSummariesStage:
    """The nightly productivity-window detect + persist pass (TK-112, EP-21, Q-99e).

    ``name`` is ``dream_window``, inserted BETWEEN ``dream_behavior_log`` and ``dream_pattern``
    (TK-113) in the ``wombat.dream`` graph (``build_dream_pathway``, ``pathways/dream_pathway.
    py``).
    """

    name: str = "dream_window"
    transitions: tuple[str, ...] = ("dream_pattern",)

    def __init__(self, *, store: BehaviorEventLog, writer: ObservationWriter, tz: ZoneInfo) -> None:
        self._store = store
        self._writer = writer
        self._tz = tz

    async def run(self, ctx: StageContext) -> StageResult:
        now = ctx.clock()

        window_count = 0
        errors = 0
        try:
            events = self._store.events_between(now - timedelta(days=_LOOKBACK_DAYS), now)
            # TK-213: persona-feedback rows are not behavioral productivity events — excluded
            # before the detector ever sees the corpus (writer-owns-honesty).
            events = [event for event in events if event.event_type != "persona_feedback"]
            summaries = detect_productivity_windows(events)
            window_count = len(summaries)
            if summaries:
                subject = f"productivity_window:{wombat_today(now, self._tz).isoformat()}"
                await self._writer.record(
                    Claim(
                        predicate=ClaimPredicate.PRODUCTIVITY_WINDOW,
                        subject=subject,
                        value=json.dumps([window_summary_to_dict(s) for s in summaries]),
                        event_id=None,
                        observed_at=now,
                    )
                )
        except Exception:
            logger.error(
                "dream_window: detector or write failed; tonight's window pass is skipped",
                exc_info=True,
            )
            window_count = 0
            errors = 1

        return Transition(
            to="dream_pattern",
            output=Artifact(
                kind=DREAM_WINDOW_REPORT_KIND,
                produced_by=self.name,
                provenance=Provenance(source="system", confidence=1.0, recorded_at=now),
                data={"windows": window_count, "errors": errors},
            ),
        )


__all__ = ["DREAM_WINDOW_REPORT_KIND", "WriteWindowSummariesStage"]
