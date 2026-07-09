"""TK-166 — boot replay of the pg pending journal (CR-1, P1, EP-8, Q-83 ruling).

ALL tests in this module require a real Postgres and are gated on ``WOMBAT_TEST_PG_DSN`` (the
SAME convention as ``tests/integration/test_serve_boot.py`` / ``tests/gate/
test_pending_journal_pg.py``): absent it, the whole module is skipped LOUDLY at collection time.

    docker run --rm -d -p 5511:5432 -e POSTGRES_PASSWORD=wombat postgres:16
    WOMBAT_TEST_PG_DSN=postgresql://postgres:wombat@localhost:5511/postgres

  AC1 (replay wired): a ``PgPendingJournal`` at the runtime DSN holding journaled
      ``PendingSetAdd`` records from a PRIOR process (items whose queue rows were already
      acked) -> ``assemble_runtime`` (default ``replay_pending``) builds the gate's pending
      set via ``PendingSet.rebuild_from_journal`` over that SAME pg journal. Pinned by
      inspecting the ACTUAL ``pending_set`` wired into the composed ``Gate`` (captured via a
      ``bootstrap.Gate`` spy) -- NOT an independent re-read of ``bundle.pending_journal``,
      which is a pure read invariant to whether the replay wiring is present or reverted
      (repair round finding: the original version of this test passed even against a
      sabotaged cold-constructor bootstrap).
  AC2 (the CR-1 crash-recovery proof): a FIRST assembled runtime scores an item below
      threshold (held, journaled add, acked off the queue by ``review_or_speak``) and is
      dropped WITHOUT any teardown (simulated kill). A SECOND ``assemble_runtime`` over the
      SAME dsn boots (default ``replay_pending``) and the held item is present in the SECOND
      bundle's own gate pending set (same spy technique as AC1) -- it survived the restart,
      never silently lost.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import UTC, datetime

import psycopg
import pytest
from cogworx.claims.provenance import Artifact, Provenance
from cogworx.loop.state import RunStatus

from wombat import bootstrap
from wombat.config import WombatConfig
from wombat.domain.daily_ledger import ensure_schema as ensure_daily_ledger_schema
from wombat.gate.decay import DayRolloverProtocol
from wombat.gate.models import ItemKind
from wombat.gate.pending_journal_pg import PgPendingJournal
from wombat.gate.pending_journal_pg import ensure_schema as ensure_pending_journal_schema
from wombat.gate.pending_set import PendingSet, PendingSetAdd
from wombat.gate.pipeline import Gate, UserModelProtocol
from wombat.gate.trigger import CeilingProtocol
from wombat.params import load_operating_params
from wombat.queue import QueueItem
from wombat.queue import ensure_schema as ensure_queue_schema

_DSN = os.environ.get("WOMBAT_TEST_PG_DSN")

if not _DSN:
    pytest.skip(
        "WOMBAT_TEST_PG_DSN is not set — skipping the TK-166 boot-replay DSN-gated tests, which "
        "require a real throwaway Postgres. Start one with:\n"
        "  docker run --rm -d -p 5511:5432 -e POSTGRES_PASSWORD=wombat postgres:16\n"
        "then export WOMBAT_TEST_PG_DSN=postgresql://postgres:wombat@localhost:5511/postgres",
        allow_module_level=True,
    )


def _config() -> WombatConfig:
    # An unreachable base_url (mirrors test_serve_boot.py's own documented pattern): the mouth's
    # model call fails fast and ComposeStage degrades cleanly — this module proves the boot-
    # replay WIRING, not a real DeepSeek response.
    return WombatConfig(deepseek_api_key="dummy-not-real-key", deepseek_base_url="https://x.test")


@pytest.fixture(autouse=True)
def _reset_singleton() -> None:
    bootstrap.reset_engine()


@pytest.fixture
def clean_tables() -> None:
    """Ensure every schema this composition touches exists, then truncate (mirrors
    ``test_serve_boot.py``'s own ``clean_tables`` convention)."""
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


def _spy_on_gate_pending_set(monkeypatch: pytest.MonkeyPatch) -> list[PendingSet]:
    """Monkeypatch ``bootstrap.Gate`` to capture each constructed ``Gate``'s ``pending_set``
    kwarg, in call order.

    This is the discriminating assertion point (repair round): ``bundle.pending_journal`` is
    constructed UNCONDITIONALLY inside ``assemble_runtime`` regardless of ``replay_pending``,
    so re-deriving a PendingSet from it independently is a pure journal read invariant to
    whether the boot-replay wiring is present. Capturing the ACTUAL ``pending_set`` handed to
    the composed ``Gate`` instead proves what the gate itself was built with: empty under the
    cold ``PendingSet(journal=..., max_pending=...)`` constructor, populated under
    ``PendingSet.rebuild_from_journal(...)``.
    """
    captured: list[PendingSet] = []
    real_gate = Gate  # the directly-imported class (bootstrap.Gate isn't a re-exported attr)

    def _spy_gate(
        *,
        user_model: UserModelProtocol,
        pending_set: PendingSet,
        ceiling: CeilingProtocol,
        urgency_threshold: float,
        load_flush_threshold: float,
        flush_min_age_seconds: float,
        decay_ttl_seconds: float,
        day_rollover: DayRolloverProtocol,
        clock: Callable[[], float],
    ) -> Gate:
        captured.append(pending_set)
        return real_gate(
            user_model=user_model,
            pending_set=pending_set,
            ceiling=ceiling,
            urgency_threshold=urgency_threshold,
            load_flush_threshold=load_flush_threshold,
            flush_min_age_seconds=flush_min_age_seconds,
            decay_ttl_seconds=decay_ttl_seconds,
            day_rollover=day_rollover,
            clock=clock,
        )

    monkeypatch.setattr(bootstrap, "Gate", _spy_gate)
    return captured


# --------------------------------------------------------------------------------------- AC1


def test_ac1_boot_replay_restores_pending_set_from_a_prior_processs_journal(
    clean_tables: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A PRIOR process's journaled adds are present in the journal -- the DEFAULT (eager)
    ``assemble_runtime`` builds the GATE's own pending set via ``PendingSet.rebuild_from_
    journal``, so the gate (captured via a ``bootstrap.Gate`` spy, not an independent re-read)
    reflects them."""
    assert _DSN is not None
    op = load_operating_params()

    # Simulate a PRIOR process: it held two items (their queue rows already acked elsewhere)
    # and journaled the adds directly.
    writer = PgPendingJournal(_DSN)
    try:
        writer.append(
            PendingSetAdd(
                item_id="prior-a", item_kind=ItemKind.GENERIC, urgency=0.2, load=0.1, added_at=1.0
            )
        )
        writer.append(
            PendingSetAdd(
                item_id="prior-b", item_kind=ItemKind.DRAFT, urgency=0.3, load=0.2, added_at=2.0
            )
        )
    finally:
        writer.close()

    captured_pending_sets = _spy_on_gate_pending_set(monkeypatch)
    bundle = bootstrap.assemble_runtime(config=_config(), dsn=_DSN, params=op)
    try:
        assert len(captured_pending_sets) == 1
        gate_pending_set = captured_pending_sets[0]
        ids = {item.item_id for item in gate_pending_set.list()}
        assert ids == {"prior-a", "prior-b"}
        assert gate_pending_set.cumulative_load() == pytest.approx(0.3)
    finally:
        bundle.queue.close()
        bundle.daily_ledger.close()
        bundle.pending_journal.close()


# --------------------------------------------------------------------------------------- AC2


async def test_ac2_held_item_survives_a_restart_into_a_second_assembled_runtime(
    clean_tables: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CR-1 headline proof: a held item journaled by a FIRST assembled runtime, dropped
    WITHOUT teardown (simulated crash), is present in the SECOND assembled runtime's OWN gate
    pending set (captured via a ``bootstrap.Gate`` spy, not an independent journal re-read) --
    it survived the restart, never silently lost."""
    assert _DSN is not None
    op = load_operating_params()

    captured_pending_sets = _spy_on_gate_pending_set(monkeypatch)

    # 1. FIRST runtime: enqueue a low-priority item that scores well below urgency_threshold
    #    (a non-timed, automated-sender GENERIC item) -- it is always HELD, never surfaced.
    bundle1 = bootstrap.assemble_runtime(config=_config(), dsn=_DSN, params=op)
    bundle1.queue.enqueue(
        QueueItem(
            idempotency_key="held-1",
            payload={
                "item_kind": "generic",
                "subject": "Newsletter digest",
                "is_timed": False,
                "sender_class": "automated",
            },
        )
    )
    run_id = "run-ac2"
    result = await bundle1.engine.run(
        run_id=run_id,
        session_id=run_id,
        pathway_id=bundle1.drain_pathway_id,
        initial=_initial_artifact(),
    )
    # HOLD -> review_or_speak returns Done (hold_report), not a park -- the run completes, the
    # item's queue row is acked, and its add is journaled into the SAME pending_journal table.
    assert result.status is RunStatus.COMPLETED

    # 2. SIMULATED CRASH: bundle1 is dropped here with NO .close() calls -- the restart proof
    #    requires nothing be torn down between the two assemblies.

    # 3. SECOND runtime boots over the SAME dsn (default replay_pending=True).
    bundle2 = bootstrap.assemble_runtime(config=_config(), dsn=_DSN, params=op)
    try:
        assert len(captured_pending_sets) == 2
        gate2_pending_set = captured_pending_sets[-1]  # the SECOND bundle's own gate
        ids = {item.item_id for item in gate2_pending_set.list()}
        assert "held-1" in ids  # survived the restart -- not silently lost
    finally:
        bundle1.queue.close()
        bundle1.daily_ledger.close()
        bundle1.pending_journal.close()
        bundle2.queue.close()
        bundle2.daily_ledger.close()
        bundle2.pending_journal.close()
