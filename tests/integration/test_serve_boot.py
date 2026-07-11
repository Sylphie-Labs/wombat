"""TK-53 — DSN-gated runtime boot acceptance criteria (EP-1, Q-71, Q-46).

ALL tests in this module require a real Postgres and are gated on ``WOMBAT_TEST_PG_DSN``
(the SAME convention as ``tests/integration/test_drain_pathway_e2e.py`` / ``tests/gate/
test_pending_journal_pg.py``): absent it, the whole module is skipped LOUDLY at collection time.

    docker run --rm -d -p 5511:5432 -e POSTGRES_PASSWORD=wombat postgres:16
    WOMBAT_TEST_PG_DSN=postgresql://postgres:wombat@localhost:5511/postgres

  AC3/AC4 (real-pg fidelity) ``assemble_runtime()`` composed against a REAL Postgres carries a
      non-None spend ledger, a non-default BudgetPolicy, an ``isinstance`` TK-29 PG
      ``PendingJournal``, and ``pathways.get`` resolves the registered drain pathway.
  AC1 (TK-230, DEC-41) the standing-loop cycle drains MORE THAN ONE item without a restart: item
      A is enqueued and drained to a genuinely terminal ``Done`` run (never a self-parked
      ``Wait`` any more — TK-230 retired that pattern, see ``test_drain_queue_stage.py``), THEN
      item B is enqueued and a SECOND pump-style peek-and-drain sweep — driven by hand here,
      mirroring ``wombat.runtime._run_drain_pump``'s own bounded peek/drain loop, never an
      unbounded ``run_forever`` — picks it up too, in a FRESH run, proving nothing is stranded
      after the first item (the CRF-2 bug this ticket fixes).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

import psycopg
import pytest
from cogworx.claims.provenance import Artifact, Provenance
from cogworx.loop.state import RunStatus

from wombat import bootstrap
from wombat.bootstrap import RuntimeBundle
from wombat.config import WombatConfig
from wombat.domain.daily_ledger import ensure_schema as ensure_daily_ledger_schema
from wombat.gate.pending_journal_pg import PgPendingJournal
from wombat.gate.pending_journal_pg import ensure_schema as ensure_pending_journal_schema
from wombat.params import load_operating_params
from wombat.queue import QueueItem
from wombat.queue import ensure_schema as ensure_queue_schema

_DSN = os.environ.get("WOMBAT_TEST_PG_DSN")

if not _DSN:
    pytest.skip(
        "WOMBAT_TEST_PG_DSN is not set — skipping the TK-53 runtime boot DSN-gated tests, which "
        "require a real throwaway Postgres. Start one with:\n"
        "  docker run --rm -d -p 5511:5432 -e POSTGRES_PASSWORD=wombat postgres:16\n"
        "then export WOMBAT_TEST_PG_DSN=postgresql://postgres:wombat@localhost:5511/postgres",
        allow_module_level=True,
    )


def _config() -> WombatConfig:
    # An unreachable base_url (mirrors scripts/demo_drain.py's own documented pattern): the
    # mouth's model call fails fast and ComposeStage degrades cleanly to the terse template —
    # this module proves the runtime WIRING, not a real DeepSeek response. wombat_voice_enabled
    # is forced off explicitly (TK-230) — this module's tests drive real items through the FULL
    # pathway to completion, and an explicit kwarg overrides whatever a populated operator .env
    # at the repo root configures, so this never risks a real voice/audio adapter call.
    return WombatConfig(
        deepseek_api_key="dummy-not-real-key",
        deepseek_base_url="https://x.test",
        wombat_voice_enabled=False,
    )


@pytest.fixture(autouse=True)
def _reset_singleton() -> None:
    bootstrap.reset_engine()


@pytest.fixture
def clean_tables() -> None:
    """Ensure every schema this composition touches exists, then truncate (mirrors
    ``test_drain_pathway_e2e.py``'s own ``clean_table`` convention)."""
    assert _DSN is not None
    with psycopg.connect(_DSN) as conn:
        ensure_queue_schema(conn)
        ensure_daily_ledger_schema(conn)
        ensure_pending_journal_schema(conn)
        with conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE wombat_queue")
            cur.execute("TRUNCATE TABLE daily_ledger")
            cur.execute("TRUNCATE TABLE pending_journal")
        conn.commit()


def _initial_artifact() -> Artifact:
    return Artifact(
        kind="drain-tick",
        produced_by="test",
        provenance=Provenance(source="system", confidence=1.0, recorded_at=datetime.now(UTC)),
        data={},
    )


# --- AC3/AC4 (real-pg fidelity): assembly carries the real budget/spend/PG-journal/pathway -----


def test_assemble_runtime_against_real_postgres_carries_the_real_composition(
    clean_tables: None,
) -> None:
    assert _DSN is not None
    op = load_operating_params()
    bundle = bootstrap.assemble_runtime(config=_config(), dsn=_DSN, params=op, tz=ZoneInfo("UTC"))
    try:
        graph = bundle.pathways.get(bundle.drain_pathway_id)
        assert graph is not None

        assert isinstance(bundle.pending_journal, PgPendingJournal)

        assert bundle.engine._budget_policy.max_usd_per_drive is not None
        assert bundle.engine._budget_policy.max_calls_per_drive is not None
        assert bundle.compose_stage._spend_ledger is not None
    finally:
        bundle.queue.close()
        bundle.daily_ledger.close()
        bundle.pending_journal.close()
        bundle.behavior_event_log.close()


# --- AC1 (TK-230, DEC-41): the standing-loop cycle keeps draining past the first item ----------


async def _drain_until_empty(bundle: RuntimeBundle, *, max_iterations: int = 10) -> int:
    """One pump-style sweep, driven by hand: chase ``pending_count()`` down to 0, firing one
    fresh ``engine.run`` drive per still-pending item — mirrors ``wombat.runtime._run_drain_pump``
    exactly, BOUNDED (never an unbounded ``run_forever``/beat-sleep loop) so a regression can
    never hang this test. Returns how many runs it fired."""
    runs = 0
    for _ in range(max_iterations):
        if bundle.queue.pending_count() <= 0:
            return runs
        run_id = f"run-ac1-pump-{uuid4()}"
        resumed = await bundle.engine.run(
            run_id=run_id,
            session_id=run_id,
            pathway_id=bundle.drain_pathway_id,
            initial=_initial_artifact(),
        )
        assert resumed.status is RunStatus.COMPLETED  # each fresh run reaches a genuine Done
        runs += 1
    return runs


def _vip_item(idempotency_key: str, subject: str) -> QueueItem:
    return QueueItem(
        idempotency_key=idempotency_key,
        payload={
            "item_kind": "generic",
            "subject": subject,
            "is_timed": True,
            "seconds_to_event": 0.0,
            "sender_class": "vip",
        },
    )


async def test_ac1_pump_style_drain_picks_up_item_b_after_item_a_without_restart(
    clean_tables: None,
) -> None:
    """Corrects the old AC5 blind spot: that test only ever proved a SINGLE item survives one
    self-park-then-wake cycle. TK-230 (CRF-2) fixed the actual bug — a run reaching terminal
    ``Done`` after item A used to strand every item enqueued afterward until a process restart.
    This proves item B, enqueued only AFTER item A has already drained to Done, is still picked
    up — in a fresh run, no restart — by the SAME pump-style sweep."""
    assert _DSN is not None
    op = load_operating_params()
    bundle = bootstrap.assemble_runtime(config=_config(), dsn=_DSN, params=op, tz=ZoneInfo("UTC"))
    try:
        # 1. Item A: enqueued, then drained to a genuinely terminal Done run.
        bundle.queue.enqueue(_vip_item("ac1-item-a", "Board call starting now"))
        drained_a = await _drain_until_empty(bundle)
        assert drained_a == 1
        assert bundle.queue.pending_count() == 0

        # 2. Item B: enqueued ONLY NOW — after A's run already reached Done. The old bug: nothing
        #    would ever re-drive drain_queue again after a Done run, stranding B until a restart.
        bundle.queue.enqueue(_vip_item("ac1-item-b", "Second call starting now"))
        drained_b = await _drain_until_empty(bundle)
        assert drained_b == 1  # the regression check: B is picked up WITHOUT a restart

        assert bundle.queue.drain() == []  # both items genuinely acked, none stranded
    finally:
        bundle.queue.close()
        bundle.daily_ledger.close()
        bundle.pending_journal.close()
        bundle.behavior_event_log.close()
