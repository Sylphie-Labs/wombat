"""TK-177 — outbound live wiring acceptance criteria (EP-18, Q-92).

WIRING ONLY over already-built pieces (TK-78 ``DraftComposer``, TK-79 ``DraftDispatchStage``,
TK-80 ``reply_intent.build``) — no new domain logic here either. ALL tests in this module require
a real Postgres and are gated on ``WOMBAT_TEST_PG_DSN`` (the Q-46 done-bar, mirroring every other
``tests/integration/*_e2e.py`` module):

    docker run --rm -d -p 5511:5432 -e POSTGRES_PASSWORD=wombat postgres:16
    WOMBAT_TEST_PG_DSN=postgresql://postgres:wombat@localhost:5511/postgres

  AC1 emission+idempotency (WIRE 1, ``sources/bootstrap.GmailWithReplyIntents``): a fake wrapped
      Gmail poller returning one HIGH-triage message -> one poll tick enqueues the message item
      AND exactly one draft item (``item_kind="draft"``, deterministic
      ``idempotency_key("gmail", "reply:<message_id>")``); a second poll of the SAME message
      enqueues nothing (``ALREADY_QUEUED``); a NORMAL-band message emits no draft item.
  AC2 end-to-end park (WIRE 2/3, ``bootstrap.assemble_runtime``): the wired drain graph, on the
      REAL Engine with a fake ``gmail.drafts.create`` capability and the real gate/queue, surfaces
      a draft item through the real gate, routes DRAFT -> ``draft_composer``, parks
      ``AWAITING_HUMAN`` at the COMPUTED ``ask_step_index``, and ``provide_human_input({'decision':
      'approve'})`` completes with the trail row DISPATCHED and the drafts.create spy count still
      exactly 1.
  AC3 loud-skip (WIRE 2/3): a Google-less ``assemble_runtime`` still boots; the warning names the
      skipped outbound wiring; the drain graph is BYTE-IDENTICAL to the pre-TK-177 5-stage
      construction and the brief pathway registration is unaffected.

Presence (TK-11) is forced ACTIVE for AC2 by monkeypatching the ONE impure OS-idle read
(``wombat.sources.presence.read_idle_ms``) — this module's own real-idle-time read is otherwise
environment-dependent (a genuinely idle build host would hold every item, never surfacing). Gmail
auth/session are faked at the SAME sanctioned module boundary ``tests/sources/test_bootstrap.py``
already established (``wombat.integrations.gmail.session.GmailAuth``/``AuthorizedSession``).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
import pytest
from cogworx.claims.provenance import Artifact, Provenance
from cogworx.loop.state import RunStatus
from cogworx.substrate.journal import RunState
from pydantic import SecretStr

import wombat.integrations.gmail.session as gmail_session_module
import wombat.sources.presence as presence_module
from wombat import bootstrap
from wombat.config import WombatConfig
from wombat.domain.daily_ledger import ensure_schema as ensure_daily_ledger_schema
from wombat.domain.item_identity import idempotency_key as derive_key
from wombat.gate.pending_journal_pg import ensure_schema as ensure_pending_journal_schema
from wombat.integrations.gmail.models import GmailMessageItem
from wombat.integrations.gmail.reply_intent import ReplyIntent
from wombat.integrations.gmail.triage import load_triage_rules
from wombat.params import load_operating_params
from wombat.queue import EnqueueResult, QueueItem, WombatQueue
from wombat.queue import ensure_schema as ensure_queue_schema
from wombat.sources.base import SourceEvent
from wombat.sources.bootstrap import GmailWithReplyIntents
from wombat.trail.reader import ActionTrailReader
from wombat.trail.schema import ensure_schema as ensure_trail_schema

_DSN = os.environ.get("WOMBAT_TEST_PG_DSN")

if not _DSN:
    pytest.skip(
        "WOMBAT_TEST_PG_DSN is not set — skipping the TK-177 outbound wiring e2e, which requires "
        "a real throwaway Postgres. Start one with:\n"
        "  docker run --rm -d -p 5511:5432 -e POSTGRES_PASSWORD=wombat postgres:16\n"
        "then export WOMBAT_TEST_PG_DSN=postgresql://postgres:wombat@localhost:5511/postgres",
        allow_module_level=True,
    )

_FIXED_NOW = datetime(2026, 7, 9, 9, 0, tzinfo=UTC)

# The pre-TK-177 baseline drain-graph stage set (byte-identical AC3 check), ADDITIVELY updated
# for TK-164 (Q-96): compose now transitions onward to the new "speak" terminal. Further
# ADDITIVELY updated (TK-229 un-staling) for the chat_reply and reflection_compose stages now
# built unconditionally into the drain graph regardless of google wiring (see bootstrap.py's
# two build_drain_pathway(...) branches, both of which include them).
_BASELINE_DRAIN_STAGES = frozenset(
    {
        "drain_queue",
        "gate",
        "review_or_speak",
        "compose_dispatch",
        "compose",
        "chat_reply",
        "speak",
        "reflection_compose",
    }
)


@pytest.fixture(autouse=True)
def _reset_singleton() -> None:
    bootstrap.reset_engine()


@pytest.fixture
def clean_tables() -> None:
    """Ensure every schema this composition touches exists, then truncate (mirrors
    ``test_serve_boot.py``'s ``clean_tables`` convention, plus the new trail table)."""
    assert _DSN is not None
    with psycopg.connect(_DSN) as conn:
        ensure_queue_schema(conn)
        ensure_daily_ledger_schema(conn)
        ensure_pending_journal_schema(conn)
        ensure_trail_schema(conn)
        with conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE wombat_queue")
            cur.execute("TRUNCATE TABLE daily_ledger")
            cur.execute("TRUNCATE TABLE pending_journal")
            cur.execute("TRUNCATE TABLE action_trail_projection")
        conn.commit()


@pytest.fixture()
def _no_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """TK-202 (Q-103)/TK-229: chdir off the repo root so pydantic-settings' ``env_file=".env"``
    resolution (relative to CWD) can never pick up the populated operator .env underneath a
    google-less ``_config()`` — mirrors ``tests/unit/test_runtime.py``'s own fixture of the same
    name. Opt-in only (not autouse) — requested by name from the ONE test that needs a
    structurally google-less, brief-less config regardless of whatever the operator's real .env
    stages (GOOGLE_OAUTH_*, WOMBAT_BRIEF_PATH, etc)."""
    monkeypatch.chdir(tmp_path)


def _config(*, with_google: bool = False) -> WombatConfig:
    """Mirrors ``test_serve_boot.py``'s ``_config()`` — an unreachable DeepSeek base_url so the
    mouth degrades cleanly to the terse template, never a real network call."""
    if not with_google:
        return WombatConfig(
            deepseek_api_key="dummy-not-real-key", deepseek_base_url="https://x.test"
        )
    return WombatConfig(
        deepseek_api_key="dummy-not-real-key",
        deepseek_base_url="https://x.test",
        google_oauth_client_id="test-client-id",
        google_oauth_client_secret=SecretStr("test-client-secret"),
    )


def _initial_artifact() -> Artifact:
    return Artifact(
        kind="drain-tick",
        produced_by="test",
        provenance=Provenance(source="system", confidence=1.0, recorded_at=datetime.now(UTC)),
        data={},
    )


# ============================================================================================
# AC1 — WIRE 1: emission + idempotency (GmailWithReplyIntents)
# ============================================================================================


@dataclass
class _FakePoller:
    """A minimal ``InputSource`` returning the SAME fixed events on every ``poll()`` call."""

    id: str
    poll_interval_seconds: float
    events: list[SourceEvent] = field(default_factory=list)

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def poll(self) -> list[SourceEvent]:
        return list(self.events)


def _high_message() -> GmailMessageItem:
    return GmailMessageItem(
        message_id="m-high-1",
        subject="URGENT: contract needs your reply",
        sender="a@example.com",
        received_at=_FIXED_NOW,
        body_text="please get back to me today",
    )


def _normal_message() -> GmailMessageItem:
    return GmailMessageItem(
        message_id="m-normal-1",
        subject="weekly newsletter",
        sender="noreply@example.com",
        received_at=_FIXED_NOW,
        body_text="here is your weekly digest",
    )


async def test_ac1_high_triage_message_emits_message_and_draft_item_idempotently(
    clean_tables: None,
) -> None:
    assert _DSN is not None
    queue = WombatQueue(_DSN, max_size=10)
    try:
        high = _high_message()
        fake_poller = _FakePoller(
            id="gmail",
            poll_interval_seconds=300.0,
            events=[SourceEvent(event_key=high.message_id, payload=high.to_payload())],
        )
        source = GmailWithReplyIntents(wrapped=fake_poller, rules=load_triage_rules())
        assert source.id == "gmail"

        # ONE poll tick.
        events = await source.poll()
        assert len(events) == 2
        for event in events:
            queue.enqueue(
                QueueItem(
                    idempotency_key=derive_key(source.id, event.event_key), payload=event.payload
                )
            )

        rows = queue.drain()
        assert len(rows) == 2
        message_rows = [r for r in rows if r.payload.get("item_kind") != "draft"]
        draft_rows = [r for r in rows if r.payload.get("item_kind") == "draft"]
        assert len(message_rows) == 1
        assert len(draft_rows) == 1
        assert message_rows[0].idempotency_key == derive_key("gmail", high.message_id)
        assert draft_rows[0].idempotency_key == derive_key("gmail", f"reply:{high.message_id}")
        assert draft_rows[0].payload["message_id"] == high.message_id
        assert draft_rows[0].payload["recipient"] == high.sender

        # A second poll of the SAME message enqueues NOTHING new (ALREADY_QUEUED on both).
        events2 = await source.poll()
        results = [
            queue.enqueue(
                QueueItem(
                    idempotency_key=derive_key(source.id, event.event_key), payload=event.payload
                )
            )
            for event in events2
        ]
        assert results == [EnqueueResult.ALREADY_QUEUED, EnqueueResult.ALREADY_QUEUED]
    finally:
        queue.close()


async def test_ac1_normal_band_message_emits_no_draft_item(clean_tables: None) -> None:
    assert _DSN is not None
    queue = WombatQueue(_DSN, max_size=10)
    try:
        normal = _normal_message()
        fake_poller = _FakePoller(
            id="gmail",
            poll_interval_seconds=300.0,
            events=[SourceEvent(event_key=normal.message_id, payload=normal.to_payload())],
        )
        source = GmailWithReplyIntents(wrapped=fake_poller, rules=load_triage_rules())

        events = await source.poll()
        assert len(events) == 1  # the message item ONLY — no draft item for a NORMAL band

        for event in events:
            queue.enqueue(
                QueueItem(
                    idempotency_key=derive_key(source.id, event.event_key), payload=event.payload
                )
            )

        rows = queue.drain()
        assert len(rows) == 1
        assert rows[0].payload.get("item_kind") != "draft"
    finally:
        queue.close()


# ============================================================================================
# AC2 — WIRE 2/3: end-to-end park + approve through the REAL wired drain graph
# ============================================================================================


class _FakeCredentials:
    """A sentinel standing in for a real ``google.oauth2.credentials.Credentials``."""


class _FakeGmailAuth:
    def __init__(self, *, config: WombatConfig, token_store: Any = None) -> None:
        pass

    def get_credentials(self) -> _FakeCredentials:
        return _FakeCredentials()


class _FakeDraftResponse:
    def __init__(self, body: dict[str, Any]) -> None:
        self._body = body

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._body


class _FakeDraftSession:
    """The fake ``gmail.drafts.create`` HTTP session — spies on every ``.post()`` call."""

    def __init__(self, credentials: Any) -> None:
        self.credentials = credentials
        self.calls: list[tuple[str, dict[str, Any], float]] = []

    def post(self, url: str, *, json: dict[str, Any], timeout: float) -> _FakeDraftResponse:
        self.calls.append((url, json, timeout))
        return _FakeDraftResponse({"id": "draft-live-1"})


class _FakeTokenStore:
    def __init__(self, *, initial: str | None = None) -> None:
        self._value = initial

    def load(self) -> str | None:
        return self._value

    def save(self, token: str) -> None:
        self._value = token

    def clear(self) -> None:
        self._value = None


def _force_active_presence(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force ``wombat.sources.presence.read_idle_ms`` (the ONE impure OS-idle read) to report
    zero idle time — presence is otherwise a genuine environment-dependent signal (a build host
    that is actually idle would HOLD every item, never surfacing; TK-177 proves the drain WIRING,
    not the presence heuristic TK-11 already owns)."""
    monkeypatch.setattr(presence_module, "read_idle_ms", lambda: 0)


def _fake_gmail_session(monkeypatch: pytest.MonkeyPatch) -> _FakeDraftSession:
    """Fakes Gmail auth/session at the SAME sanctioned module boundary
    ``tests/sources/test_bootstrap.py`` uses — the live HTTP path is never exercised here."""
    fake_session = _FakeDraftSession(credentials=None)
    monkeypatch.setattr(gmail_session_module, "GmailAuth", _FakeGmailAuth)
    monkeypatch.setattr(gmail_session_module, "AuthorizedSession", lambda creds: fake_session)
    return fake_session


def _draft_queue_item(reply: ReplyIntent) -> QueueItem:
    """A draft item's payload, PLUS the scoring signals (``is_timed``/``seconds_to_event``/
    ``sender_class``) that clear the real Gate's audited urgency bar deterministically — mirrors
    ``test_drain_pathway_e2e.py``'s own real-gate VIP-item construction. Extra keys are ignored by
    ``ReplyIntent.from_payload`` (it reads named keys only), so this is a lossless superset."""
    payload = reply.to_payload()
    payload.update({"is_timed": True, "seconds_to_event": 0.0, "sender_class": "vip"})
    return QueueItem(
        idempotency_key=derive_key("gmail", f"reply:{reply.message_id}"), payload=payload
    )


async def test_ac2_draft_surfaces_parks_and_approve_dispatches_with_one_capability_call(
    clean_tables: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _DSN is not None
    _force_active_presence(monkeypatch)
    fake_session = _fake_gmail_session(monkeypatch)

    op = load_operating_params()
    bundle = bootstrap.assemble_runtime(
        config=_config(with_google=True),
        dsn=_DSN,
        params=op,
        tz=ZoneInfo("UTC"),
        gmail_token_store=_FakeTokenStore(initial="fake-stored-token"),
        gcal_token_store=_FakeTokenStore(initial="fake-stored-token"),
    )
    try:
        reply = ReplyIntent(
            recipient="jane@example.com",
            subject_or_thread_ref="Q3 budget",
            reply_kind="high",
            quoted_excerpt="Quick update on the budget.",
            message_id="m1",
            matched_rules=("urgent-keyword",),
        )
        bundle.queue.enqueue(_draft_queue_item(reply))

        run_id = "ac2-run"
        parked = await bundle.engine.run(
            run_id=run_id,
            session_id=run_id,
            pathway_id=bundle.drain_pathway_id,
            initial=_initial_artifact(),
        )

        assert parked.status is RunStatus.AWAITING_HUMAN
        # DraftComposer's ONE pre-park dispatch already happened.
        assert len(fake_session.calls) == 1
        _url, body, _timeout = fake_session.calls[0]
        assert body["message"]["raw"]  # a base64url RFC 5322 message was built

        # ask_step_index PROOF: the drain graph is drain_queue(0)/gate(1)/review_or_speak(2)/
        # compose_dispatch(3)/draft_composer(4) for a fresh single-item drive — the parked step's
        # OWN recorded position is the authoritative proof (not a re-derivation of bootstrap
        # internals).
        park_step = next(s for s in parked.steps if s.stage_name == "draft_composer")
        assert park_step.step_index == 4

        final = await bundle.engine.provide_human_input(run_id, payload={"decision": "approve"})
        assert final.status is RunStatus.COMPLETED
        # ZERO further capability calls attributable to draft_dispatch — spy count stays 1.
        assert len(fake_session.calls) == 1

        reader = ActionTrailReader(_DSN)
        try:
            rows = reader.rows()
            action_row = next(r for r in rows if r.action_id == f"{run_id}:draft_composer")
            assert action_row.status == "dispatched"
        finally:
            reader.close()
    finally:
        bundle.queue.close()
        bundle.daily_ledger.close()
        bundle.pending_journal.close()


# ============================================================================================
# TK-179 AC1/AC2 (CR2-2, Q-94) — the stage-identity lookup repro: the register's exact failure
# scenario, rebased onto the DEC-41/TK-255 empty-queue-completes contract. The drain graph is
# strictly linear (drain_queue(0)/gate(1)/review_or_speak(2)/compose_dispatch(3)/
# draft_composer(4)) and an empty-queue run now COMPLETES rather than self-parking, so a
# terminal run can never be ``fire_timer``'d into a HIGHER park position any more — index 4 is
# the ONLY reachable park for a draft surfaced on a fresh run. DraftDispatchStage must still
# locate the parked step by STAGE IDENTITY (walking the run's committed step history for the
# last "draft_composer" step), not a precomputed index, or the real approval reads None, writes
# a false refusal, and strands the run RUNNING.
# ============================================================================================


async def _idle_then_surface_draft(
    bundle: bootstrap.RuntimeBundle, run_id: str, reply: ReplyIntent
) -> RunState:
    """Enqueue the draft item BEFORE the run ever starts, then drive a single fresh run —
    post-DEC-41 an empty queue is no longer reachable as an idle detour (it COMPLETES, it does
    not self-park), so this parks AWAITING_HUMAN directly at the fresh-run position (TK-255)."""
    bundle.queue.enqueue(_draft_queue_item(reply))

    surfaced = await bundle.engine.run(
        run_id=run_id,
        session_id=run_id,
        pathway_id=bundle.drain_pathway_id,
        initial=_initial_artifact(),
    )
    return surfaced


async def test_tk179_ac1_idled_drain_approve_dispatches_via_stage_identity_lookup_pg(
    clean_tables: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _DSN is not None
    _force_active_presence(monkeypatch)
    fake_session = _fake_gmail_session(monkeypatch)

    op = load_operating_params()
    bundle = bootstrap.assemble_runtime(
        config=_config(with_google=True),
        dsn=_DSN,
        params=op,
        tz=ZoneInfo("UTC"),
        gmail_token_store=_FakeTokenStore(initial="fake-stored-token"),
        gcal_token_store=_FakeTokenStore(initial="fake-stored-token"),
    )
    try:
        reply = ReplyIntent(
            recipient="jane@example.com",
            subject_or_thread_ref="Q3 budget",
            reply_kind="high",
            quoted_excerpt="Quick update on the budget.",
            message_id="m-idled-approve",
            matched_rules=("urgent-keyword",),
        )
        run_id = "tk179-ac1-run"

        surfaced = await _idle_then_surface_draft(bundle, run_id, reply)
        assert surfaced.status is RunStatus.AWAITING_HUMAN
        assert len(fake_session.calls) == 1
        # DEC-41/TK-255: the drain graph is strictly linear (drain_queue(0)/gate(1)/
        # review_or_speak(2)/compose_dispatch(3)/draft_composer(4)); an empty-queue run now
        # COMPLETES rather than self-parking, so a terminal run can never be fire_timer'd to a
        # higher position — index 4 is the ONLY reachable park.
        park_step = next(s for s in surfaced.steps if s.stage_name == "draft_composer")
        assert park_step.step_index == 4

        final = await bundle.engine.provide_human_input(run_id, payload={"decision": "approve"})

        assert final.status is RunStatus.COMPLETED
        # ZERO further capability calls attributable to draft_dispatch — spy count stays 1.
        assert len(fake_session.calls) == 1

        reader = ActionTrailReader(_DSN)
        try:
            rows = reader.rows()
            action_row = next(r for r in rows if r.action_id == f"{run_id}:draft_composer")
            # DISPATCHED (not stuck PENDING) is the observable proof of "zero refusals": a
            # refusal targets the SAME action_id via INSERT ... ON CONFLICT DO NOTHING, so a
            # structural refusal here would leave this row silently stranded at PENDING forever
            # rather than adding a second row — DISPATCHED is only reachable via mark_dispatched,
            # which the stage calls ONLY after successfully reading the approve decision.
            assert action_row.status == "dispatched"
        finally:
            reader.close()
    finally:
        bundle.queue.close()
        bundle.daily_ledger.close()
        bundle.pending_journal.close()


async def test_tk179_ac2_idled_drain_reject_cancels_via_stage_identity_lookup_pg(
    clean_tables: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _DSN is not None
    _force_active_presence(monkeypatch)
    fake_session = _fake_gmail_session(monkeypatch)

    op = load_operating_params()
    bundle = bootstrap.assemble_runtime(
        config=_config(with_google=True),
        dsn=_DSN,
        params=op,
        tz=ZoneInfo("UTC"),
        gmail_token_store=_FakeTokenStore(initial="fake-stored-token"),
        gcal_token_store=_FakeTokenStore(initial="fake-stored-token"),
    )
    try:
        reply = ReplyIntent(
            recipient="jane@example.com",
            subject_or_thread_ref="Q3 budget",
            reply_kind="high",
            quoted_excerpt="Quick update on the budget.",
            message_id="m-idled-reject",
            matched_rules=("urgent-keyword",),
        )
        run_id = "tk179-ac2-run"

        surfaced = await _idle_then_surface_draft(bundle, run_id, reply)
        assert surfaced.status is RunStatus.AWAITING_HUMAN
        assert len(fake_session.calls) == 1
        # DEC-41/TK-255: index 4 is the ONLY reachable park (see AC1's comment above).
        park_step = next(s for s in surfaced.steps if s.stage_name == "draft_composer")
        assert park_step.step_index == 4

        final = await bundle.engine.provide_human_input(run_id, payload={"decision": "reject"})

        assert final.status is RunStatus.COMPLETED
        assert len(fake_session.calls) == 1  # unchanged — no further dispatch

        reader = ActionTrailReader(_DSN)
        try:
            rows = reader.rows()
            action_row = next(r for r in rows if r.action_id == f"{run_id}:draft_composer")
            assert action_row.status == "cancelled"
        finally:
            reader.close()
    finally:
        bundle.queue.close()
        bundle.daily_ledger.close()
        bundle.pending_journal.close()


# ============================================================================================
# AC3 — WIRE 2/3: Google-less loud-skip, drain + brief pathways byte-identical to baseline
# ============================================================================================


async def test_ac3_google_less_boot_loud_skips_outbound_wiring(
    clean_tables: None, _no_env_file: None, caplog: pytest.LogCaptureFixture
) -> None:
    assert _DSN is not None
    op = load_operating_params()

    with caplog.at_level("WARNING"):
        bundle = bootstrap.assemble_runtime(
            config=_config(with_google=False), dsn=_DSN, params=op, tz=ZoneInfo("UTC")
        )
    try:
        assert "gmail outbound wiring not wired" in caplog.text
        assert "GOOGLE_OAUTH_CLIENT_ID" in caplog.text

        assert bundle.drain_pathway_id == "wombat.drain"
        graph = bundle.pathways.get(bundle.drain_pathway_id)
        assert set(graph.names()) == _BASELINE_DRAIN_STAGES

        # The brief pathway's own registration is unaffected — pre-TK-177 baseline: skipped
        # (blank WOMBAT_BRIEF_PATH), same as it always was.
        assert bundle.brief_pathway_id is None
    finally:
        bundle.queue.close()
        bundle.daily_ledger.close()
        bundle.pending_journal.close()
