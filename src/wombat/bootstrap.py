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
transitions onward to ``DreamScaffoldStage`` (still the reachable terminal). No gate/pipeline
change here — the gate re-reads a tuned parameter on its next drive via the as-built
``UserModel.ratings_for`` seam.
"""

from __future__ import annotations

import json
import logging
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

from .compose.templates import TemplateComposer
from .config import ConfigurationError, WombatConfig, load_config
from .cost.daily_spend_ledger import DailySpendLedger
from .domain.brief_schedule import BriefRunLedger
from .domain.daily_ledger import DailyLedger, wombat_today
from .gate.ceiling import CeilingLedger
from .gate.decay import DayRollover
from .gate.gate import gate_item_from_queue_item
from .gate.models import GateAction, GateDecision, ItemKind
from .gate.pending_journal_pg import PgPendingJournal
from .gate.pending_set import PendingSet
from .gate.pipeline import Gate
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
    DreamConsolidationStage,
    DreamOutcomeStage,
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
from .queue import QueueItem, WombatQueue
from .rating.rating_tuner import RatingTuner
from .sources.bootstrap import (
    _has_google_client_credentials,
    build_brief_fetches,
    build_source_registry,
)
from .sources.presence import make_presence_provider
from .sources.registry import SourceRegistry
from .stages.brief_compose_stage import BriefComposeStage
from .stages.brief_deliver_stage import BriefDeliverStage
from .stages.brief_force_flush_stage import BriefForceFlushStage
from .stages.brief_gather_stage import BriefGatherStage
from .stages.brief_timer_stage import BriefTimerStage
from .stages.compose import ComposeStage
from .stages.compose_dispatch_router import ComposeDispatchRouter
from .stages.draft_dispatch import DraftDispatchStage
from .stages.drain_queue import DrainQueueStage
from .stages.gate_stage import GateStage, make_gate_evaluator
from .stages.review_or_speak import ReviewOrSpeakStage
from .substrate import SubstrateBundle, build_substrate
from .trail.writer import ActionTrailWriter
from .user_model.claims import Claim, ClaimPredicate
from .user_model.feedback_source import FeedbackSignal
from .user_model.observation_writer import ObservationWriter
from .user_model.outcome_inference import ItemDisposition
from .user_model.outcome_labeler import OutcomeLabeler
from .user_model.user_model import UserModel

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

# Module-level singleton (ruff B008 — a ZoneInfo() call cannot live in a default arg). The
# DailyLedger day-boundary tz; demo_drain.py's own real-DailyLedger wiring uses the same UTC.
_UTC_ZONE = ZoneInfo("UTC")

_lock = threading.Lock()
_engine: Engine | None = None


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
    tz: ZoneInfo = _UTC_ZONE,
    daily_ledger: DailyLedger | None = None,
) -> ComposeStage:
    """Assemble the mouth's ``ComposeStage`` wired with TK-9 layer 2 (Q-68): a real
    ``DailySpendLedger`` (over a ``DailyLedger`` on ``dsn``) and the ``mouth_daily_token_ceiling``
    tunable from OperatingParams, so the pre-call ceiling gate and post-call token accounting are
    live — not the optional-and-disabled ``ComposeStage`` defaults.

    ``daily_ledger`` (TK-173, CR-15) lets a caller (``assemble_runtime``) hand in an ALREADY
    constructed ``DailyLedger`` so this stage shares that ONE instance/connection/close()
    lifecycle instead of opening a second one on the same ``dsn``. Defaults to constructing a
    fresh ``DailyLedger(dsn, tz=tz)`` for standalone callers (tests, ``scripts/demo_drain.py``).
    """
    op = params if params is not None else load_operating_params()
    ledger = daily_ledger if daily_ledger is not None else DailyLedger(dsn, tz=tz)
    spend_ledger = DailySpendLedger(ledger)
    return ComposeStage(
        config=config,
        template_composer=TemplateComposer(),
        spend_ledger=spend_ledger,
        daily_token_ceiling=op.mouth_daily_token_ceiling,
    )


def build_brief_compose_stage(
    *,
    config: WombatConfig,
    dsn: str,
    params: OperatingParams | None = None,
    tz: ZoneInfo = _UTC_ZONE,
    daily_ledger: DailyLedger | None = None,
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
    """
    op = params if params is not None else load_operating_params()
    ledger = daily_ledger if daily_ledger is not None else DailyLedger(dsn, tz=tz)
    spend_ledger = DailySpendLedger(ledger)
    return BriefComposeStage(
        config=config,
        tz=tz,
        spend_ledger=spend_ledger,
        daily_token_ceiling=op.mouth_daily_token_ceiling,
    )


def build_brief_deliver_stage(
    *,
    config: WombatConfig,
    tz: ZoneInfo = _UTC_ZONE,
    speak: Callable[[str], None] | None = None,
) -> BriefDeliverStage:
    """Assemble the morning brief's terminal ``BriefDeliverStage`` (TK-101, Q-78).

    Resolves the append-only text-sink path from ``config.wombat_brief_path``; a blank/absent
    path fails LOUD at construction (``ConfigurationError`` naming it) rather than wiring a
    stage that would raise on its first delivery. ``config.wombat_voice_enabled`` gates voice;
    ``speak`` is the injected voice sink (EP-30 narrowed) — passed through untouched, ``None`` by
    default so callers that never wire a voice provider get text-only delivery.
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
    )


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
) -> DraftComposer:
    """Assemble TK-78's ``DraftComposer`` via a small bootstrap factory (TK-177, the Q-69
    assemble-via-factory lesson) — a thin, directly-testable wrapper mirroring
    ``build_compose_stage``/``build_brief_compose_stage`` above, rather than ``assemble_runtime``
    constructing the stage inline. ``DraftComposer.__init__`` already self-binds the TK-151
    external tier policy (``bind_external_tier``) — nothing further to wire here.
    """
    return DraftComposer(writer=writer, clock=clock)


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


def assemble_runtime(
    *,
    config: WombatConfig,
    dsn: str,
    params: OperatingParams | None = None,
    tz: ZoneInfo = _UTC_ZONE,
    replay_pending: bool = True,
    gmail_token_store: GmailTokenStore | None = None,
) -> RuntimeBundle:
    """Compose the ONE standing wombat process (TK-53, Q-71).

    ``gmail_token_store`` (TK-177, EP-18) lets a caller/test override the real OS-keyring token
    store the outbound Gmail wiring (WIRE 2/3 below) reads its Q-67 presence check against —
    mirrors ``build_source_registry``'s own ``gmail_token_store`` seam; it is threaded into
    BOTH that call and the outbound wiring's own check below, so the source-side read wiring and
    the draft-side write wiring always agree on the SAME stored credential. Defaults to ``None``
    (the real ``GmailKeyringTokenStore``), behavior-preserving for every existing caller.

    Registers the TK-7 drain pathway (id ``DRAIN_PATHWAY_ID``) and wires the REAL production
    ``Gate`` (TK-27) — a durable ``PendingSet`` backed by the TK-29 Postgres ``PendingJournal``
    (the Q-70/RISK-5 boot obligation), a ``CeilingLedger`` over a real ``DailyLedger``, the real
    presence provider, and ``UserModel`` ratings — over the SAME ``build_engine``/
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
    """
    op = params if params is not None else load_operating_params()

    # The v1 cold-boot substrate (Q-36/TK-14): in-memory journal/graph/latent + a FRESH
    # PathwayRegistry. This EXACT registry is handed to build_engine below, so the pathway
    # registered on it here is what the Engine resolves at run/resume/fire_timer time.
    substrate = build_substrate()

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
    gate_stage = GateStage(
        evaluate=make_gate_evaluator(
            gate=gate,
            staleness_ceiling_s=op.presence_staleness_ceiling_seconds,
            confidence_floor=op.presence_confidence_floor,
            clock=_epoch_now,
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
            gmail_session = make_gmail_session(config, token_store=gmail_store)
            capability_registry.register(make_drafts_create_capability(gmail_session))
            # ActionTrailWriter (TK-146) has no other boot composition site yet (TK-177) — one
            # instance over this SAME dsn, shared by draft_composer and draft_dispatch below.
            action_trail_writer = ActionTrailWriter(dsn)
            draft_composer_stage = build_draft_composer_stage(
                writer=action_trail_writer, clock=_utc_now
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
        config=config, dsn=dsn, params=op, tz=tz, daily_ledger=daily_ledger
    )

    if draft_composer_stage is not None:
        # TK-177: the draft-item leg — compose_dispatch (DRAFT) -> draft_composer -> draft_dispatch.
        # ask_step_index is COMPUTED from this exact stages list (Q-92: graph-position-sensitive,
        # never hardcoded) — the positional index draft_composer lands at in a fresh single-item
        # drive is the step_index the engine records the human's approve/reject answer under.
        pre_dispatch_stages = (
            drain_queue_stage,
            gate_stage,
            review_or_speak_stage,
            compose_dispatch_router,
            draft_composer_stage,
        )
        draft_ask_step_index = pre_dispatch_stages.index(draft_composer_stage)
        assert action_trail_writer is not None, (
            "assemble_runtime: draft_composer_stage is only ever set alongside "
            "action_trail_writer (both assigned in the SAME wired branch above)"
        )
        draft_dispatch_stage = DraftDispatchStage(
            writer=action_trail_writer, ask_step_index=draft_ask_step_index
        )
        graph = build_drain_pathway(*pre_dispatch_stages, compose_stage, draft_dispatch_stage)
    else:
        graph = build_drain_pathway(
            drain_queue_stage,
            gate_stage,
            review_or_speak_stage,
            compose_dispatch_router,
            compose_stage,
        )
    substrate.pathways.register(DRAIN_PATHWAY_ID, graph)

    # TK-46/TK-175/TK-47 (Q-85/Q-90): register wombat.dream UNCONDITIONALLY — both
    # DreamOutcomeStage's entity-KG reads and DreamConsolidationStage's sweepers are as harmless
    # on a Google-less/sink-less boot as the terminal scaffold was (no external deps beyond the
    # SAME shared entity_kg constructed above + the SAME deepseek descriptor _deepseek_registry
    # registers).
    dream_spec = _deepseek_spec(config)
    dream_substrate = build_dream_substrate(entity_kg=entity_kg, spec=dream_spec, params=op)
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
    dream_graph = build_dream_pathway(
        dream_consolidation_stage, dream_outcome_stage, dream_tune_stage
    )
    substrate.pathways.register(DREAM_PATHWAY_ID, dream_graph)

    # TK-96: register wombat.brief off the SAME composed Gate/substrate/dsn — CONDITIONAL on a
    # non-blank brief sink path (mirrors build_brief_deliver_stage's own fail-loud-at-construction
    # posture, but decided HERE so a Google-less/sink-less boot still starts rather than raising).
    raw_brief_path = config.wombat_brief_path
    brief_pathway_id: str | None = None
    if raw_brief_path is None or not raw_brief_path.strip():
        logger.warning(
            "assemble_runtime: WOMBAT_BRIEF_PATH is missing/blank; skipping wombat.brief "
            "pathway registration (the drain spine still boots without a brief sink)"
        )
    else:
        triage_rules = load_triage_rules()
        brief_fetches = build_brief_fetches(config, tz=tz)
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
            config=config, dsn=dsn, params=op, tz=tz, daily_ledger=daily_ledger
        )
        brief_deliver_stage = build_brief_deliver_stage(config=config, tz=tz)
        brief_graph = build_brief_pathway(
            brief_gather_stage, brief_force_flush_stage, brief_compose_stage, brief_deliver_stage
        )
        substrate.pathways.register(BRIEF_PATHWAY_ID, brief_graph)
        brief_pathway_id = BRIEF_PATHWAY_ID

    engine = build_engine(
        substrate, config=config, params=op, capability_registry=capability_registry
    )
    source_registry = build_source_registry(
        config, queue, tz=tz, gmail_token_store=gmail_token_store
    )

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
        brief_pathway_id=brief_pathway_id,
        brief_schedule_pathway_id=brief_schedule_pathway_id,
        entity_kg=entity_kg,
        observation_writer=observation_writer,
    )
