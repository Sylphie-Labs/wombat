"""wombat composition root — assemble ONE cog-worx Engine with wombat's config (TK-1).

Pure wiring (DEC-12, never forks the Engine): it passes the four required substrate seams from
an injected ``SubstrateBundle`` (TK-14) plus wombat's six optional seams to ``Engine(...)``. It
does NOT call the model — the DeepSeek profile is registered as a ``ModelSpec`` DESCRIPTOR; the
client is built per-drive at run-start, so composition stays model-silent.

TK-9 (Q-68) layer 1: ``build_engine`` wires a REAL ``BudgetPolicy`` from OperatingParams'
``mouth_max_usd_per_drive``/``mouth_max_calls_per_drive`` (previously the unbounded
``BudgetPolicy()`` default) — cog-worx mints a fresh per-drive ``BudgetGuard`` from it
(``engine.py``); zero further wiring needed, per-DRIVE-SEGMENT ceilings (CF-3.0-B cumulative-
per-run stays deferred in cog-worx).

``build_compose_stage`` is TK-9 layer 2's factory: the daily ``DailySpendLedger``/ceiling need a
Postgres ``dsn`` that the cog-worx ``Engine`` itself has no seam for, so it is assembled
separately from ``build_engine`` (which never gained a ``dsn`` concept) rather than forking the
Engine's construction (DEC-12).

TK-53 (Q-71) layer 3: ``assemble_runtime`` composes the ONE standing process — it REGISTERS the
TK-7 drain pathway and wires the REAL production gate (TK-27) over the TK-29 durable Postgres
``PendingJournal`` (RISK-5's boot obligation), reusing ``build_engine``/``build_compose_stage``/
``build_source_registry`` rather than hand-rolling (closing the ``scripts/demo_drain.py``
Q-69 bypass gap). ``wombat.runtime.serve()`` only starts/drives/stops what this returns — it
registers nothing (the ticket's own non_goal).

TK-96: ``assemble_runtime`` ALSO registers the ``wombat.brief`` pathway — the four already-built
brief stages, wired via ``build_brief_pathway`` off the SAME composed ``Gate``/substrate/
``dsn`` the drain pathway uses (never a second gate, never a second Postgres). Registration is
CONDITIONAL on ``config.wombat_brief_path`` being non-blank (mirrors ``build_brief_deliver_stage``'s
own fail-loud-at-construction posture, but at the composition-root level so a Google-less/
sink-less boot still starts): blank/absent -> a loud warning and the pathway is simply not
registered (``RuntimeBundle.brief_pathway_id`` stays ``None``); TK-97's timer/fence wires against
that field once it exists, never a hardcoded pathway id.

TK-46 (Q-85): ``assemble_runtime`` ALSO registers the ``wombat.dream`` pathway, UNCONDITIONALLY
(unlike ``wombat.brief``): no external deps beyond the shared entity KG this composition already
builds, so a dream run is harmless even on a Google-less/sink-less boot. Additive
``RuntimeBundle.dream_pathway_id`` mirrors ``brief_pathway_id``'s field shape; TK-52's nightly
trigger/fence wires against that field once it exists, never a hardcoded pathway id.

TK-175 (Q-90 split, EP-12): ``DreamOutcomeStage`` (``build_dream_pathway``'s ``outcome`` arg) —
the nightly collect/infer/label pass — is constructed over the SAME shared ``entity_kg``/
``outcome_labeler`` instances TK-176 also threads into the drain-side wiring (both built once,
below); it transitions onward to ``DreamScaffoldStage`` (still the reachable terminal, TK-46's own
isolation proofs unaffected).

TK-47 (EP-13, DEC-12/DEC-23): the dream graph's entry is now ``DreamConsolidationStage``
(``build_dream_pathway``'s ``consolidate`` arg) — the nightly consolidation sweep, upstream of
``DreamOutcomeStage``. Composed via TK-54's ``build_dream_substrate`` over the SAME shared
``entity_kg`` instance and the SAME deepseek ``ModelSpec`` descriptor ``_deepseek_registry``
registers for the drain-side profile (``_deepseek_spec``, factored out so both call sites build
the identical descriptor) — ``CoherenceReconciler`` and ``ClaimExtractor`` are then constructed
directly over ``DreamSubstrate``'s ``store``/``oracle``/``model``/``source_registry`` (the SAME
cold-boot substrate bundle's ``journal`` backs the extractor, never a second journal). Registration
stays UNCONDITIONAL (Q-85) — no external deps beyond what this composition already builds.

TK-52 (Q-85): ``assemble_runtime`` ALSO registers ``wombat.dream_schedule`` — the once-nightly
dream timer, mirroring TK-97's ``wombat.brief_schedule`` wiring VERBATIM: built AFTER
``build_engine`` (the ``fire_dream`` closure captures the live ``Engine``), night-keyed
``run_id``, the SAME shared ``DailyLedger`` instance (a distinct ``"dream:run"`` row). UNLIKE
``wombat.brief_schedule``, registration is UNCONDITIONAL (mirrors ``wombat.dream``'s own
unconditional registration above — the dream scaffold needs no external config). Additive
``RuntimeBundle.dream_schedule_pathway_id`` is therefore never ``None``.

TK-176 (Q-90 split of TK-175, EP-12): the drain-side outcome-loop wiring, over ONE shared
user-scope entity KG instead of TK-53's original throwaway ``InMemoryEntityKG()``. ``entity_kg``/
``scope_registry``/``observation_writer`` are constructed ONCE here and threaded into BOTH
``UserModel`` (the read seam TK-42's ``Gate`` scores through) and ``OutcomeLabeler`` (TK-45's
write seam) — additive ``RuntimeBundle.entity_kg``/``RuntimeBundle.observation_writer`` fields
expose the SAME instances (AC4). Two closures compose ``GateStage``'s new TK-176 seams: ``_absorb_
feedback`` parses the TK-51 ``FeedbackSignal`` wire, records ONE ``BEHAVIOR_OBSERVED`` claim, then
acks the item off the SAME ``WombatQueue.ack`` call ``ReviewOrSpeakStage`` uses (AC1); ``_stamp_
resolution`` stamps an ``OUTCOME_PENDING`` claim per gate-decided item (AC2) — Q-22 BINDS: never a
terminal ``OUTCOME_*`` on this hot path. V1 honesty (Q-36/TK-14): the in-memory KG resets per
process — no persistence is added here.

TK-49 (Q-91, EP-14): the dream graph's new ``dream_tune`` stage — ``DreamTuneStage``
(``build_dream_pathway``'s ``tune`` arg) — composes ``wombat.rating.rating_tuner.RatingTuner``
over the SAME shared ``entity_kg``/``observation_writer`` instances TK-176 built above, plus the
SAME loaded ``OperatingParams`` (the LOCKED TK-48 bound block lives at ``op.rating_tuner``); it
transitions onward to ``dream_behavior_log`` (Q-98 superseded Q-91's end-state; TK-111 below). No
gate/pipeline change here — the gate re-reads a tuned parameter on its next drive via the as-built
``UserModel.ratings_for`` seam.

TK-111 (Q-98, EP-21): the dream graph's new ``dream_behavior_log`` stage — ``DreamBehaviorLogStage``
(``build_dream_pathway``'s new ``behavior_log`` arg), inserted between ``dream_tune`` (later
``dream_persona``, TK-214) and the window-detect pass below — composes a ``BehaviorEventLog`` over
the SAME runtime ``dsn`` every
other Postgres-touching seam here uses, and the SAME shared ``entity_kg``/``_RUNTIME_USER_ID``
TK-176 built above (never a second KG instance). Exposed on ``RuntimeBundle.behavior_event_log``
so ``runtime.py``'s teardown can close it (the SAME TK-184 lifecycle pattern as ``action_trail_
writer``/``daily_ledger``/``pending_journal``/``queue``).

TK-112 (Q-99e, EP-21): the dream graph's new ``dream_window`` stage —
``wombat.behavior.stages.write_window_summaries.WriteWindowSummariesStage``
(``build_dream_pathway``'s new ``window`` arg), inserted between ``dream_behavior_log`` and the
reachable terminal — composes over the SAME shared ``behavior_event_log``/``observation_writer``
instances built above (never a second instance of either) and the SAME configured ``tz``. No new
``RuntimeBundle`` field: unlike ``behavior_event_log`` this stage owns no closeable resource of
its own.

TK-213 (EP-35, DEC-36/DEC-37(h), Q-112(a)): ``assemble_runtime`` builds a persona-feedback
``recorder`` closure over the SAME ``behavior_event_log`` instance above — NEVER a second
``BehaviorEventLog``/connection — and threads it into ``build_source_registry`` as
``persona_feedback_recorder``. This is the SECOND sanctioned writer into
``wombat_behavior_events`` (the first is the nightly ``DreamBehaviorLogStage``): the Q-112(a)
row encoding is ``event_type='persona_feedback'``, ``source_id='asr'``, ``outcome_label=`` the
matched lexicon phrase VERBATIM, ``duration_seconds=None``, and
``idempotency_key=domain.item_identity.idempotency_key('persona_feedback', <the dropped audio
file's sha256 event_key>)`` — a re-drop of identical audio bytes upserts the SAME row, while
distinct recordings of the same phrase stay distinct rows.

TK-214 (EP-35, DEC-36/DEC-37(h), Q-112 pre-ruled — CLOSES EP-35): the dream graph's new
``dream_persona`` stage — ``DreamPersonaStage`` (``build_dream_pathway``'s new ``persona`` arg),
inserted between ``dream_tune`` and ``dream_facts`` (TK-297's stage, its new downstream neighbor,
superseding ``dream_behavior_log`` as the immediate next stage) — composes over the SAME shared
``behavior_event_log``/``live_persona`` instances built above (never a second
``BehaviorEventLog``/``LivePersona``). No new ``RuntimeBundle`` field: like ``dream_window_stage``
this stage owns no closeable resource of its own.

TK-297 (EP-13, DEC-65g): the dream graph's new ``dream_facts`` stage — ``DreamFactsStage``
(``build_dream_pathway``'s new ``facts`` arg), inserted between ``dream_persona`` and
``dream_derive`` (TK-299's stage, its new downstream neighbor, superseding ``dream_behavior_log``
as the immediate next stage) — composes over the SAME budget-guarded ``dream_substrate.model``
every other dream-consolidation call site uses (DEC-23, never a second model/guard) and the
``user_facts_store``/``chat_turn_store`` instances (TK-294/TK-295), HOISTED above the dream-stage
block for this reason — both are Q-46 fully-lazy, zero I/O at construction, so the hoist is
behavior-neutral. No new ``RuntimeBundle`` field: like ``dream_window_stage`` this stage owns no
closeable resource of its own (``user_facts_store``/``chat_turn_store`` are exposed separately,
unchanged).

TK-299 (EP-37, DEC-66): the dream graph's new ``dream_derive`` stage — ``DreamDeriveStage``
(``build_dream_pathway``'s new ``derive`` arg), inserted between ``dream_facts`` and
``dream_behavior_log`` — PURE CODE, no model call: composes over the SAME ``external_item_store``
(TK-245) and ``user_facts_store`` (TK-294) instances built above (never a second connection to
either table). No new ``RuntimeBundle`` field: like ``dream_facts_stage`` this stage owns no
closeable resource of its own.
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from cogworx.capability.registry import Registry
from cogworx.coherence.reconciler import CoherenceReconciler
from cogworx.context.personality import PersonalityProfile
from cogworx.context.rules import RuleSet
from cogworx.coordination.events import Event, EventType
from cogworx.cost.budget import BudgetPolicy
from cogworx.knowledge.scopes import ScopeRegistry
from cogworx.loop.pathway import PathwayRegistry
from cogworx.model.base import ModelCapabilities
from cogworx.model.providers.config import ProviderConfig
from cogworx.model.registry import ModelRegistry, ModelSpec
from cogworx.recall.stack import RecallStack
from cogworx.runtime.claim_extractor import ClaimExtractor
from cogworx.runtime.engine import Engine
from cogworx.substrate.entity_kg import EntityKG
from cogworx.substrate.journal import Journal, RunState
from cogworx.testing.doubles import InMemoryEntityKG

from .behavior.event_log import BehaviorEventLog
from .behavior.stages.dream_derive import DreamDeriveStage
from .behavior.stages.dream_facts import DreamFactsStage
from .behavior.stages.pattern_detector import PatternDetectorStage
from .behavior.stages.reflection_compose import ReflectionComposeStage
from .behavior.stages.write_window_summaries import WriteWindowSummariesStage
from .chat.surface import ChatReplyBroker, ChatSurface
from .chat_turns import ChatTurnStore
from .compose.templates import TemplateComposer
from .config import ConfigurationError, WombatConfig, load_config
from .cost.daily_spend_ledger import DailySpendLedger
from .domain.brief_schedule import BriefRunLedger
from .domain.daily_ledger import DailyLedger, wombat_today
from .domain.item_identity import idempotency_key
from .external_store import ExternalItemStore
from .gate.ceiling import CeilingLedger, FlushDayLatch
from .gate.decay import DayRollover
from .gate.gate import gate_item_from_queue_item
from .gate.models import GateAction, GateDecision, GateItem, ItemKind
from .gate.pending_journal_pg import PgPendingJournal
from .gate.pending_set import PendingSet
from .gate.pipeline import Gate
from .gate.quiet_hours import in_quiet_hours
from .gate.trigger import effective_urgency_threshold
from .integrations.gcal.token_store import TokenStore as GcalTokenStore
from .integrations.gmail.draft_composer import (
    DraftComposer,
    DraftTrailWriter,
    make_drafts_create_capability,
)
from .integrations.gmail.session import make_gmail_session
from .integrations.gmail.token_store import GMAIL_KEYRING_ACCOUNT
from .integrations.gmail.token_store import KeyringTokenStore as GmailKeyringTokenStore
from .integrations.gmail.token_store import TokenStore as GmailTokenStore
from .integrations.gmail.triage import load_triage_rules
from .kb.loader import load_psychology_kb
from .kb.schema import ValidationError as KBValidationError
from .observations import CurrentActivity, ObservationStore
from .observe_mic import MicInCallProbe
from .observe_screen import ScreenActivityCollector
from .params import OperatingParams, load_operating_params
from .pathways.brief_pathway import (
    BRIEF_PATHWAY_ID,
    BRIEF_SCHEDULE_PATHWAY_ID,
    brief_trigger_artifact,
    build_brief_pathway,
    build_brief_schedule_pathway,
)
from .pathways.drain_pathway import build_drain_pathway
from .pathways.dream_pathway import (
    DREAM_PATHWAY_ID,
    DreamBehaviorLogStage,
    DreamConsolidationStage,
    DreamOutcomeStage,
    DreamPersonaStage,
    DreamTuneStage,
    build_dream_pathway,
    dream_trigger_artifact,
)
from .pathways.dream_substrate import build_dream_substrate
from .pathways.dream_trigger import (
    DREAM_SCHEDULE_PATHWAY_ID,
    DreamRunLedger,
    DreamTimerStage,
    build_dream_schedule_pathway,
)
from .persona.feedback import FeedbackToken
from .persona.live import LivePersona
from .persona.matrix import matrix_from_config
from .queue import QueueItem, WombatQueue
from .rating.rating_tuner import RatingTuner
from .schema_preflight import ensure_all_schemas
from .scratchpad import ScratchpadStore
from .settings_store import SettingsStore
from .sinks.speak import SpeakSink
from .sources.bootstrap import (
    _has_google_client_credentials,
    build_brief_fetches,
    build_source_registry,
)
from .sources.chat_source import ChatSource
from .sources.presence import PresenceSnapshot, make_presence_provider
from .sources.registry import SourceRegistry
from .sources.seen_ledger import DedupingEnqueuer, SeenLedger
from .stages.brief_compose_stage import BriefComposeStage
from .stages.brief_deliver_stage import BriefDeliverStage
from .stages.brief_force_flush_stage import BriefForceFlushStage
from .stages.brief_gather_stage import BriefGatherStage
from .stages.brief_timer_stage import BriefTimerStage
from .stages.chat_reply import ChatReplyStage
from .stages.compose import ComposeStage
from .stages.compose_dispatch_router import ComposeDispatchRouter
from .stages.draft_dispatch import DraftDispatchStage
from .stages.drain_queue import DrainQueueStage
from .stages.gate_stage import GateStage, make_gate_evaluator
from .stages.review_or_speak import ReviewOrSpeakStage
from .stages.speech_shape import SpeechShapeStage
from .substrate import SubstrateBundle, build_substrate
from .trail.writer import ActionTrailWriter
from .user_facts import UserFactsStore
from .user_model.claims import Claim, ClaimPredicate
from .user_model.feedback_source import FeedbackSignal
from .user_model.observation_writer import ObservationWriter
from .user_model.outcome_inference import ItemDisposition
from .user_model.outcome_labeler import OutcomeLabeler
from .user_model.user_model import UserModel
from .voice.context_prefetch import (
    build_current_activity_context,
    build_user_facts_context,
    build_voice_context,
)
from .voice.reply_context import LastSpokenRegister
from .voice.select import build_tts_adapter

logger = logging.getLogger(__name__)

MODEL_PROFILE = "deepseek"

# TK-53 (Q-71) composition-root constants for the standing runtime.
DRAIN_PATHWAY_ID = "wombat.drain"
# TK-96: BRIEF_PATHWAY_ID ("wombat.brief") is imported above from pathways/brief_pathway.py —
# never redefined here, so the constant has exactly one owner.
_DRAIN_BATCH_SIZE = 1
# A plain composition-root default (mirrors sources/bootstrap.py's DEFAULT_*_POLL_INTERVAL_
# SECONDS pattern) — no ticket asked for a TK-13 tunable here, so this is not an OperatingParams
# field.
_DRAIN_POLL_INTERVAL_SECONDS = 5.0
# wombat is a single-user product (no multi-tenant ticket exists); the entity-KG scope this
# process's UserModel reads/writes under.
_RUNTIME_USER_ID = "wombat-user"

# CRF-3 (DEC-41(e)): the Engine's structural step ceiling (cog-worx engine.py names unbounded
# loops the anti-pattern; the DEFAULT is 1000) MUST stay a real runaway-loop guard while never
# tripping on a legitimate eternal self-park -- BriefTimerStage (TK-97) and DreamTimerStage
# (TK-52) are deliberately-eternal Wait(to=self) runs that accrue exactly one committed step per
# Sweeper re-drive, so the once-daily cadence would exhaust the cog-worx default after ~1000 days.
# 100_000 wakes outlives any plausible process lifetime while still catching a genuine runaway.
_ENGINE_MAX_STEPS = 100_000

_lock = threading.Lock()
_engine: Engine | None = None


def _log_engine_event(event: Event) -> None:
    """The wombat ``event_sink`` (CRF-3, DEC-41(e)): the ONE place an ``Engine``-emitted ``Event``
    is observed, so a run's terminal failure is never silent. ``RUN_FAILED`` (e.g. the
    ``max_steps`` ceiling tripping) logs LOUD at ERROR naming the ``run_id`` -- previously routed
    to a ``None`` sink and dropped. Every other lifecycle event logs at DEBUG only: this sink is a
    safety net against silent death, not a general-purpose event log.
    """
    if event.type is EventType.RUN_FAILED:
        logger.error(
            "cog-worx Engine: run %s FAILED (RUN_FAILED event) -- the run will not complete",
            event.run_id,
        )
    else:
        logger.debug("cog-worx Engine: %s run_id=%s", event.type, event.run_id)


def _deepseek_spec(config: WombatConfig) -> ModelSpec:
    """The DeepSeek ``ModelSpec`` descriptor (DeepSeek speaks the OpenAI-compatible protocol).

    Factored out of ``_deepseek_registry`` (TK-47) so ``assemble_runtime``'s dream-consolidation
    wiring (``build_dream_substrate``) can build the IDENTICAL descriptor rather than a second,
    independently-drifting copy — briefing's "the SAME deepseek ModelSpec build_engine registers".
    """
    return ModelSpec(
        provider="openai_compat",
        config=ProviderConfig(
            api_key=config.deepseek_api_key,
            base_url=config.deepseek_base_url,
            model_pro="deepseek-chat",
            model_flash="deepseek-chat",
        ),
        # capabilities is REQUIRED for a non-None base_url (DeepSeek endpoint).
        capabilities=ModelCapabilities(structured_output=True, streaming=True, tools=True),
    )


def _deepseek_registry(config: WombatConfig) -> ModelRegistry:
    """Register the DeepSeek profile as a ModelSpec descriptor (no client built here)."""
    registry = ModelRegistry()
    registry.register_spec(MODEL_PROFILE, _deepseek_spec(config))
    return registry


def _personality() -> PersonalityProfile:
    return PersonalityProfile(
        name="wombat", role="a quiet personal assistant", tone="concise and calm"
    )


def build_engine(
    bundle: SubstrateBundle | None = None,
    *,
    config: WombatConfig | None = None,
    params: OperatingParams | None = None,
    capability_registry: Registry | None = None,
) -> Engine:
    """Assemble (once) and return the wombat Engine. Idempotent: a second call returns the same
    instance — never a silent duplicate (AC3). ``bundle``/``config``/``params`` default to the
    cold-boot substrate, env config, and the packaged ``wombat_params.yaml`` respectively.

    ``capability_registry`` (TK-177, EP-18) lets a caller (``assemble_runtime``) hand in an
    ALREADY assembled ``Registry`` (e.g. carrying the Q-67-gated ``gmail.drafts.create``
    capability) so this is the ONE ``Registry`` the engine ever dispatches through — never a
    second, empty one. Defaults to a bare ``Registry()`` (behavior-preserving for every existing
    caller that never passes this).
    """
    global _engine
    with _lock:
        if _engine is None:
            cfg = config if config is not None else load_config()
            sub = bundle if bundle is not None else build_substrate()
            op = params if params is not None else load_operating_params()
            registry = capability_registry if capability_registry is not None else Registry()
            _engine = Engine(
                models=_deepseek_registry(cfg),
                journal=sub.journal,
                graph_store=sub.graph_store,
                latent=sub.latent,
                pathways=sub.pathways,
                model_profile=MODEL_PROFILE,
                # TK-9 layer 1 (Q-68): real per-drive ceilings from OperatingParams — was the
                # unbounded BudgetPolicy() default.
                budget_policy=BudgetPolicy(
                    max_usd_per_drive=op.mouth_max_usd_per_drive,
                    max_calls_per_drive=op.mouth_max_calls_per_drive,
                ),
                registry=registry,
                recall_stack=RecallStack(channels=[]),
                personality=_personality(),
                rules=RuleSet(),
                # CRF-3 (DEC-41(e)): a real (not cog-worx's 1000-step default) runaway-loop
                # ceiling, plus a logging sink so a RUN_FAILED (or any other) event is never
                # dropped into a None sink -- no run ever dies silent.
                max_steps=_ENGINE_MAX_STEPS,
                event_sink=_log_engine_event,
            )
        return _engine


def reset_engine() -> None:
    """Drop the process singleton so the next build_engine() reassembles. For tests / re-init."""
    global _engine
    with _lock:
        _engine = None


def build_compose_stage(
    *,
    config: WombatConfig,
    dsn: str,
    params: OperatingParams | None = None,
    tz: ZoneInfo,
    daily_ledger: DailyLedger | None = None,
    live_persona: LivePersona | None = None,
) -> ComposeStage:
    """Assemble the mouth's ``ComposeStage`` wired with TK-9 layer 2 (Q-68): a real
    ``DailySpendLedger`` (over a ``DailyLedger`` on ``dsn``) and the ``mouth_daily_token_ceiling``
    tunable from OperatingParams, so the pre-call ceiling gate and post-call token accounting are
    live — not the optional-and-disabled ``ComposeStage`` defaults.

    ``daily_ledger`` (TK-173, CR-15) lets a caller (``assemble_runtime``) hand in an ALREADY
    constructed ``DailyLedger`` so this stage shares that ONE instance/connection/close()
    lifecycle instead of opening a second one on the same ``dsn``. Defaults to constructing a
    fresh ``DailyLedger(dsn, tz=tz)`` for standalone callers (tests, ``scripts/demo_drain.py``).

    ``live_persona`` (TK-209) threads the runtime persona authority through unchanged — ``None``
    (the default) preserves ``ComposeStage``'s own frozen-at-__init__ instruction behavior for
    every standalone caller that doesn't wire one. TK-216: the SAME ``live_persona`` is also
    handed to ``TemplateComposer`` so the degrade path's brevity wrapper variant reads the CURRENT
    matrix too — the one ``TemplateComposer`` construction site.
    """
    op = params if params is not None else load_operating_params()
    ledger = daily_ledger if daily_ledger is not None else DailyLedger(dsn, tz=tz)
    spend_ledger = DailySpendLedger(ledger)
    return ComposeStage(
        config=config,
        template_composer=TemplateComposer(live_persona=live_persona),
        spend_ledger=spend_ledger,
        daily_token_ceiling=op.mouth_daily_token_ceiling,
        live_persona=live_persona,
        timeout_seconds=op.mouth_model_timeout_seconds,
    )


def build_brief_compose_stage(
    *,
    config: WombatConfig,
    dsn: str,
    params: OperatingParams | None = None,
    tz: ZoneInfo,
    daily_ledger: DailyLedger | None = None,
    live_persona: LivePersona | None = None,
) -> BriefComposeStage:
    """Assemble the morning brief's ``BriefComposeStage`` wired with the SAME TK-9 layer 2 budget
    plumbing as ``build_compose_stage``: a real ``DailySpendLedger`` over a ``DailyLedger`` on the
    SAME ``dsn``/``tz`` and the SAME ``"spend:tokens"`` ledger row, plus the
    ``mouth_daily_token_ceiling`` tunable from OperatingParams — so drain and brief share ONE
    daily token cap rather than each hand-rolling its own (the Q-69-lesson wiring, TK-53).

    ``daily_ledger`` (TK-173, CR-15) mirrors ``build_compose_stage``'s own seam: pass the SAME
    already constructed ``DailyLedger`` so ``assemble_runtime`` closes exactly one connection per
    assembly, not one per compose stage. Defaults to constructing a fresh one for standalone
    callers.

    ``live_persona`` (TK-209) threads the runtime persona authority through unchanged — ``None``
    (the default) preserves ``BriefComposeStage``'s own frozen-at-__init__ instruction behavior
    for every standalone caller that doesn't wire one.
    """
    op = params if params is not None else load_operating_params()
    ledger = daily_ledger if daily_ledger is not None else DailyLedger(dsn, tz=tz)
    spend_ledger = DailySpendLedger(ledger)
    return BriefComposeStage(
        config=config,
        tz=tz,
        spend_ledger=spend_ledger,
        daily_token_ceiling=op.mouth_daily_token_ceiling,
        live_persona=live_persona,
        timeout_seconds=op.mouth_model_timeout_seconds,
    )


def build_brief_deliver_stage(
    *,
    config: WombatConfig,
    tz: ZoneInfo,
    speak: Callable[[str], None] | None = None,
    on_spoken: Callable[[str, str], None] | None = None,
) -> BriefDeliverStage:
    """Assemble the morning brief's terminal ``BriefDeliverStage`` (TK-101, Q-78).

    Resolves the append-only text-sink path from ``config.wombat_brief_path``; a blank/absent
    path fails LOUD at construction (``ConfigurationError`` naming it) rather than wiring a
    stage that would raise on its first delivery. ``config.wombat_voice_enabled`` gates voice;
    ``speak`` is the injected voice sink (EP-30 narrowed) — passed through untouched, ``None`` by
    default so callers that never wire a voice provider get text-only delivery. ``on_spoken``
    (TK-288, DEC-64 gap A) is passed through untouched too — ``None`` by default so standalone
    callers stay byte-identical.
    """
    raw_path = config.wombat_brief_path
    if raw_path is None or not raw_path.strip():
        msg = (
            "build_brief_deliver_stage: WOMBAT_BRIEF_PATH is missing/blank; "
            "the sink cannot be wired"
        )
        raise ConfigurationError(msg)
    return BriefDeliverStage(
        sink_path=Path(raw_path),
        tz=tz,
        voice_enabled=config.wombat_voice_enabled,
        speak=speak,
        on_spoken=on_spoken,
    )


def build_speak_sink(config: WombatConfig) -> SpeakSink:
    """Assemble the drain pathway's terminal ``SpeakSink`` (TK-164, Q-96; rerouted by TK-193).

    ``config.wombat_voice_enabled`` gates voice; when it is true, this ALSO attempts to construct
    the configured ``TTSAdapter`` via ``voice.select.build_tts_adapter`` (local by default, or a
    cloud provider wrapped in a local-fallback wrapper per DEC-28) — any construction/selection
    gap degrades to ``adapter=None`` (logged loud by ``build_tts_adapter``) rather than raising,
    so a voice-off/lib-less boot is unaffected (AC4). ``voice_enabled=False`` never even attempts
    construction — no cloud, no local.
    """
    adapter = build_tts_adapter(config) if config.wombat_voice_enabled else None
    return SpeakSink(voice_enabled=config.wombat_voice_enabled, adapter=adapter)


def build_speech_shape_stage(
    *,
    config: WombatConfig,
    dsn: str,
    params: OperatingParams | None = None,
    tz: ZoneInfo,
    daily_ledger: DailyLedger | None = None,
    adapter_present: bool,
) -> SpeechShapeStage:
    """Assemble the drain pathway's ``speech_shape`` hop (TK-267, DEC-55): a SECOND DeepSeek mouth
    call, wired with the SAME TK-9 layer 2 budget plumbing as ``build_compose_stage`` — a real
    ``DailySpendLedger`` over a ``DailyLedger`` on the SAME ``dsn``/``tz`` and the SAME
    ``"spend:tokens"`` ledger row, plus the SAME ``mouth_daily_token_ceiling`` tunable, so both
    mouths share ONE daily token cap.

    ``daily_ledger`` (mirrors ``build_compose_stage``'s own CR-15 seam) lets a caller hand in an
    ALREADY constructed ``DailyLedger`` so this stage shares that ONE instance/connection rather
    than opening a second one on the same ``dsn``.

    ``adapter_present`` mirrors ``config.wombat_voice_enabled`` gating ``build_speak_sink``'s own
    adapter construction — ``assemble_runtime`` passes whether the SAME TTS adapter it built for
    ``SpeakSink`` is non-``None``, so this stage's pre-call gate (voice on AND adapter present)
    matches ``SpeakSink``'s own gate exactly.
    """
    op = params if params is not None else load_operating_params()
    ledger = daily_ledger if daily_ledger is not None else DailyLedger(dsn, tz=tz)
    spend_ledger = DailySpendLedger(ledger)
    return SpeechShapeStage(
        config=config,
        voice_enabled=config.wombat_voice_enabled,
        adapter_present=adapter_present,
        spend_ledger=spend_ledger,
        daily_token_ceiling=op.mouth_daily_token_ceiling,
        timeout_seconds=op.mouth_model_timeout_seconds,
        # TK-303 (DEC-67e): the DEC-64 spoken-reply length cap, restart-tier from the settings
        # table/env (no hot-apply).
        max_chars=config.wombat_spoken_reply_max_chars,
        # TK-318 (DEC-69b): the pane's-actual-reply voice opt-in, restart-tier (no hot-apply).
        speak_full_replies=config.wombat_speak_full_replies,
    )


def make_speak_callable(config: WombatConfig) -> Callable[[str], None] | None:
    """Build the voice closure ``BriefDeliverStage``'s injected ``speak`` seam consumes (TK-101,
    Q-78; rerouted by TK-193) — the SAME adapter TYPE/selection ``build_speak_sink`` binds into
    the drain pathway (Q-96's "ONE adapter, two delivery points").

    Returns a closure over ``voice.select.build_tts_adapter``'s result iff
    ``config.wombat_voice_enabled`` AND that construction/selection succeeds; otherwise ``None``
    (logged loud by ``build_tts_adapter``) — ``BriefDeliverStage`` already treats ``speak=None``
    as text-only delivery (TK-101), so a voice-off/lib-less boot stays byte-identical to today.
    """
    if not config.wombat_voice_enabled:
        return None
    adapter = build_tts_adapter(config)
    if adapter is None:
        return None
    return adapter.speak


def _epoch_now() -> float:
    """The real-clock default for the epoch-seconds seams (the gate/presence provider) this
    module composes — the ONE place ``assemble_runtime`` reads a wall clock directly."""
    return datetime.now(UTC).timestamp()


def _utc_now() -> datetime:
    """The real-clock default for ``BriefGatherStage``'s ``datetime``-typed clock seam (TK-96) —
    mirrors ``_epoch_now`` above, just not epoch-seconds shaped."""
    return datetime.now(UTC)


def build_draft_composer_stage(
    *,
    writer: DraftTrailWriter,
    clock: Callable[[], datetime] = _utc_now,
    assistant_name: str = "Steward",
    live_persona: LivePersona | None = None,
    timeout_seconds: float | None = None,
) -> DraftComposer:
    """Assemble TK-78's ``DraftComposer`` via a small bootstrap factory (TK-177, the Q-69
    assemble-via-factory lesson) — a thin, directly-testable wrapper mirroring
    ``build_compose_stage``/``build_brief_compose_stage`` above, rather than ``assemble_runtime``
    constructing the stage inline. ``DraftComposer.__init__`` already self-binds the TK-151
    external tier policy (``bind_external_tier``) — nothing further to wire here. ``assistant_name``
    (TK-194) threads ``config.wombat_assistant_name`` into the system instruction only; the default
    preserves every existing caller's behavior unchanged. ``live_persona`` (TK-209) threads the
    runtime persona authority through unchanged — ``None`` (the default) preserves the frozen
    ``assistant_name``-only instruction for every standalone caller that doesn't wire one.
    ``timeout_seconds`` (TK-283, DEC-61) is OPTIONAL — ``None`` (the default) preserves
    ``DraftComposer``'s own ``_DEFAULT_TIMEOUT_SECONDS`` for every standalone caller that doesn't
    wire the ``mouth_model_timeout_seconds`` tunable; ``assemble_runtime`` passes it explicitly.
    """
    if timeout_seconds is not None:
        return DraftComposer(
            writer=writer,
            clock=clock,
            assistant_name=assistant_name,
            live_persona=live_persona,
            timeout_seconds=timeout_seconds,
        )
    return DraftComposer(
        writer=writer, clock=clock, assistant_name=assistant_name, live_persona=live_persona
    )


def _guard_drain_batch_size(batch_size: int) -> None:
    """Loud guard (TK-172, CR-10) at the ONE place the drain batch size is consumed.

    ``gate.pipeline`` (``gate/pipeline.py``) can return ``SURFACE_IMMEDIATE`` MID-ITERATION —
    the rest of the batch is never scored or held. Yet ``gate_stage`` pairs that ONE decision
    with EVERY drained item, and ``review_or_speak`` acks them all. This is safe ONLY because
    ``_DRAIN_BATCH_SIZE`` is 1 (exactly one item per decision, nothing dropped). Raise loud
    rather than let a future batch_size bump silently strand unscored, wrongly-acked items —
    batch>1 needs its own decision-wire redesign (gate_stage/review_or_speak), not a quiet
    constant change here.
    """
    if batch_size != 1:
        msg = (
            f"drain batch_size={batch_size!r} != 1: gate.pipeline's mid-batch SURFACE_IMMEDIATE "
            "return is paired with the WHOLE drained batch by gate_stage and acked wholesale by "
            "review_or_speak -- safe only at batch_size=1. Batch>1 needs a decision-wire "
            "redesign, not a constant change (TK-172, CR-10)."
        )
        raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class RuntimeBundle:
    """Everything ``wombat.runtime.serve()`` needs to start/drive/stop the standing process
    (TK-53). Composed ONCE by ``assemble_runtime``; ``runtime.py`` only starts/drives/stops these
    seams — it registers nothing (DEC-8, the ticket's own non_goal).
    """

    engine: Engine
    pathways: PathwayRegistry
    journal: Journal
    drain_pathway_id: str
    # TK-46 (Q-85): the registered ``wombat.dream`` off-path scaffold pathway id — registration is
    # UNCONDITIONAL (the scaffold has no external deps), so unlike ``brief_pathway_id`` this is
    # never ``None``. TK-52's nightly trigger/fence wires against this field.
    dream_pathway_id: str
    # TK-52 (Q-85): the registered ``wombat.dream_schedule`` pathway id (the once-nightly dream
    # timer) — registration is UNCONDITIONAL (mirrors ``dream_pathway_id`` above; the dream
    # scaffold needs no external config), but the field stays ``str | None`` to mirror ``brief_
    # schedule_pathway_id``'s shape (``runtime.serve()`` gates its third boot drive on it being
    # non-None regardless).
    dream_schedule_pathway_id: str | None
    source_registry: SourceRegistry
    pending_journal: PgPendingJournal
    queue: WombatQueue
    daily_ledger: DailyLedger
    compose_stage: ComposeStage
    # TK-209 (EP-33, DEC-34/DEC-37(g)): the ONE composition-root-owned runtime persona authority —
    # the same instance threaded into all four mouth constructions above (AC1's identity-through-
    # reroute holds because every mouth reads THIS instance). Also the TK-212/TK-214/TK-215
    # write/read seam (voice commands, nightly persona learning, gate proactivity actuation).
    live_persona: LivePersona
    # TK-96: the registered ``wombat.brief`` pathway id, or ``None`` when ``config.wombat_brief_
    # path`` was blank/absent and registration was skipped (TK-97's entrypoint reads this field).
    brief_pathway_id: str | None
    # TK-97: the registered ``wombat.brief_schedule`` pathway id (the once-daily brief timer), or
    # ``None`` when the brief path was blank/absent (BOTH brief + schedule are skipped together).
    # ``runtime.serve()`` fires a second initial drive on this pathway only when it is non-None.
    brief_schedule_pathway_id: str | None
    # TK-176: the ONE shared user-scope entity KG (replaces the TK-53 throwaway
    # InMemoryEntityKG()) — IS the KG UserModel reads through and observation_writer writes
    # through (AC4). V1 honesty (Q-36/TK-14): in-memory, resets per process.
    entity_kg: EntityKG
    # TK-176: the ONE ObservationWriter over entity_kg (S7) — the write seam both the hot-path
    # feedback-absorb closure and OutcomeLabeler (via stamp_resolution) go through.
    observation_writer: ObservationWriter
    # TK-111 (Q-98): the ONE BehaviorEventLog instance the nightly DreamBehaviorLogStage writes
    # through, over the SAME runtime dsn every other Postgres seam here uses. UNCONDITIONAL
    # (mirrors dream_pathway_id — no external deps beyond the shared entity_kg), so unlike
    # action_trail_writer below this is never None; runtime.py's teardown closes it via the SAME
    # TK-184 finally pattern.
    behavior_event_log: BehaviorEventLog
    # TK-184 (CR2-10): the ActionTrailWriter constructed only when Google client creds + a stored
    # Gmail token are both present (WIRE 2/3 below), shared by draft_composer_stage/draft_dispatch_
    # stage — was previously constructed here but never exposed, so runtime's teardown never
    # closed it (the exact leak class TK-173/CR-15 closed for DailyLedger). ``None`` on a
    # Google-less/token-less boot; ``runtime.py``'s teardown closes it only when non-None.
    action_trail_writer: ActionTrailWriter | None = None
    # TK-222 (EP-32, Q-110(d)): the loopback chat surface — ``None`` when ``config.wombat_chat_
    # handshake_file`` was blank/absent (chat disabled). ``runtime.serve()`` starts/stops this
    # GUARDED (CON-3): any start/run failure is ONE loud WARNING, the rest of the bundle
    # (drain loop, brief, other sources) is unaffected.
    chat_surface: ChatSurface | None = None
    # TK-269 (DEC-56a): pass-through to the SAME ``ChatSource`` instance registered into
    # ``source_registry`` above (mirrors the ``chat_surface`` field precedent) — ``None`` exactly
    # when chat is disabled (chat_surface is also None then). ``runtime._drive_and_serve`` uses
    # this to hand the running-loop ``DrainWake``'s ``set`` callable to the source, wiring the
    # interactive-enqueue wake WITHOUT this module (constructed too early for a loop-bound wake)
    # needing to know anything about it.
    chat_source: ChatSource | None = None
    # TK-245 (DEC-45(c)/(d), ruling v2.68 r6): the source-poll store sink target — ALWAYS
    # constructed by assemble_runtime (dsn is a required str, ExternalItemStore is fully lazy —
    # no connection at construction), typed Optional with default None ONLY to mirror the
    # action_trail_writer/chat_surface field-declaration precedent above so hand-rolled
    # RuntimeBundle constructions elsewhere (tests) don't need to pass it.
    external_item_store: ExternalItemStore | None = None
    # TK-247 (DEC-46, ruling v2.68 r5): the scoped working-memory store — ALWAYS constructed by
    # assemble_runtime (dsn is a required str, ScratchpadStore is fully lazy — no connection at
    # construction), typed Optional with default None ONLY to mirror the external_item_store
    # field-declaration precedent above so hand-rolled RuntimeBundle constructions elsewhere
    # (tests) don't need to pass it.
    scratchpad_store: ScratchpadStore | None = None
    # TK-295 (DEC-65e): the 7-day rolling chat/voice-turn ledger — ALWAYS constructed by
    # assemble_runtime (dsn is a required str, ChatTurnStore is fully lazy — no connection at
    # construction), typed Optional with default None ONLY to mirror the scratchpad_store
    # field-declaration precedent above so hand-rolled RuntimeBundle constructions elsewhere
    # (tests) don't need to pass it.
    chat_turn_store: ChatTurnStore | None = None
    # TK-310 (DEC-68(a)/(c)): the ambient-observability screen channel — ``observation_store``,
    # ``current_activity``, and ``screen_collector`` are constructed TOGETHER, ONLY when
    # ``config.wombat_observe_screen`` is true at ``assemble_runtime`` time (structural inertness:
    # toggle off => all three stay ``None`` => no store, no writes, no polling). Unlike external_
    # item_store/scratchpad_store/chat_turn_store above, these are NOT unconditionally constructed
    # — the toggle gate IS the point (consent-gated capture, DEC-68(b)).
    observation_store: ObservationStore | None = None
    current_activity: CurrentActivity | None = None
    screen_collector: ScreenActivityCollector | None = None
    # TK-313 (DEC-68(a)/(e)): the ambient-observability mic channel — constructed ONLY when
    # config.wombat_observe_mic is true (the SAME structural-inertness contract as screen_
    # collector above). Shares observation_store/current_activity with the screen channel (ONE
    # CurrentActivity single-slot snapshot, DEC-68(a) — either toggle alone is enough to
    # construct them; observations.CurrentActivity.in_call ships from birth for exactly this).
    mic_probe: MicInCallProbe | None = None


def assemble_runtime(
    *,
    config: WombatConfig,
    dsn: str,
    params: OperatingParams | None = None,
    tz: ZoneInfo,
    replay_pending: bool = True,
    gmail_token_store: GmailTokenStore | None = None,
    gcal_token_store: GcalTokenStore | None = None,
) -> RuntimeBundle:
    """Compose the ONE standing wombat process (TK-53, Q-71).

    ``gmail_token_store`` (TK-177, EP-18) lets a caller/test override the real OS-keyring token
    store the outbound Gmail wiring (WIRE 2/3 below) reads its Q-67 presence check against —
    mirrors ``build_source_registry``'s own ``gmail_token_store`` seam; it is threaded into
    BOTH that call and the outbound wiring's own check below, so the source-side read wiring and
    the draft-side write wiring always agree on the SAME stored credential. Defaults to ``None``
    (the real ``GmailKeyringTokenStore``), behavior-preserving for every existing caller.

    ``gcal_token_store`` (TK-254, ISS-10(a)) mirrors ``gmail_token_store`` above for the gcal
    seam — threaded into BOTH ``build_source_registry`` and ``build_brief_fetches`` (the source
    poller and the morning-brief fetch share this exact wired/unwired decision, TK-96) so tests
    can inject an in-memory fake instead of ever touching the real OS keyring. Defaults to
    ``None`` (the real ``GcalKeyringTokenStore``), behavior-preserving for every existing caller.

    Registers the TK-7 drain pathway (id ``DRAIN_PATHWAY_ID``) and wires the REAL production
    ``Gate`` (TK-27) — a durable ``PendingSet`` backed by the TK-29 Postgres ``PendingJournal``
    (the Q-70/RISK-5 boot obligation), a ``CeilingLedger`` over a real ``DailyLedger``, a
    ``FlushDayLatch`` (TK-287) over the SAME ``DailyLedger``, the real presence provider, and
    ``UserModel`` ratings — over the SAME ``build_engine``/
    ``build_compose_stage``/``build_source_registry`` factories every other composition path
    uses (never hand-rolled, closing the ``scripts/demo_drain.py`` Q-69 budget-bypass gap).

    ``dsn`` backs every Postgres-touching seam composed here (the queue, the daily ledger, and
    the pending journal) — ONE Postgres, per ASMP-2. Every adapter below is lazy (no connection
    at construction) EXCEPT the pending-set boot replay (TK-166, CR-1): with ``replay_pending=
    True`` (the DEFAULT — the ``serve()`` production posture) assembly performs ONE eager read
    of the pending journal via ``PendingSet.rebuild_from_journal`` so held items journaled by a
    PRIOR process survive into this gate — a restart is this product's normal operating
    condition, not a data-loss event. Pass ``replay_pending=False`` for a connection-free
    assembly (tests/tooling): the cold ``PendingSet(journal=..., max_pending=...)`` constructor
    stands, an unreachable ``dsn`` is safe, and the first real I/O happens once the returned
    bundle is actually driven.

    TK-203 (CR3-1, Q-104): on the SAME ``replay_pending=True`` posture, assembly's FIRST pg act
    (before even the eager replay above) is ``schema_preflight.ensure_all_schemas(dsn)`` — it
    applies every packaged ``ensure_schema`` migration so a brand-new, empty Postgres boots
    clean instead of crashing the eager replay with ``UndefinedTable`` (the 2026-07-09 incident).
    Gated on ``replay_pending`` rather than run unconditionally: that flag already marks this
    posture as pg-eager, so the pre-flight adds no new reachability requirement, and the
    ``replay_pending=False`` posture stays connection-free as documented above.
    """
    op = params if params is not None else load_operating_params()

    # TK-209 (EP-33, DEC-34/DEC-37(g)): the ONE composition-root-owned runtime persona authority
    # — built once here from the config-level persona fields, threaded into all four mouth
    # constructions below, and exposed on RuntimeBundle.live_persona (the TK-212/TK-214/TK-215
    # write/read seam). Voice-off and default-config boots construct this identically — it is not
    # a voice feature. TK-243 (DEC-43): backed by a SettingsStore over the SAME dsn as every
    # other Postgres seam here — construction is fully lazy (SettingsStore itself connects lazily,
    # LivePersona touches it not at all until its first poll), so this stays safe on the
    # replay_pending=False/connection-free posture too.
    live_persona = LivePersona(
        matrix_from_config(config),
        config.wombat_assistant_name,
        store=SettingsStore(dsn),
        # TK-292 (DEC-65a/c): the CHAT mouth's second name slot — "" -> None, so LivePersona/
        # instruction_for fall back to "the user" exactly like the field's own default renders.
        user_name=config.wombat_user_name or None,
    )

    # The v1 cold-boot substrate (Q-36/TK-14): in-memory journal/graph/latent + a FRESH
    # PathwayRegistry. This EXACT registry is handed to build_engine below, so the pathway
    # registered on it here is what the Engine resolves at run/resume/fire_timer time.
    substrate = build_substrate()

    # TK-203 (CR3-1, Q-104): the pre-flight — the FIRST pg act on this posture, before the eager
    # replay below — applies every packaged migration so a brand-new database boots clean.
    if replay_pending:
        ensure_all_schemas(dsn)

    queue = WombatQueue(dsn, max_size=op.max_pending)
    pending_journal = PgPendingJournal(dsn)
    # TK-166 (CR-1, Q-83): the ONE eager read this composition performs — replay the durable
    # journal into the gate's pending set so a prior process's held items survive a restart.
    pending_set = (
        PendingSet.rebuild_from_journal(pending_journal, max_pending=op.max_pending)
        if replay_pending
        else PendingSet(journal=pending_journal, max_pending=op.max_pending)
    )
    daily_ledger = DailyLedger(dsn, tz=tz)
    ceiling = CeilingLedger(
        daily_ledger=daily_ledger, per_class_daily_ceiling=op.per_class_daily_ceiling
    )
    # TK-28 (Q-73): DayRollover composed over the SAME DailyLedger instance as CeilingLedger
    # (ceiling.py precedent) so the exactly-once boundary observation and the per-class ceiling
    # share ONE durable row lifecycle.
    day_rollover = DayRollover(daily_ledger=daily_ledger)
    # TK-287 (DEC-63b): FlushDayLatch composed over the SAME DailyLedger instance too — the
    # once-per-wombat-day gate on the load-flush arm, restart-durable (Postgres-backed, not
    # in-memory). No tunable to inject: once per wombat day is pinned (DEC-63 rejected a
    # cooldown knob).
    flush_latch = FlushDayLatch(daily_ledger=daily_ledger)

    # TK-176: ONE shared user-scope entity KG (replaces the TK-53 throwaway InMemoryEntityKG())
    # threaded into BOTH the read seam (UserModel) and the write seam (ObservationWriter, via
    # OutcomeLabeler) — additive RuntimeBundle.entity_kg/observation_writer expose these SAME
    # instances (AC4). V1 honesty (Q-36/TK-14): in-memory, resets per process; no persistence.
    entity_kg = InMemoryEntityKG()
    scope_registry = ScopeRegistry()
    observation_writer = ObservationWriter(
        entity_kg=entity_kg, scope_registry=scope_registry, user_id=_RUNTIME_USER_ID
    )
    outcome_labeler = OutcomeLabeler(writer=observation_writer)
    user_model = UserModel(entity_kg=entity_kg, user_id=_RUNTIME_USER_ID)
    gate = Gate(
        user_model=user_model,
        pending_set=pending_set,
        ceiling=ceiling,
        urgency_threshold=op.urgency_threshold,
        load_flush_threshold=op.load_flush_threshold,
        flush_min_age_seconds=op.flush_min_age_seconds,
        decay_ttl_seconds=op.decay_ttl_seconds,
        day_rollover=day_rollover,
        clock=_epoch_now,
        flush_latch=flush_latch,
        # TK-215 (DEC-37(a)/Q-107(a)): reads the LIVE proactivity level at scoring time — a
        # live_persona.set() between two scored items lands on the very next item, no restart.
        threshold_fn=lambda: effective_urgency_threshold(
            op.urgency_threshold, live_persona.matrix.proactivity, op.personality_band
        ),
    )
    presence_provider = make_presence_provider(
        clock=_epoch_now,
        staleness_ceiling_s=op.presence_staleness_ceiling_seconds,
        idle_threshold_s=op.presence_idle_threshold_seconds,
    )

    async def absorb_feedback(item: QueueItem) -> None:
        """TK-176 (AC1): the hot-path feedback-diversion write. Parses the TK-51 ``FeedbackSignal``
        wire off ``item.payload``, records ONE ``BEHAVIOR_OBSERVED`` claim (subject=item_ref) via
        the SAME shared ``observation_writer``, then acks the item off the SAME
        ``WombatQueue.ack`` call ``ReviewOrSpeakStage`` uses. Any exception (a malformed payload
        or a KG-write failure) propagates to ``GateStage``'s own caught-and-logged fault posture,
        which deliberately leaves the item un-acked so the at-least-once queue redelivers it.
        """
        signal = FeedbackSignal.from_payload(item.payload)
        await observation_writer.record(
            Claim(
                predicate=ClaimPredicate.BEHAVIOR_OBSERVED,
                subject=signal.item_ref,
                value=json.dumps({"kind": "feedback", "response": signal.response}),
                event_id=None,
                observed_at=_utc_now(),
            )
        )
        assert item.item_id is not None, (
            "assemble_runtime.absorb_feedback: a drained queue_item must carry a "
            "server-assigned item_id"
        )
        queue.ack(item.item_id)

    def _disposition_for(action: GateAction) -> ItemDisposition:
        """TK-176: GateAction -> the Q-90 closed ItemDisposition vocabulary — every non-HOLD
        action is a surface (mirrors review_or_speak's own _SURFACE_ACTIONS closed set)."""
        return "held" if action is GateAction.HOLD else "surfaced"

    async def stamp_resolution(decision: GateDecision, queue_item: QueueItem) -> None:
        """TK-176 (AC2): the hot-path OUTCOME_PENDING stamp — Q-22 BINDS, never a terminal
        OUTCOME_* here. Resolves the SAME EventClass the gate itself scored this item under,
        stamps 'surfaced'/'held' from the gate's own decision, and carries the item's
        payload-borne 'event_id' when the source supplied one (None otherwise)."""
        gate_item = gate_item_from_queue_item(queue_item)
        event_class = user_model.resolve_event_class(gate_item)
        raw_event_id = queue_item.payload.get("event_id")
        event_id = str(raw_event_id) if raw_event_id is not None else None
        await outcome_labeler.stamp_pending(
            item_ref=queue_item.idempotency_key,
            event_class=event_class,
            disposition=_disposition_for(decision.action),
            resolved_at=_utc_now(),
            event_id=event_id,
        )

    _guard_drain_batch_size(_DRAIN_BATCH_SIZE)
    drain_queue_stage = DrainQueueStage(
        queue,
        batch_size=_DRAIN_BATCH_SIZE,
        poll_interval_seconds=_DRAIN_POLL_INTERVAL_SECONDS,
    )
    _inner_gate_evaluate = make_gate_evaluator(
        gate=gate,
        staleness_ceiling_s=op.presence_staleness_ceiling_seconds,
        confidence_floor=op.presence_confidence_floor,
        clock=_epoch_now,
    )

    # TK-315 (ISS-31 3): mutable single-slot state so the wrapper below logs its INFO line only
    # on a window state CHANGE, not on every evaluate pass (hold behavior itself is
    # byte-unchanged — this only trims logging volume).
    _quiet_hours_state: dict[str, bool] = {"active": False}

    async def _quiet_hours_gate_evaluate(
        items: list[GateItem], presence: PresenceSnapshot | None
    ) -> GateDecision:
        """TK-304 (DEC-67g, RULING v2.172 r6): holds the immediate-voice arm across
        ``config.wombat_quiet_start``-``config.wombat_quiet_end`` by forcing ``presence=None``
        into the inner evaluator — the SAME canonical ``presence_hold`` predicate every other
        presence-first call site uses (``gate_stage.py``'s own ``make_gate_evaluator``) then
        degrades that to HOLD, pending set intact (reduce-only, CON-2/CON-3 clean). Out of
        window this is a byte-transparent passthrough — ``presence`` reaches the inner evaluator
        unchanged.

        TK-315 (ISS-31 3): the entering/leaving INFO line below fires ONLY on a window state
        CHANGE (one INFO entering, one INFO leaving), not on every evaluate pass — hold behavior
        is otherwise identical."""
        now_active = in_quiet_hours(
            datetime.now(tz).time(), config.wombat_quiet_start, config.wombat_quiet_end
        )
        if now_active and not _quiet_hours_state["active"]:
            logger.info(
                "gate: quiet hours active (%s-%s); holding the immediate-voice arm",
                config.wombat_quiet_start,
                config.wombat_quiet_end,
            )
        elif not now_active and _quiet_hours_state["active"]:
            logger.info(
                "gate: quiet hours ended (%s-%s); immediate-voice arm resumed",
                config.wombat_quiet_start,
                config.wombat_quiet_end,
            )
        _quiet_hours_state["active"] = now_active
        if now_active:
            return await _inner_gate_evaluate(items, None)
        return await _inner_gate_evaluate(items, presence)

    # TK-313 (DEC-68(a)/(e)): mutable single-slot state so the wrapper below logs its INFO line
    # only on an in-call state CHANGE, not on every evaluate pass (mirrors the TK-315 quiet-hours
    # convention above exactly).
    _mic_in_call_state: dict[str, bool] = {"active": False}

    async def _mic_in_call_gate_evaluate(
        items: list[GateItem], presence: PresenceSnapshot | None
    ) -> GateDecision:
        """TK-313 (DEC-68(a)/(e)): a wrapper-of-wrapper composing OVER
        ``_quiet_hours_gate_evaluate`` (gate_stage.py itself byte-untouched). Reads the LIVE
        ``current_activity.in_call`` flag (flipped in place by ``MicInCallProbe``'s own poll
        loop — never invoked directly here) at evaluate time and, when True, forces
        ``presence=None`` into ``_quiet_hours_gate_evaluate``, which folds its OWN quiet-hours
        check on top before reaching the inner evaluator — the SAME canonical presence_hold
        predicate then degrades that to HOLD, pending set intact. Out of call this is a
        byte-transparent passthrough to ``_quiet_hours_gate_evaluate`` (including its own
        quiet-hours behavior). Only installed when ``config.wombat_observe_mic`` is true —
        otherwise ``gate_stage.evaluate`` is ``_quiet_hours_gate_evaluate`` itself (AC6:
        byte-identical to the pre-arc wiring)."""
        now_in_call = current_activity is not None and current_activity.in_call
        if now_in_call and not _mic_in_call_state["active"]:
            logger.info("gate: in-call detected — holding the immediate-voice arm")
        elif not now_in_call and _mic_in_call_state["active"]:
            logger.info("gate: call ended — immediate-voice arm resumed")
        _mic_in_call_state["active"] = now_in_call
        if now_in_call:
            return await _quiet_hours_gate_evaluate(items, None)
        return await _quiet_hours_gate_evaluate(items, presence)

    gate_stage = GateStage(
        evaluate=(
            _mic_in_call_gate_evaluate if config.wombat_observe_mic else _quiet_hours_gate_evaluate
        ),
        presence_provider=presence_provider,
        absorb_feedback=absorb_feedback,
        stamp_resolution=stamp_resolution,
    )
    review_or_speak_stage = ReviewOrSpeakStage(queue=queue)

    # TK-177 (EP-18, Q-92): the outbound Gmail-reply wiring — gated on the SAME Q-67 presence
    # checks sources/bootstrap.py's own gmail construction uses (client creds + a stored token),
    # decided ONCE here so the capability registration below and the DRAFT route/dispatch-edge
    # additions to the drain graph are an all-or-nothing pair (the LOUD-SKIP contract, TK-16
    # pattern): a Google-less/capability-less boot registers neither, and the drain graph stays
    # BYTE-IDENTICAL to the pre-TK-177 5-stage construction.
    capability_registry = Registry()
    composer_by_kind = {ItemKind.GENERIC: "compose"}
    # TK-222 (EP-32, Q-110(d) ruling 2): chat rides the EXISTING generic mouth — no new
    # composer/pathway. Wired UNCONDITIONALLY (harmless even when the chat surface itself is
    # never enabled below: no ItemKind.CHAT item can originate without it).
    composer_by_kind[ItemKind.CHAT] = "compose"
    # TK-114 (EP-22, Q-102b-f): the reflection-render leg is UNCONDITIONAL (mirrors dream_pattern's
    # own posture below) — no external deps beyond the psychology KB, loaded once here and wrapped
    # the SAME CON-3 default as TK-113's own load further down: a load failure never fails the
    # whole boot, ReflectionComposeStage just boots with an empty kb (safe default prompt, no
    # phrasing hints).
    try:
        reflection_kb = load_psychology_kb()
    except (FileNotFoundError, KBValidationError):
        logger.warning(
            "assemble_runtime: load_psychology_kb failed; ReflectionComposeStage boots with an "
            "empty KB (safe default prompt, no phrasing hints) rather than failing the whole boot",
            exc_info=True,
        )
        reflection_kb = []
    reflection_compose_stage = ReflectionComposeStage(
        kb=reflection_kb, live_persona=live_persona, timeout_seconds=op.mouth_model_timeout_seconds
    )
    composer_by_kind[ItemKind.REFLECTION] = "reflection_compose"
    draft_composer_stage: DraftComposer | None = None
    action_trail_writer: ActionTrailWriter | None = None
    if _has_google_client_credentials(config):
        gmail_store = (
            gmail_token_store
            if gmail_token_store is not None
            else GmailKeyringTokenStore(account=GMAIL_KEYRING_ACCOUNT)
        )
        if gmail_store.load() is None:
            logger.warning(
                "assemble_runtime: gmail outbound wiring not wired (drafts.create capability + "
                "DRAFT route/dispatch-edge skipped together): no stored Gmail credential — run "
                "`python -m wombat.integrations.gmail.auth` once to grant consent, then restart"
            )
        else:
            # TK-253 (DEC-49, CRF-6 precedent): a stored-but-expired/revoked credential must
            # degrade exactly like the no-stored-credential branch above, not crash boot —
            # RefreshError/transport/JSON-decode/scope failures on a bad stored credential are
            # one operational class. This does NOT wrap the interactive consent flow itself
            # (get_credentials/gmail/auth.py) — only the eager-refresh session factory call.
            try:
                gmail_session = make_gmail_session(config, token_store=gmail_store)
            except Exception:
                logger.warning(
                    "assemble_runtime: gmail outbound wiring not wired (drafts.create capability + "
                    "DRAFT route/dispatch-edge skipped together): stored Gmail credential failed "
                    "to refresh — run `python -m wombat.integrations.gmail.auth` once to "
                    "re-consent, then restart",
                    exc_info=True,
                )
            else:
                capability_registry.register(make_drafts_create_capability(gmail_session))
                # ActionTrailWriter (TK-146) has no other boot composition site (TK-177) — ONE
                # instance over this SAME dsn, shared by draft_composer and draft_dispatch below,
                # and exposed on RuntimeBundle.action_trail_writer (TK-184) so runtime's teardown
                # can close it.
                action_trail_writer = ActionTrailWriter(dsn)
                draft_composer_stage = build_draft_composer_stage(
                    writer=action_trail_writer,
                    clock=_utc_now,
                    assistant_name=config.wombat_assistant_name,
                    live_persona=live_persona,
                    timeout_seconds=op.mouth_model_timeout_seconds,
                )
                composer_by_kind[ItemKind.DRAFT] = "draft_composer"
    else:
        logger.warning(
            "assemble_runtime: gmail outbound wiring not wired (drafts.create capability + "
            "DRAFT route/dispatch-edge skipped together): GOOGLE_OAUTH_CLIENT_ID/"
            "GOOGLE_OAUTH_CLIENT_SECRET not configured (boot continues Google-less)"
        )

    compose_dispatch_router = ComposeDispatchRouter(composer_by_kind=composer_by_kind)
    # TK-173 (CR-15): share the ONE DailyLedger constructed above (the ceiling/day-rollover
    # instance) rather than letting build_compose_stage open a second connection on the same
    # dsn — runtime.py's teardown only ever closed bundle.daily_ledger, so a second instance
    # would leak its lazily-opened connection past process shutdown.
    compose_stage = build_compose_stage(
        config=config,
        dsn=dsn,
        params=op,
        tz=tz,
        daily_ledger=daily_ledger,
        live_persona=live_persona,
    )
    # TK-164 (Q-96): the new drain-graph terminal — compose now transitions onward (TK-222: via
    # chat_reply, see below) instead of ending the spine itself; ONE SpeakSink instance is
    # appended to BOTH graph variants below (draft_dispatch stays its own separate terminal,
    # untouched).
    # TK-267 (DEC-55): the TTS adapter is built ONCE here (inlining what build_speak_sink itself
    # does) so speech_shape_stage's adapter-presence gate reads the SAME adapter SpeakSink speaks
    # through, rather than constructing (and possibly loud-logging) a second one.
    tts_adapter = build_tts_adapter(config) if config.wombat_voice_enabled else None
    # TK-288 (DEC-64 gap A, v2.151 ruling): ONE shared LastSpokenRegister, threaded via its
    # note_spoken bound method into BOTH speak sites below (this drain-graph SpeakSink AND the
    # brief pathway's BriefDeliverStage further down) — never two registers, never build_speak_
    # sink (zero src callers, stays byte-untouched).
    # TK-303 (DEC-67e): ttl_seconds carries the DEC-64 reply window, restart-tier from the
    # settings table/env (no hot-apply).
    last_spoken_register = LastSpokenRegister(
        clock=_epoch_now, ttl_seconds=config.wombat_reply_window_seconds
    )
    speak_stage = SpeakSink(
        voice_enabled=config.wombat_voice_enabled,
        adapter=tts_adapter,
        on_spoken=last_spoken_register.note_spoken,
    )
    speech_shape_stage = build_speech_shape_stage(
        config=config,
        dsn=dsn,
        params=op,
        tz=tz,
        daily_ledger=daily_ledger,
        adapter_present=tts_adapter is not None,
    )

    # TK-222 (EP-32, Q-110(d) ruling 5): the chat input surface — enabled IFF
    # config.wombat_chat_handshake_file is non-blank (loud-skip parity with sources.bootstrap's
    # own _maybe_register_* pattern). chat_reply_stage is built UNCONDITIONALLY either way —
    # EVERY compose-composed item hops through it now (ruling 3) — but its broker is None on a
    # chat-disabled boot, making it a pure pass-through (chat_source/chat_surface stay None too,
    # so nothing is registered into source_registry below).
    chat_source: ChatSource | None = None
    chat_reply_broker: ChatReplyBroker | None = None
    chat_surface: ChatSurface | None = None
    raw_chat_handshake_path = (config.wombat_chat_handshake_file or "").strip()
    if not raw_chat_handshake_path:
        logger.warning(
            "assemble_runtime: WOMBAT_CHAT_HANDSHAKE_FILE not configured — skipping the chat "
            "input surface (boot continues without it)"
        )
    else:
        chat_source = ChatSource()
        chat_reply_broker = ChatReplyBroker()
        chat_surface = ChatSurface(
            source=chat_source,
            broker=chat_reply_broker,
            token=secrets.token_urlsafe(32),
            handshake_path=Path(raw_chat_handshake_path),
        )
    chat_reply_stage = ChatReplyStage(broker=chat_reply_broker)

    # TK-280 (DEC-60c server half, EP-32): the ASR turn_hook seam — None when chat is disabled
    # (chat_reply_broker is None), otherwise a closure that derives item_id via the SAME
    # idempotency_key('asr', event_key) derivation sources.registry.SourceRegistry uses at
    # enqueue time (ASRSource.id == "asr") and registers the turn into the broker's ledger.
    asr_turn_hook: Callable[[str, str, str], None] | None = None
    if chat_reply_broker is not None:
        _voice_turn_broker = chat_reply_broker

        def asr_turn_hook(event_key: str, transcript: str, captured_at: str) -> None:
            item_id = idempotency_key("asr", event_key)
            _voice_turn_broker.register_voice_turn(item_id, transcript, captured_at)

    # TK-245 (ruling v2.68 r5): ALWAYS constructed (dsn is a required str here; the store is
    # fully lazy — no connection at construction), regardless of replay_pending — this is the
    # source-poll sink target, never a runtime boot mode of its own. Constructed HERE (rather than
    # alongside scratchpad_store further down) so TK-290's asr_context_hook closure below has it
    # in scope.
    external_item_store = ExternalItemStore(dsn)
    # TK-289 (DEC-64 gap A, half 2): the ASR context_hook seam — reads the SAME shared
    # last_spoken_register above (unconditional; not gated on chat, unlike asr_turn_hook). Fresh
    # (within-TTL) spoken text stamps {"replying_to": text}; stale/None yields no key (key ABSENT,
    # never an empty string) — ASRSource itself enforces the reserved-key merge order.
    #
    # TK-290 (DEC-64 gap B): merged into this SAME closure — build_voice_context reads the SAME
    # external_item_store constructed above, over the SAME configured tz, at call time (a fresh
    # today-window/recent-gmail read every poll, never memoized). A None/raising store degrades to
    # {} plus its own single warning (CON-3); this hook still returns whatever replying_to gave.
    #
    # TK-296 (DEC-65f): ALSO merged into this SAME closure — build_user_facts_context reads
    # user_facts_store, constructed further below (TK-297 ruling r2 hoisted it above the
    # dream-stage block, still below this closure's own definition). Safe despite the source-order
    # forward reference: Python closures resolve free variables at CALL time, and this closure is
    # only ever called (by ASRSource.poll()/ChatSurface._accept_message, both well after
    # assemble_runtime returns) once user_facts_store has been assigned.
    #
    # TK-311 (DEC-68(d)(1)): ALSO merged into this SAME closure — build_current_activity_context
    # reads current_activity, constructed further below (in the wombat_observe_screen-gated block
    # alongside screen_collector) — the SAME forward-reference safety as user_facts_store just
    # above applies (closures resolve free variables at call time, well after assemble_runtime
    # returns and current_activity has been assigned, whether to a live CurrentActivity or left
    # None on a toggle-off boot).
    def asr_context_hook() -> dict[str, str]:
        text = last_spoken_register.current()
        extra: dict[str, str] = {} if text is None else {"replying_to": text}
        extra.update(build_voice_context(external_item_store, tz=tz, clock=_utc_now))
        extra.update(build_user_facts_context(user_facts_store))
        extra.update(build_current_activity_context(current_activity))
        return extra

    if chat_source is not None:
        # TK-296 (DEC-65f, RULING r3 v2.159): the SAME shared closure, wired into ChatSource's
        # PUBLIC context_hook attribute — typed chat turns now get the identical known_user_
        # context/replying_to/calendar/gmail grounding voice turns already get. Post-construction
        # assignment (ChatSource was already constructed above, before this closure existed) —
        # mirrors the wake attribute's own late-wire pattern (set by a different caller, runtime.py
        # there; here it is simply defined later in this same function).
        chat_source.context_hook = asr_context_hook

    if draft_composer_stage is not None:
        # TK-177: the draft-item leg — compose_dispatch (DRAFT) -> draft_composer -> draft_dispatch.
        # TK-179/Q-94: DraftDispatchStage locates the parked draft_composer step BY STAGE IDENTITY
        # at run time (ctx.journal.load_run + reverse-walk for stage_name == "draft_composer") —
        # NOT a precomputed graph position, which goes stale the moment the standing drain run
        # idles on even one Sweeper poll before the draft item surfaces. No index to compute here.
        pre_dispatch_stages = (
            drain_queue_stage,
            gate_stage,
            review_or_speak_stage,
            compose_dispatch_router,
            draft_composer_stage,
        )
        assert action_trail_writer is not None, (
            "assemble_runtime: draft_composer_stage is only ever set alongside "
            "action_trail_writer (both assigned in the SAME wired branch above)"
        )
        draft_dispatch_stage = DraftDispatchStage(writer=action_trail_writer)
        graph = build_drain_pathway(
            *pre_dispatch_stages,
            compose_stage,
            chat_reply_stage,
            speech_shape_stage,
            speak_stage,
            reflection_compose_stage,
            draft_dispatch_stage,
        )
    else:
        graph = build_drain_pathway(
            drain_queue_stage,
            gate_stage,
            review_or_speak_stage,
            compose_dispatch_router,
            compose_stage,
            chat_reply_stage,
            speech_shape_stage,
            speak_stage,
            reflection_compose_stage,
        )
    substrate.pathways.register(DRAIN_PATHWAY_ID, graph)

    # TK-296 (DEC-65f): the durable what-wombat-knows-about-the-user store — ALWAYS constructed
    # (dsn is a required str here; UserFactsStore is fully lazy — no connection at construction).
    # HOISTED here (TK-297 ruling r2) — above the dream-stage block, since DreamFactsStage
    # (constructed below, beside dream_persona_stage) needs it in scope too. Also read by
    # asr_context_hook's closure (already defined above it in this function's source — see that
    # closure's own comment for why the forward reference is safe); the move is behavior-neutral
    # (Q-46 fully-lazy, zero I/O at construction).
    user_facts_store = UserFactsStore(dsn)
    # TK-295 (DEC-65e): the 7-day rolling chat/voice-turn ledger — ALWAYS constructed (dsn is a
    # required str here; fully lazy, mirrors user_facts_store immediately above). HOISTED here
    # (TK-297 ruling r2) for the SAME reason — DreamFactsStage needs it in scope beside
    # dream_persona_stage. Also threaded into build_source_registry below so the SourceRegistry
    # sink tap records the user's own utterances; purged once at boot by runtime.serve() (the
    # scratchpad_store precedent).
    chat_turn_store = ChatTurnStore(dsn)

    # TK-46/TK-175/TK-47 (Q-85/Q-90): register wombat.dream UNCONDITIONALLY — both
    # DreamOutcomeStage's entity-KG reads and DreamConsolidationStage's sweepers are as harmless
    # on a Google-less/sink-less boot as the terminal scaffold was (no external deps beyond the
    # SAME shared entity_kg constructed above + the SAME deepseek descriptor _deepseek_registry
    # registers).
    dream_spec = _deepseek_spec(config)
    dream_substrate = build_dream_substrate(entity_kg=entity_kg, spec=dream_spec, params=op, tz=tz)
    dream_reconciler = CoherenceReconciler(
        entity_kg=entity_kg, store=dream_substrate.store, oracle=dream_substrate.oracle
    )
    dream_extractor = ClaimExtractor(
        journal=substrate.journal,
        entity_kg=entity_kg,
        model=dream_substrate.model,
        source_registry=dream_substrate.source_registry,
    )
    dream_consolidation_stage = DreamConsolidationStage(
        reconciler=dream_reconciler, extractor=dream_extractor
    )
    dream_outcome_stage = DreamOutcomeStage(
        entity_kg=entity_kg, labeler=outcome_labeler, user_id=_RUNTIME_USER_ID
    )
    # TK-49 (Q-91, EP-14): RatingTuner composed over the SAME shared user-scope entity_kg/
    # observation_writer trio TK-176 built above, plus the SAME loaded OperatingParams — the gate
    # never gets a second writer/reader onto this scope.
    rating_tuner = RatingTuner(
        entity_kg=entity_kg,
        writer=observation_writer,
        params=op,
        user_id=_RUNTIME_USER_ID,
        clock=_utc_now,
    )
    dream_tune_stage = DreamTuneStage(tuner=rating_tuner)
    # TK-111 (Q-98): BehaviorEventLog over the SAME runtime dsn every other Postgres seam here
    # uses, and DreamBehaviorLogStage over the SAME shared entity_kg/_RUNTIME_USER_ID (never a
    # second KG instance). UNCONDITIONAL (mirrors dream_pathway_id's own posture) — no external
    # deps beyond what this composition already builds.
    behavior_event_log = BehaviorEventLog(dsn)
    dream_behavior_log_stage = DreamBehaviorLogStage(
        store=behavior_event_log, entity_kg=entity_kg, user_id=_RUNTIME_USER_ID
    )
    # TK-214 (EP-35): DreamPersonaStage over the SAME behavior_event_log instance above (never a
    # second BehaviorEventLog/connection) and the SAME live_persona runtime authority every mouth
    # call site reads. UNCONDITIONAL (mirrors dream_behavior_log_stage's own posture) — no
    # external deps beyond what this composition already builds.
    dream_persona_stage = DreamPersonaStage(event_log=behavior_event_log, live_persona=live_persona)
    # TK-297 (EP-13, DEC-65g): DreamFactsStage over the SAME budget-guarded dream_substrate.model
    # every other dream-consolidation call site uses (never a second model/guard) and the
    # user_facts_store/chat_turn_store hoisted above (never a second connection to either table).
    # UNCONDITIONAL (mirrors dream_persona_stage's own posture) — no external deps beyond what
    # this composition already builds.
    dream_facts_stage = DreamFactsStage(
        model=dream_substrate.model,
        chat_turns=chat_turn_store,
        user_facts=user_facts_store,
    )
    # TK-299 (EP-37, DEC-66): DreamDeriveStage over the SAME external_item_store constructed
    # above (never a second connection) and the SAME user_facts_store dream_facts_stage just used
    # (never a second UserFactsStore instance). UNCONDITIONAL (mirrors dream_facts_stage's own
    # posture) — no external deps beyond what this composition already builds; pure code, no
    # model.
    dream_derive_stage = DreamDeriveStage(
        external_items=external_item_store,
        user_facts=user_facts_store,
    )

    def _record_persona_feedback(
        token: FeedbackToken, event_key: str, timestamp: datetime
    ) -> None:
        """TK-213 (Q-112(a)): the bootstrap-owned SECOND sanctioned writer into
        ``wombat_behavior_events`` — writes through the SAME ``behavior_event_log`` instance
        above, never a second connection. See the module docstring for the full row-encoding
        ruling."""
        behavior_event_log.upsert(
            idempotency_key=idempotency_key("persona_feedback", event_key),
            event_type="persona_feedback",
            source_id="asr",
            timestamp_utc=timestamp,
            outcome_label=token.phrase,
            duration_seconds=None,
        )
    # TK-112 (Q-99e): WriteWindowSummariesStage over the SAME shared behavior_event_log/
    # observation_writer instances built above (never a second instance of either) and the SAME
    # configured tz. UNCONDITIONAL (mirrors dream_behavior_log_stage's own posture) — no external
    # deps beyond what this composition already builds.
    dream_window_stage = WriteWindowSummariesStage(
        store=behavior_event_log, writer=observation_writer, tz=tz
    )
    # TK-113 (Q-99b/f/g): the psychology KB is loaded ONCE here at boot and injected into
    # PatternDetectorStage — the stage itself never loads the KB. A load failure (TK-115 AC4:
    # FileNotFoundError or ValidationError) is caught, logged LOUD, and falls back to an empty
    # KB — a safe no-nudge default (pattern_warrants_nudge over an empty kb always returns False)
    # rather than failing the whole boot over a KB problem.
    try:
        psychology_kb = load_psychology_kb()
    except (KBValidationError, FileNotFoundError):
        logger.error(
            "assemble_runtime: load_psychology_kb failed; PatternDetectorStage boots with an "
            "empty KB (no pattern will ever match tonight) rather than failing the whole boot",
            exc_info=True,
        )
        psychology_kb = []
    # PatternDetectorStage over the SAME shared entity_kg/_RUNTIME_USER_ID/tz trio every other
    # dream stage here uses, and the ONE shared WombatQueue's bound enqueue (ASMP-2 custody —
    # never a second queue/connection).
    dream_pattern_stage = PatternDetectorStage(
        entity_kg=entity_kg,
        kb=psychology_kb,
        enqueue=queue.enqueue,
        user_id=_RUNTIME_USER_ID,
        tz=tz,
    )
    dream_graph = build_dream_pathway(
        dream_consolidation_stage,
        dream_outcome_stage,
        dream_tune_stage,
        dream_persona_stage,
        dream_facts_stage,
        dream_derive_stage,
        dream_behavior_log_stage,
        dream_window_stage,
        dream_pattern_stage,
    )
    substrate.pathways.register(DREAM_PATHWAY_ID, dream_graph)

    # TK-96: register wombat.brief off the SAME composed Gate/substrate/dsn — CONDITIONAL on a
    # non-blank brief sink path (mirrors build_brief_deliver_stage's own fail-loud-at-construction
    # posture, but decided HERE so a Google-less/sink-less boot still starts rather than raising).
    raw_brief_path = config.wombat_brief_path
    brief_pathway_id: str | None = None
    # TK-212 (EP-34): built ONCE and shared between the brief-deliver stage below and the
    # source-registry ASR persona-command-hook seam further down — never a second/third TTS
    # adapter construction (Q-96's "ONE adapter, N delivery points").
    speak = make_speak_callable(config)
    if raw_brief_path is None or not raw_brief_path.strip():
        logger.warning(
            "assemble_runtime: WOMBAT_BRIEF_PATH is missing/blank; skipping wombat.brief "
            "pathway registration (the drain spine still boots without a brief sink)"
        )
    else:
        triage_rules = load_triage_rules()
        brief_fetches = build_brief_fetches(
            config,
            tz=tz,
            gcal_token_store=gcal_token_store,
            gmail_token_store=gmail_token_store,
        )
        brief_gather_stage = BriefGatherStage(
            fetch_calendar=brief_fetches.fetch_calendar,
            fetch_gmail=brief_fetches.fetch_gmail,
            triage_rules=triage_rules,
            clock=_utc_now,
        )
        # SAME composed gate as the drain pathway (never a second Gate/pending-set/ceiling).
        brief_force_flush_stage = BriefForceFlushStage(select_items=gate.select_items, tz=tz)
        # TK-173 (CR-15): the SAME shared DailyLedger instance, not a third connection.
        brief_compose_stage = build_brief_compose_stage(
            config=config,
            dsn=dsn,
            params=op,
            tz=tz,
            daily_ledger=daily_ledger,
            live_persona=live_persona,
        )
        # TK-164 (Q-96): bind the SAME TTS adapter TYPE into the brief's already-built injected
        # speak seam (TK-101) — discharges the "TK-164 binds real TTS into THIS seam" promise
        # (Q-78). None unless voice_enabled AND the adapter constructs; a voice-off/lib-less boot
        # stays byte-identical (seam None, text-only delivery).
        # TK-288 (DEC-64 gap A): the SAME shared last_spoken_register as the drain pathway's
        # speak_stage above — one register, fed from both speak sites.
        brief_deliver_stage = build_brief_deliver_stage(
            config=config, tz=tz, speak=speak, on_spoken=last_spoken_register.note_spoken
        )
        brief_graph = build_brief_pathway(
            brief_gather_stage, brief_force_flush_stage, brief_compose_stage, brief_deliver_stage
        )
        substrate.pathways.register(BRIEF_PATHWAY_ID, brief_graph)
        brief_pathway_id = BRIEF_PATHWAY_ID

    engine = build_engine(
        substrate, config=config, params=op, capability_registry=capability_registry
    )
    # TK-247 (ruling v2.68 r5): ALWAYS constructed (dsn is a required str here; the store is
    # fully lazy — no connection at construction), mirroring external_item_store above (TK-245,
    # now constructed earlier — TK-290 needs it in scope for asr_context_hook's closure).
    scratchpad_store = ScratchpadStore(dsn)
    # TK-286 (DEC-63a): the persisted exactly-once seam every source's enqueue shares — wraps the
    # SAME shared queue instance so a source item, once successfully enqueued, never re-enters the
    # queue on a later poll with an unchanged payload (closes the live repeat-flush defect:
    # wombat_queue's own ON CONFLICT dedup only holds while the row is LIVE, and ack() DELETEs it).
    # Threaded ONLY into build_source_registry below — PatternDetectorStage above KEEPS the raw
    # queue.enqueue (an internally-derived nightly pattern event, not a re-polled source item).
    seen_ledger = SeenLedger(dsn)
    deduping_enqueue = DedupingEnqueuer(queue, seen_ledger)
    source_registry = build_source_registry(
        config,
        deduping_enqueue,
        tz=tz,
        gcal_token_store=gcal_token_store,
        gmail_token_store=gmail_token_store,
        live_persona=live_persona,
        speak=speak,
        persona_feedback_recorder=_record_persona_feedback,
        external_item_store=external_item_store,
        chat_turn_store=chat_turn_store,
        turn_hook=asr_turn_hook,
        context_hook=asr_context_hook,
    )
    if chat_source is not None:
        # TK-222 (Q-110(d) ruling 1): registered exactly like every other source — the registry
        # never learns chat is push-backed/HTTP-fed (registration-not-rewrite, DEC-5).
        source_registry.register(chat_source)

    # TK-97 (Q-80): register wombat.brief_schedule — the once-daily brief timer — inside the SAME
    # brief-path conditional (blank brief path already loud-skipped BOTH above, leaving
    # brief_pathway_id None here). Built AFTER build_engine so the fire_brief closure can capture
    # the live Engine. fire_brief drives wombat.brief under a DAY-KEYED run_id: same wombat-day ->
    # same run_id -> the TK-101 file-marker makes a re-fire a replay=True no-op (AC4).
    brief_schedule_pathway_id: str | None = None
    if brief_pathway_id is not None:

        async def fire_brief(now: datetime) -> RunState:
            run_id = f"wombat-brief-{wombat_today(now, tz).isoformat()}"
            return await engine.run(
                run_id=run_id,
                session_id=run_id,
                pathway_id=BRIEF_PATHWAY_ID,
                initial=brief_trigger_artifact(now),
            )

        # The exactly-once fence rides the SAME DailyLedger instance as the ceiling/spend ledgers
        # (a distinct "brief:run" row — no collision), so the runtime shares ONE row lifecycle.
        brief_run_ledger = BriefRunLedger(daily_ledger)
        brief_timer_stage = BriefTimerStage(
            fire_brief=fire_brief,
            ran_today=brief_run_ledger.ran_today,
            mark_ran=brief_run_ledger.mark_ran,
            tz=tz,
            brief_time=op.morning_brief_time,
        )
        schedule_graph = build_brief_schedule_pathway(brief_timer_stage)
        substrate.pathways.register(BRIEF_SCHEDULE_PATHWAY_ID, schedule_graph)
        brief_schedule_pathway_id = BRIEF_SCHEDULE_PATHWAY_ID

    # TK-52 (Q-85): register wombat.dream_schedule — the once-nightly dream timer — mirroring
    # TK-97's fire_brief wiring VERBATIM. UNCONDITIONAL (unlike wombat.brief_schedule above): the
    # dream scaffold has no external deps, so this always registers regardless of the brief path.
    # Built AFTER build_engine so fire_dream can capture the live Engine. fire_dream drives
    # wombat.dream under a NIGHT-KEYED run_id: same wombat-night -> same run_id -> the Engine's
    # own run_id double-drive guard (verified as-built at TK-53) is the second idempotency layer.
    async def fire_dream(now: datetime) -> RunState:
        run_id = f"wombat-dream-{wombat_today(now, tz).isoformat()}"
        return await engine.run(
            run_id=run_id,
            session_id=run_id,
            pathway_id=DREAM_PATHWAY_ID,
            initial=dream_trigger_artifact(now),
        )

    # The exactly-once fence rides the SAME DailyLedger instance as the ceiling/spend/brief-run
    # ledgers (a distinct "dream:run" row — no collision), so the runtime shares ONE row lifecycle.
    dream_run_ledger = DreamRunLedger(daily_ledger)
    dream_timer_stage = DreamTimerStage(
        fire_dream=fire_dream,
        ran_tonight=dream_run_ledger.ran_tonight,
        mark_ran=dream_run_ledger.mark_ran,
        tz=tz,
        dream_time=op.nightly_dream_time,
    )
    dream_schedule_graph = build_dream_schedule_pathway(dream_timer_stage)
    substrate.pathways.register(DREAM_SCHEDULE_PATHWAY_ID, dream_schedule_graph)
    dream_schedule_pathway_id: str | None = DREAM_SCHEDULE_PATHWAY_ID

    # TK-310 (DEC-68(a)/(c)): the ambient-observability screen channel — constructed ONLY when
    # config.wombat_observe_screen is true (structural inertness: toggle off leaves all three
    # None, so no store/writes/polling ever happen). dsn is a required str here; ObservationStore
    # is fully lazy (Q-46) — zero connection at construction either way.
    #
    # TK-313 (DEC-68(a)/(e)): observation_store/current_activity are now shared with the mic
    # channel — constructed when EITHER toggle is true, since CurrentActivity is ONE single-slot
    # snapshot object (app/title/in_call together) that both channels write into in place. Either
    # toggle alone still leaves screen_collector/mic_probe themselves gated on their OWN flag.
    observation_store: ObservationStore | None = None
    current_activity: CurrentActivity | None = None
    screen_collector: ScreenActivityCollector | None = None
    mic_probe: MicInCallProbe | None = None
    if config.wombat_observe_screen or config.wombat_observe_mic:
        observation_store = ObservationStore(dsn)
        current_activity = CurrentActivity()
    if config.wombat_observe_screen:
        assert observation_store is not None and current_activity is not None, (
            "assemble_runtime: wombat_observe_screen true must have constructed the shared "
            "observation_store/current_activity pair above"
        )
        screen_collector = ScreenActivityCollector(
            store=observation_store,
            current_activity=current_activity,
            tz=tz,
            clock=_utc_now,
        )
    if config.wombat_observe_mic:
        assert observation_store is not None and current_activity is not None, (
            "assemble_runtime: wombat_observe_mic true must have constructed the shared "
            "observation_store/current_activity pair above"
        )
        mic_probe = MicInCallProbe(
            store=observation_store,
            current_activity=current_activity,
            tz=tz,
            clock=_utc_now,
        )

    return RuntimeBundle(
        engine=engine,
        pathways=substrate.pathways,
        journal=substrate.journal,
        drain_pathway_id=DRAIN_PATHWAY_ID,
        dream_pathway_id=DREAM_PATHWAY_ID,
        dream_schedule_pathway_id=dream_schedule_pathway_id,
        source_registry=source_registry,
        pending_journal=pending_journal,
        queue=queue,
        daily_ledger=daily_ledger,
        compose_stage=compose_stage,
        live_persona=live_persona,
        brief_pathway_id=brief_pathway_id,
        brief_schedule_pathway_id=brief_schedule_pathway_id,
        entity_kg=entity_kg,
        observation_writer=observation_writer,
        behavior_event_log=behavior_event_log,
        action_trail_writer=action_trail_writer,
        chat_surface=chat_surface,
        chat_source=chat_source,
        external_item_store=external_item_store,
        scratchpad_store=scratchpad_store,
        chat_turn_store=chat_turn_store,
        observation_store=observation_store,
        current_activity=current_activity,
        screen_collector=screen_collector,
        mic_probe=mic_probe,
    )
