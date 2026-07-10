"""TK-53 — DSN-gated runtime boot acceptance criteria (EP-1, Q-71, Q-46).

ALL tests in this module require a real Postgres and are gated on ``WOMBAT_TEST_PG_DSN``
(the SAME convention as ``tests/integration/test_drain_pathway_e2e.py`` / ``tests/gate/
test_pending_journal_pg.py``): absent it, the whole module is skipped LOUDLY at collection time.

    docker run --rm -d -p 5511:5432 -e POSTGRES_PASSWORD=wombat postgres:16
    WOMBAT_TEST_PG_DSN=postgresql://postgres:wombat@localhost:5511/postgres

  AC3/AC4 (real-pg fidelity) ``assemble_runtime()`` composed against a REAL Postgres carries a
      non-None spend ledger, a non-default BudgetPolicy, an ``isinstance`` TK-29 PG
      ``PendingJournal``, and ``pathways.get`` resolves the registered drain pathway.
  AC5 the ONE full standing-loop cycle: enqueue an item -> the initial drive (``engine.run`` on
      the drain pathway) drains-then-Wait-self-parks on an EMPTY queue -> the item is enqueued
      -> ``Sweeper.tick`` is driven with an injected clock past the wake_at -> ``fire_timer``
      resumes the parked run and the item is drained through the REAL gate. ``tick()`` is
      driven directly — ``run_forever`` is NEVER called unbounded (the ticket's own ruling).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import psycopg
import pytest
from cogworx.claims.provenance import Artifact, Provenance
from cogworx.loop.state import RunStatus
from cogworx.runtime.sweeper import Sweeper

from wombat import bootstrap
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
    # this module proves the runtime WIRING, not a real DeepSeek response.
    return WombatConfig(deepseek_api_key="dummy-not-real-key", deepseek_base_url="https://x.test")


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


# --- AC5: the ONE full standing-loop cycle -----------------------------------------------------


async def test_ac5_full_standing_loop_cycle_enqueue_park_wake_drain(clean_tables: None) -> None:
    assert _DSN is not None
    op = load_operating_params()
    bundle = bootstrap.assemble_runtime(config=_config(), dsn=_DSN, params=op, tz=ZoneInfo("UTC"))
    try:
        run_id = "run-ac5"

        # 1. The initial drive on an EMPTY queue: drains nothing, then Wait-self-parks.
        parked = await bundle.engine.run(
            run_id=run_id,
            session_id=run_id,
            pathway_id=bundle.drain_pathway_id,
            initial=_initial_artifact(),
        )
        assert parked.status is RunStatus.WAITING

        # 2. NOW an item is enqueued — the parked run has not seen it yet.
        bundle.queue.enqueue(
            QueueItem(
                idempotency_key="ac5-vip",
                payload={
                    "item_kind": "generic",
                    "subject": "Board call starting now",
                    "is_timed": True,
                    "seconds_to_event": 0.0,
                    "sender_class": "vip",
                },
            )
        )

        # 3. Drive Sweeper.tick with an injected clock comfortably past the wake_at — NEVER
        #    run_forever unbounded.
        past_wake = datetime.now(UTC) + timedelta(hours=1)
        sweeper = Sweeper(
            journal=bundle.journal, fire=bundle.engine.fire_timer, clock=lambda: past_wake
        )
        lease_ttl = timedelta(seconds=op.sweeper_lease_ttl_seconds)
        fired = await sweeper.tick(past_wake, lease_ttl=lease_ttl)
        assert fired == 1

        # 4. fire_timer resumed the parked run and drained the item through the REAL gate.
        resumed = await bundle.journal.load_run(run_id)
        assert resumed is not None
        assert resumed.status is RunStatus.COMPLETED
        assert bundle.queue.drain() == []  # the item was acked — genuinely drained
    finally:
        bundle.queue.close()
        bundle.daily_ledger.close()
        bundle.pending_journal.close()
        bundle.behavior_event_log.close()
