"""scripts/demo_drain.py — the wombat drain-pathway thesis, running end-to-end (Q-55 demo harness).

THIS IS THE DEMO. It boots wombat's real, audited operating config, wires the REAL production
``Gate`` (TK-27) into the drain spine (TK-5/6/7/8/10) over a real ``WombatQueue`` on a throwaway
Postgres, and drives a small mixed fixture through it end-to-end — printing, per item, what the
gate decided and why.

RUN IT (a free localhost port >= 5525; adjust if that one is taken):

    docker run --rm -d --name wombat-demo-pg -p 5525:5432 -e POSTGRES_PASSWORD=wombat postgres:16
    # wait for `docker exec wombat-demo-pg pg_isready` to report "accepting connections", then:

    DEEPSEEK_API_KEY=dummy DEEPSEEK_BASE_URL=https://example.test \\
    WOMBAT_TEST_PG_DSN=postgresql://postgres:wombat@localhost:5525/postgres \\
        uv run python scripts/demo_drain.py

    # the "everything holds" presence scenarios (deterministic, no need to actually go idle):
    ... uv run python scripts/demo_drain.py --idle
    ... uv run python scripts/demo_drain.py --force-stale

    docker rm -f wombat-demo-pg

A REAL ``DEEPSEEK_API_KEY``/``DEEPSEEK_BASE_URL`` phrases the surfaced items for real; the dummy/
unreachable pair above makes ``ComposeStage``'s network call fail and degrade cleanly to
``TemplateComposer``'s terse line (proven safe: ``OpenAICompatModel`` does no I/O at construction,
only inside ``complete()``, which ``ComposeStage`` already wraps in a broad except — TK-8's own
contract). Either way this script runs to completion and never raises.

WHAT THIS SCRIPT WIRES (every seam below is as-built; nothing here is invented for the demo):
  * ``load_operating_params()`` — the SAME audited ``wombat_params.yaml`` thresholds production
    reads. PROVISIONAL (DEF-1): fixture-confirmed only, not yet Jim's confirmatory label pass —
    printed below with that exact provenance so a demo run is never mistaken for locked truth.
  * ``load_config()`` — the DeepSeek egress credentials (env-sourced, ASMP-1).
  * ONE ``WombatQueue`` (ASMP-2: the SAME instance wired into both ``DrainQueueStage`` and
    ``ReviewOrSpeakStage``) on the docker Postgres above.
  * ``cold_boot_bundle()`` substrate + a REAL cog-worx ``Engine`` + ``build_drain_pathway`` —
    mirrors ``tests/integration/test_drain_pathway_e2e.py``'s proven construction exactly.
  * The REAL production ``Gate`` (TK-27): ``UserModel`` over a fresh in-memory ``EntityKG``,
    ``PendingSet`` + ``InMemoryPendingJournal``, ``CeilingLedger`` over a REAL ``DailyLedger`` on
    the SAME docker Postgres, all wired into ``GateStage`` via ``make_gate_evaluator`` (Q-55).
  * A REAL presence provider (``make_presence_provider`` over the real OS idle signal) — pass
    ``--idle`` (a genuinely-idle snapshot) or ``--force-stale`` (a stale-timestamp snapshot) to
    script the "go idle/stale -> everything holds" moment deterministically instead of depending
    on whether you happen to be at the keyboard right now.
  * ONE seeded personalization claim (``RATING_CLAIM_PREDICATE`` / ``to_claim_payload`` via
    ``UserModel``'s own claim wire, ``rating/params.py``) showing the SAME item scoring
    differently before/after — DEC-13 personalization, live.

HONESTY NOTES (printed again at the end of every run — say them, never hide them):
  * The pending journal is IN-MEMORY here (``InMemoryPendingJournal``). Real Postgres durability
    for the pending set is TK-29, NOT YET BUILT — this demo's held items do not survive a process
    restart. Only the QUEUE's kill/restart exactly-once story is real: kill before ack -> restart
    -> the same item is redelivered (TK-2's proven lease/epoch mechanism).
  * The flush arm here only evaluates on item-carrying cycles (every ``engine.run()`` drive that
    reaches the gate with a fresh item). A dedicated heartbeat tick that flushes even while the
    queue sits idle is TK-28/TK-97, not yet built.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import psycopg
from cogworx.claims.provenance import Artifact, Claim, Provenance
from cogworx.knowledge.evidence import make_evidence
from cogworx.knowledge.identity import claim_id_for
from cogworx.model.base import ModelCapabilities
from cogworx.model.providers.config import ProviderConfig
from cogworx.model.registry import ModelRegistry, ModelSpec
from cogworx.runtime.engine import Engine
from cogworx.substrate.journal import RunState
from cogworx.testing.doubles import InMemoryEntityKG

from wombat.compose.templates import TemplateComposer
from wombat.config import WombatConfig, load_config
from wombat.domain.daily_ledger import DailyLedger
from wombat.domain.daily_ledger import ensure_schema as ensure_daily_ledger_schema
from wombat.gate.ceiling import CeilingLedger
from wombat.gate.models import GateItem, ItemKind
from wombat.gate.pending_set import InMemoryPendingJournal, PendingSet
from wombat.gate.pipeline import Gate
from wombat.gate.presence_hold import presence_hold
from wombat.gate.scoring import cognitive_load, urgency
from wombat.params import OperatingParams, load_operating_params
from wombat.pathways.drain_pathway import build_drain_pathway
from wombat.queue import QueueItem, WombatQueue
from wombat.queue import ensure_schema as ensure_queue_schema
from wombat.rating.params import (
    RATING_CLAIM_PREDICATE,
    EventClass,
    default_params_for,
    to_claim_payload,
)
from wombat.sources.presence import (
    PresenceSnapshot,
    PresenceState,
    make_presence_provider,
    read_idle_ms,
)
from wombat.stages.artifacts import (
    HOLD_REPORT,
    composed_output_from_artifact_data,
    gate_decisions_from_artifact_data,
    hold_report_from_artifact_data,
)
from wombat.stages.compose import ComposeStage
from wombat.stages.compose_dispatch_router import ComposeDispatchRouter
from wombat.stages.drain_queue import DrainQueueStage
from wombat.stages.gate_stage import GateStage, make_gate_evaluator
from wombat.stages.review_or_speak import ReviewOrSpeakStage
from wombat.substrate import cold_boot_bundle
from wombat.user_model.user_model import UserModel

_PATHWAY_ID = "demo-drain"
_MODEL_PROFILE = "deepseek"
_USER_ID = "demo-user"
_PENDING_MAX = 100

# The network-SDK loggers that shout a full connection traceback when the mouth's model call fails
# against an unreachable base_url — an EXPECTED degrade in this keyless demo (TK-8's contract),
# not a real fault, so we quiet them below rather than let them bury the clean stdout narrative.
_NOISY_LOGGERS = ("httpx", "httpcore", "openai", "openai._base_client", "wombat.stages.compose")


def _configure_console_encoding() -> None:
    """Force UTF-8 on stdout/stderr so the em-dashes and arrows in this demo's output render
    cleanly instead of as '?' under the Windows console's default cp1252 (must run before any
    print). ``reconfigure`` exists on ``TextIOWrapper`` (py3.7+); guarded for exotic streams."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


class _DropTracebackFilter(logging.Filter):
    """Strip ``exc_info``/``exc_text`` off every record so an emitted warning stays ONE clean line
    (never a multi-hundred-line httpx/openai traceback). Applied to the demo's own root handler."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.exc_info = None
        record.exc_text = None
        return True


def _quiet_expected_degrade_logging() -> None:
    """Keep the demo's stderr clean.

    When run keyless (a dummy/unreachable ``DEEPSEEK_BASE_URL``) the mouth's model call fails and
    ``ComposeStage`` degrades to the terse template — the CORRECT, demonstrated behavior, shown on
    stdout as ``SURFACED [degraded template]``. But that degrade path logs a WARNING with
    ``exc_info=True`` (TK-8's ``ComposeStage`` — NOT ours to change) and the openai/httpx/httpcore
    SDKs log their own connection errors, so without this the expected degrade dumps a full
    traceback to stderr for every surfaced item. We (a) install a root handler whose filter drops
    tracebacks, so nothing ever prints a stack, and (b) raise the known-noisy loggers to CRITICAL
    so the expected degrade is silent on stderr (its story is already told on stdout). This is a
    demo-presentation concern only — no production logging behavior is touched."""
    root = logging.getLogger()
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(logging.ERROR)
    handler.addFilter(_DropTracebackFilter())
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root.addHandler(handler)
    root.setLevel(logging.ERROR)
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.CRITICAL)


def _bar(title: str) -> None:
    print()
    print(f"=== {title} ===")


@dataclass
class _DemoClock:
    """A mutable wall-clock the demo advances deterministically.

    ``Gate``, ``PendingSet``, ``CeilingLedger``/``DailyLedger``, and the presence provider all
    read epoch-seconds or a datetime through THIS one instant — never a real clock read mid-run —
    so the accumulate-then-flush scenario is exactly reproducible run to run.
    """

    instant: datetime

    def epoch(self) -> float:
        return self.instant.timestamp()

    def wall(self) -> datetime:
        return self.instant

    def advance(self, seconds: float) -> None:
        self.instant = self.instant + timedelta(seconds=seconds)


def _print_operating_params_provenance(op: OperatingParams) -> None:
    _bar(f"Operating parameters (wombat_params.yaml, version {op.version})")
    print(
        f"  urgency_threshold        = {op.urgency_threshold}    "
        "PROVISIONAL (DEF-1, TK-26 spike, fixture-confirmed only)"
    )
    print(
        f"  load_flush_threshold     = {op.load_flush_threshold}    PROVISIONAL (DEF-1, same spike)"
    )
    print(
        f"  per_class_daily_ceiling  = {op.per_class_daily_ceiling}      "
        "PROVISIONAL (DEF-1, same spike)"
    )
    print(f"  flush_min_age_seconds    = {op.flush_min_age_seconds}  CHOSEN-HERE (TK-13 audit gap)")
    print(
        f"  presence_staleness_ceiling_seconds = {op.presence_staleness_ceiling_seconds}  "
        "CHOSEN-HERE (ported from the TK-4 spike)"
    )
    print(
        "  These are the values THIS demo runs with — provisional, not yet locked production"
        " truth. See wombat_params.yaml's own provenance legend for the full picture."
    )


async def _seed_and_show_personalization(
    user_model: UserModel, entity_kg: InMemoryEntityKG, wall_now: datetime
) -> None:
    """DEC-13 personalization, live: score the SAME draft-reply item before/after seeding one
    personalized rating claim, using only as-built seams (UserModel.ratings_for, the
    RATING_CLAIM_PREDICATE claim wire, and the real urgency()/cognitive_load() heuristics)."""
    _bar("Personalization contrast (DEC-13) — one seeded claim, the same item, two scores")

    item = GateItem(
        item_id="personalization-demo",
        item_kind=ItemKind.DRAFT,
        created_at=0.0,
        payload={"is_timed": False, "sender_class": "known_human"},
    )

    before_params = await user_model.ratings_for(item)
    before_urgency = urgency(item, before_params)
    before_load = cognitive_load(item, before_params)
    print(
        f"  BEFORE (default DRAFT_REPLY params {before_params}): "
        f"urgency={before_urgency:.2f} load={before_load:.2f}"
    )

    custom = default_params_for(EventClass.DRAFT_REPLY).with_updates(
        urgency_base=0.85, urgency_gain=0.9
    )
    scope = f"user:{_USER_ID}"
    payload = to_claim_payload(custom)
    claim_id = claim_id_for(
        EventClass.DRAFT_REPLY.value, RATING_CLAIM_PREDICATE, payload, scope=scope
    )
    claim = Claim(
        id=claim_id,
        subject=EventClass.DRAFT_REPLY.value,
        predicate=RATING_CLAIM_PREDICATE,
        payload=payload,
        epistemic_type="observation",
        provenance=Provenance(source="human", confidence=0.9, recorded_at=wall_now),
        valid_from=wall_now,
        ingest_time=wall_now,
        created_by="demo_drain",
        scope=scope,
    )
    evidence = make_evidence(
        type="attestation",
        polarity="+",
        source_id="demo-source",
        source_authority=0.9,
        recorded_at=wall_now,
    )
    await entity_kg.write_claim(claim, evidence=evidence)

    after_params = await user_model.ratings_for(item)
    after_urgency = urgency(item, after_params)
    after_load = cognitive_load(item, after_params)
    print(
        f"  AFTER  (seeded personalized claim {after_params}): "
        f"urgency={after_urgency:.2f} load={after_load:.2f}"
    )
    print(
        "  Same item, same payload — the personalized claim alone moved the score "
        f"({before_urgency:.2f} -> {after_urgency:.2f})."
    )


def _build_model_registry(config: WombatConfig) -> ModelRegistry:
    """Register the REAL DeepSeek ``ModelSpec`` (mirrors ``wombat.bootstrap``'s composition) so
    the mouth genuinely attempts a network call — a dummy/unreachable base_url degrades cleanly
    inside ``ComposeStage``'s own except (TK-8's contract); it never breaks the drain loop."""
    registry = ModelRegistry()
    spec = ModelSpec(
        provider="openai_compat",
        config=ProviderConfig(
            api_key=config.deepseek_api_key,
            base_url=config.deepseek_base_url,
            model_pro="deepseek-chat",
            model_flash="deepseek-chat",
        ),
        capabilities=ModelCapabilities(structured_output=True, streaming=True, tools=True),
    )
    registry.register_spec(_MODEL_PROFILE, spec)
    return registry


def _reset_demo_tables(dsn: str) -> None:
    """Make the demo re-runnable: ensure both schemas exist, then truncate — this demo owns its
    throwaway Postgres exclusively, so a clean slate every run is simpler than unique keys."""
    with psycopg.connect(dsn) as conn:
        ensure_queue_schema(conn)
        ensure_daily_ledger_schema(conn)
        with conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE wombat_queue")
            cur.execute("TRUNCATE TABLE daily_ledger")
        conn.commit()


def _presence_explanation(
    op: OperatingParams, presence: PresenceSnapshot | None, now_epoch: float
) -> str:
    if presence is None:
        return "presence unavailable -> HOLD (fail-safe, Q-12)"
    held = presence_hold(
        presence,
        now_epoch,
        staleness_ceiling_s=op.presence_staleness_ceiling_seconds,
        confidence_floor=op.presence_confidence_floor,
    )
    age = presence.age_seconds(now_epoch)
    verdict = "HOLD (presence)" if held else "surfacing permitted by presence"
    return (
        f"presence={presence.state.value} confidence={presence.confidence} "
        f"age={age:.0f}s -> {verdict}"
    )


async def _explain_item_before_drive(
    user_model: UserModel,
    ceiling: CeilingLedger,
    op: OperatingParams,
    item: GateItem,
    presence: PresenceSnapshot | None,
    now_epoch: float,
) -> None:
    event_class = user_model.resolve_event_class(item)
    params = await user_model.ratings_for(item)
    item_urgency = urgency(item, params)
    item_load = cognitive_load(item, params)
    worthy = item_urgency > op.urgency_threshold
    ceiling_allows = ceiling.allow(event_class)
    print(f"  item={item.item_id!r} event_class={event_class.value}")
    print(
        f"    scoring: urgency={item_urgency:.2f} (threshold {op.urgency_threshold}) "
        f"load={item_load:.2f} -> {'worthy' if worthy else 'not worthy'} of immediate surfacing"
    )
    print(f"    ceiling: allow({event_class.value}) = {ceiling_allows}")
    print(f"    {_presence_explanation(op, presence, now_epoch)}")


def _summarize_run(final: RunState) -> None:
    """Print what actually happened this drive, read straight off the journaled StepResults —
    the SAME artifacts a real consumer would read, nothing peeked out of the gate's internals."""
    steps = final.steps

    gate_steps = [s for s in steps if s.stage_name == "gate"]
    if gate_steps:
        gate_output = gate_steps[0].result.output
        assert gate_output is not None
        entries = gate_decisions_from_artifact_data(gate_output.data)
        for decision, queue_item in entries:
            print(
                f"    GATE DECIDED: {decision.action.value} "
                f"({len(decision.items)} scored item(s) carried) for {queue_item.idempotency_key!r}"
            )

    ros_steps = [s for s in steps if s.stage_name == "review_or_speak"]
    if ros_steps:
        art = ros_steps[0].result.output
        assert art is not None
        if art.kind == HOLD_REPORT:
            for hold in hold_report_from_artifact_data(art.data):
                print(f"    HELD: {hold['item_id']!r} — {hold['reason']}")

    compose_steps = [s for s in steps if s.stage_name == "compose"]
    if compose_steps:
        compose_output = compose_steps[0].result.output
        assert compose_output is not None
        text, item_id, _item_kind, degraded = composed_output_from_artifact_data(
            compose_output.data
        )
        tag = "degraded template" if degraded else "model-phrased"
        print(f'    SURFACED [{tag}] ({item_id!r}): "{text}"')


async def _drive_one(
    engine: Engine, queue: WombatQueue, run_id: str, item: QueueItem, wall_now: datetime
) -> RunState:
    queue.enqueue(item)
    initial = Artifact(
        kind="drain-tick",
        produced_by="demo_drain",
        provenance=Provenance(source="system", confidence=1.0, recorded_at=wall_now),
        data={},
    )
    return await engine.run(
        run_id=run_id, session_id=run_id, pathway_id=_PATHWAY_ID, initial=initial
    )


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--idle",
        action="store_true",
        help="Override presence with a genuinely-idle snapshot (state=IDLE) so everything holds.",
    )
    parser.add_argument(
        "--force-stale",
        action="store_true",
        help="Override presence with a stale-timestamp snapshot (age > staleness ceiling) so "
        "everything holds via the staleness defense specifically.",
    )
    parser.add_argument(
        "--force-active",
        action="store_true",
        help="Override presence with a fresh, confident ACTIVE snapshot regardless of the real "
        "OS idle signal — for a reproducible happy-path capture on a headless/automated box "
        "where nobody is actually at the keyboard (the REAL provider is still the true default).",
    )
    return parser.parse_args(argv)


async def main(argv: list[str] | None = None) -> int:
    # Presentation setup FIRST — before any print or any engine drive that could log.
    _configure_console_encoding()
    _quiet_expected_degrade_logging()

    args = _parse_args(sys.argv[1:] if argv is None else argv)

    dsn = os.environ.get("WOMBAT_TEST_PG_DSN")
    if not dsn:
        print(
            "WOMBAT_TEST_PG_DSN is not set. This demo needs a throwaway Postgres — see the "
            "module docstring for the docker one-liner.",
            file=sys.stderr,
        )
        return 1

    op = load_operating_params()
    _print_operating_params_provenance(op)

    config = load_config()

    _bar("Resetting the demo's own Postgres tables (wombat_queue, daily_ledger)")
    _reset_demo_tables(dsn)
    print("  done — this demo owns this Postgres exclusively, so a clean slate every run.")

    demo_clock = _DemoClock(instant=datetime(2026, 7, 2, 9, 0, 0, tzinfo=UTC))

    entity_kg = InMemoryEntityKG()
    user_model = UserModel(entity_kg=entity_kg, user_id=_USER_ID)
    await _seed_and_show_personalization(user_model, entity_kg, demo_clock.wall())

    pending_set = PendingSet(journal=InMemoryPendingJournal(), max_pending=_PENDING_MAX)
    daily_ledger = DailyLedger(dsn, tz=ZoneInfo("UTC"), clock=demo_clock.wall)
    ceiling = CeilingLedger(
        daily_ledger=daily_ledger, per_class_daily_ceiling=op.per_class_daily_ceiling
    )
    gate = Gate(
        user_model=user_model,
        pending_set=pending_set,
        ceiling=ceiling,
        urgency_threshold=op.urgency_threshold,
        load_flush_threshold=op.load_flush_threshold,
        flush_min_age_seconds=op.flush_min_age_seconds,
        clock=demo_clock.epoch,
    )

    _bar("Presence")
    live_idle_ms = read_idle_ms()
    print(
        f"  live OS idle signal right now: {live_idle_ms} ms "
        "(informational only — see below for what the gate actually sees)"
    )
    real_presence_provider = make_presence_provider(
        clock=demo_clock.epoch,
        staleness_ceiling_s=op.presence_staleness_ceiling_seconds,
        idle_threshold_s=op.presence_idle_threshold_seconds,
    )
    if args.idle:
        print("  --idle: overriding with a genuinely-idle snapshot (state=IDLE).")

        def presence_provider() -> PresenceSnapshot:
            idle_ms = int(op.presence_idle_threshold_seconds * 1000.0) + 5_000
            return PresenceSnapshot(
                state=PresenceState.IDLE,
                confidence=1.0,
                idle_ms=idle_ms,
                taken_at=demo_clock.epoch(),
            )
    elif args.force_stale:
        print("  --force-stale: overriding with a stale-timestamp snapshot (age > ceiling).")

        def presence_provider() -> PresenceSnapshot:
            stale_taken_at = demo_clock.epoch() - (op.presence_staleness_ceiling_seconds + 30.0)
            return PresenceSnapshot(
                state=PresenceState.ACTIVE, confidence=1.0, idle_ms=0, taken_at=stale_taken_at
            )
    elif args.force_active:
        print(
            "  --force-active: overriding with a fresh, confident ACTIVE snapshot "
            "(for a reproducible capture regardless of the real OS idle signal)."
        )

        def presence_provider() -> PresenceSnapshot:
            return PresenceSnapshot(
                state=PresenceState.ACTIVE, confidence=1.0, idle_ms=0, taken_at=demo_clock.epoch()
            )
    else:
        print("  using the REAL presence provider over the live OS idle signal.")
        presence_provider = real_presence_provider

    # ASMP-2: ONE WombatQueue instance feeds BOTH DrainQueueStage and ReviewOrSpeakStage.
    queue = WombatQueue(dsn, max_size=100)
    drain_queue_stage = DrainQueueStage(queue, batch_size=1, poll_interval_seconds=5.0)
    gate_stage = GateStage(
        evaluate=make_gate_evaluator(
            gate=gate,
            staleness_ceiling_s=op.presence_staleness_ceiling_seconds,
            confidence_floor=op.presence_confidence_floor,
            clock=demo_clock.epoch,
        ),
        presence_provider=presence_provider,
    )
    review_or_speak_stage = ReviewOrSpeakStage(queue=queue)
    compose_dispatch_router = ComposeDispatchRouter(composer_by_kind={ItemKind.GENERIC: "compose"})
    compose_stage = ComposeStage(config=config, template_composer=TemplateComposer())

    graph = build_drain_pathway(
        drain_queue_stage, gate_stage, review_or_speak_stage, compose_dispatch_router, compose_stage
    )
    bundle = cold_boot_bundle()
    bundle.pathways.register(_PATHWAY_ID, graph)

    engine = Engine(
        models=_build_model_registry(config),
        journal=bundle.journal,
        graph_store=bundle.graph_store,
        latent=bundle.latent,
        pathways=bundle.pathways,
        model_profile=_MODEL_PROFILE,
        clock=lambda: datetime.now(UTC),
    )

    # --- The mixed fixture -------------------------------------------------------------------
    vip_item = QueueItem(
        idempotency_key="demo-vip-1",
        payload={
            "item_kind": "generic",
            "subject": "Board call starting in 1 minute",
            "is_timed": True,
            "seconds_to_event": 60.0,
            "sender_class": "vip",
        },
    )
    newsletter_item = QueueItem(
        idempotency_key="demo-newsletter-1",
        payload={
            "item_kind": "generic",
            "subject": "Your weekly automated newsletter digest",
            "is_timed": False,
            "sender_class": "automated",
        },
    )
    deep_thread_item = QueueItem(
        idempotency_key="demo-thread-1",
        payload={
            "item_kind": "generic",
            "subject": "Re: Re: Re: project scope thread",
            "is_timed": False,
            "sender_class": "known_human",
            "thread_depth": 10,
        },
    )

    try:
        _bar("1) VIP near-deadline item — expect SURFACE_IMMEDIATE, one composed line")
        await _explain_item_before_drive(
            user_model,
            ceiling,
            op,
            GateItem(
                item_id="demo-vip-1",
                item_kind=ItemKind.GENERIC,
                created_at=0.0,
                payload=vip_item.payload,
            ),
            presence_provider(),
            demo_clock.epoch(),
        )
        final = await _drive_one(engine, queue, "run-vip", vip_item, demo_clock.wall())
        _summarize_run(final)

        _bar("2) Automated newsletter — expect HOLD, journaled reason")
        await _explain_item_before_drive(
            user_model,
            ceiling,
            op,
            GateItem(
                item_id="demo-newsletter-1",
                item_kind=ItemKind.GENERIC,
                created_at=0.0,
                payload=newsletter_item.payload,
            ),
            presence_provider(),
            demo_clock.epoch(),
        )
        final = await _drive_one(
            engine, queue, "run-newsletter", newsletter_item, demo_clock.wall()
        )
        _summarize_run(final)

        _bar(
            f"Advancing the injected clock by {op.flush_min_age_seconds + 60:.0f}s "
            "past flush_min_age_seconds, so the next held item tips the load-flush arm"
        )
        demo_clock.advance(op.flush_min_age_seconds + 60.0)
        print(f"  clock is now {demo_clock.wall().isoformat()}")

        _bar("3) Deep email thread — expect this cycle to trip SURFACE_FLUSH (accumulated load)")
        await _explain_item_before_drive(
            user_model,
            ceiling,
            op,
            GateItem(
                item_id="demo-thread-1",
                item_kind=ItemKind.GENERIC,
                created_at=0.0,
                payload=deep_thread_item.payload,
            ),
            presence_provider(),
            demo_clock.epoch(),
        )
        load_before = pending_set.cumulative_load()
        print(f"    pending set cumulative_load BEFORE this cycle: {load_before:.2f}")
        final = await _drive_one(engine, queue, "run-thread", deep_thread_item, demo_clock.wall())
        _summarize_run(final)
        print(f"    pending set size AFTER this cycle: {len(pending_set)}")
    finally:
        queue.close()
        daily_ledger.close()

    _bar("HONESTY NOTES")
    print(
        "  * The pending journal is IN-MEMORY (InMemoryPendingJournal) — real Postgres durability"
        " for the pending set is TK-29, not yet built. Held items here do NOT survive a restart."
    )
    print(
        "  * The kill-restart exactly-once story demonstrated by this spine is the QUEUE's (TK-2):"
        " kill before ack -> restart -> the same item redelivers. The pending set has no such"
        " proof yet."
    )
    print(
        "  * The flush arm here only evaluates on item-carrying cycles (a fresh item reaching the"
        " gate). A dedicated heartbeat tick that flushes even while the queue sits idle is"
        " TK-28/TK-97, not yet built."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
