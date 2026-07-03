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
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from cogworx.capability.registry import Registry
from cogworx.context.personality import PersonalityProfile
from cogworx.context.rules import RuleSet
from cogworx.cost.budget import BudgetPolicy
from cogworx.loop.pathway import PathwayRegistry
from cogworx.model.base import ModelCapabilities
from cogworx.model.providers.config import ProviderConfig
from cogworx.model.registry import ModelRegistry, ModelSpec
from cogworx.recall.stack import RecallStack
from cogworx.runtime.engine import Engine
from cogworx.substrate.journal import Journal
from cogworx.testing.doubles import InMemoryEntityKG

from .compose.templates import TemplateComposer
from .config import WombatConfig, load_config
from .cost.daily_spend_ledger import DailySpendLedger
from .domain.daily_ledger import DailyLedger
from .gate.ceiling import CeilingLedger
from .gate.decay import DayRollover
from .gate.models import ItemKind
from .gate.pending_journal_pg import PgPendingJournal
from .gate.pending_set import PendingSet
from .gate.pipeline import Gate
from .params import OperatingParams, load_operating_params
from .pathways.drain_pathway import build_drain_pathway
from .queue import WombatQueue
from .sources.bootstrap import build_source_registry
from .sources.presence import make_presence_provider
from .sources.registry import SourceRegistry
from .stages.brief_compose_stage import BriefComposeStage
from .stages.compose import ComposeStage
from .stages.compose_dispatch_router import ComposeDispatchRouter
from .stages.drain_queue import DrainQueueStage
from .stages.gate_stage import GateStage, make_gate_evaluator
from .stages.review_or_speak import ReviewOrSpeakStage
from .substrate import SubstrateBundle, build_substrate
from .user_model.user_model import UserModel

MODEL_PROFILE = "deepseek"

# TK-53 (Q-71) composition-root constants for the standing runtime.
DRAIN_PATHWAY_ID = "wombat.drain"
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


def _deepseek_registry(config: WombatConfig) -> ModelRegistry:
    """Register the DeepSeek profile as a ModelSpec descriptor (no client built here)."""
    registry = ModelRegistry()
    spec = ModelSpec(
        provider="openai_compat",  # DeepSeek speaks the OpenAI-compatible protocol
        config=ProviderConfig(
            api_key=config.deepseek_api_key,
            base_url=config.deepseek_base_url,
            model_pro="deepseek-chat",
            model_flash="deepseek-chat",
        ),
        # capabilities is REQUIRED for a non-None base_url (DeepSeek endpoint).
        capabilities=ModelCapabilities(structured_output=True, streaming=True, tools=True),
    )
    registry.register_spec(MODEL_PROFILE, spec)
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
) -> Engine:
    """Assemble (once) and return the wombat Engine. Idempotent: a second call returns the same
    instance — never a silent duplicate (AC3). ``bundle``/``config``/``params`` default to the
    cold-boot substrate, env config, and the packaged ``wombat_params.yaml`` respectively."""
    global _engine
    with _lock:
        if _engine is None:
            cfg = config if config is not None else load_config()
            sub = bundle if bundle is not None else build_substrate()
            op = params if params is not None else load_operating_params()
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
                registry=Registry(),
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
) -> ComposeStage:
    """Assemble the mouth's ``ComposeStage`` wired with TK-9 layer 2 (Q-68): a real
    ``DailySpendLedger`` (over a ``DailyLedger`` on ``dsn``) and the ``mouth_daily_token_ceiling``
    tunable from OperatingParams, so the pre-call ceiling gate and post-call token accounting are
    live — not the optional-and-disabled ``ComposeStage`` defaults.
    """
    op = params if params is not None else load_operating_params()
    daily_ledger = DailyLedger(dsn, tz=tz)
    spend_ledger = DailySpendLedger(daily_ledger)
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
) -> BriefComposeStage:
    """Assemble the morning brief's ``BriefComposeStage`` wired with the SAME TK-9 layer 2 budget
    plumbing as ``build_compose_stage``: a real ``DailySpendLedger`` over a ``DailyLedger`` on the
    SAME ``dsn``/``tz`` and the SAME ``"spend:tokens"`` ledger row, plus the
    ``mouth_daily_token_ceiling`` tunable from OperatingParams — so drain and brief share ONE
    daily token cap rather than each hand-rolling its own (the Q-69-lesson wiring, TK-53).
    """
    op = params if params is not None else load_operating_params()
    daily_ledger = DailyLedger(dsn, tz=tz)
    spend_ledger = DailySpendLedger(daily_ledger)
    return BriefComposeStage(
        config=config,
        tz=tz,
        spend_ledger=spend_ledger,
        daily_token_ceiling=op.mouth_daily_token_ceiling,
    )


def _epoch_now() -> float:
    """The real-clock default for the epoch-seconds seams (the gate/presence provider) this
    module composes — the ONE place ``assemble_runtime`` reads a wall clock directly."""
    return datetime.now(UTC).timestamp()


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
    source_registry: SourceRegistry
    pending_journal: PgPendingJournal
    queue: WombatQueue
    daily_ledger: DailyLedger
    compose_stage: ComposeStage


def assemble_runtime(
    *,
    config: WombatConfig,
    dsn: str,
    params: OperatingParams | None = None,
    tz: ZoneInfo = _UTC_ZONE,
) -> RuntimeBundle:
    """Compose the ONE standing wombat process (TK-53, Q-71).

    Registers the TK-7 drain pathway (id ``DRAIN_PATHWAY_ID``) and wires the REAL production
    ``Gate`` (TK-27) — a durable ``PendingSet`` backed by the TK-29 Postgres ``PendingJournal``
    (the Q-70/RISK-5 boot obligation), a ``CeilingLedger`` over a real ``DailyLedger``, the real
    presence provider, and ``UserModel`` ratings — over the SAME ``build_engine``/
    ``build_compose_stage``/``build_source_registry`` factories every other composition path
    uses (never hand-rolled, closing the ``scripts/demo_drain.py`` Q-69 budget-bypass gap).

    ``dsn`` backs every Postgres-touching seam composed here (the queue, the daily ledger, and
    the pending journal) — ONE Postgres, per ASMP-2. Every adapter below is lazy (no connection
    at construction), so calling this with an unreachable ``dsn`` is safe; the first real I/O
    happens once the returned bundle is actually driven.
    """
    op = params if params is not None else load_operating_params()

    # The v1 cold-boot substrate (Q-36/TK-14): in-memory journal/graph/latent + a FRESH
    # PathwayRegistry. This EXACT registry is handed to build_engine below, so the pathway
    # registered on it here is what the Engine resolves at run/resume/fire_timer time.
    substrate = build_substrate()

    queue = WombatQueue(dsn, max_size=op.max_pending)
    pending_journal = PgPendingJournal(dsn)
    pending_set = PendingSet(journal=pending_journal, max_pending=op.max_pending)
    daily_ledger = DailyLedger(dsn, tz=tz)
    ceiling = CeilingLedger(
        daily_ledger=daily_ledger, per_class_daily_ceiling=op.per_class_daily_ceiling
    )
    # TK-28 (Q-73): DayRollover composed over the SAME DailyLedger instance as CeilingLedger
    # (ceiling.py precedent) so the exactly-once boundary observation and the per-class ceiling
    # share ONE durable row lifecycle.
    day_rollover = DayRollover(daily_ledger=daily_ledger)
    user_model = UserModel(entity_kg=InMemoryEntityKG(), user_id=_RUNTIME_USER_ID)
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
    )
    review_or_speak_stage = ReviewOrSpeakStage(queue=queue)
    compose_dispatch_router = ComposeDispatchRouter(composer_by_kind={ItemKind.GENERIC: "compose"})
    compose_stage = build_compose_stage(config=config, dsn=dsn, params=op, tz=tz)

    graph = build_drain_pathway(
        drain_queue_stage,
        gate_stage,
        review_or_speak_stage,
        compose_dispatch_router,
        compose_stage,
    )
    substrate.pathways.register(DRAIN_PATHWAY_ID, graph)

    engine = build_engine(substrate, config=config, params=op)
    source_registry = build_source_registry(config, queue, tz=tz)

    return RuntimeBundle(
        engine=engine,
        pathways=substrate.pathways,
        journal=substrate.journal,
        drain_pathway_id=DRAIN_PATHWAY_ID,
        source_registry=source_registry,
        pending_journal=pending_journal,
        queue=queue,
        daily_ledger=daily_ledger,
        compose_stage=compose_stage,
    )
