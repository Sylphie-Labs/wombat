"""TK-150 — local-residency assertion acceptance criteria (EP-28, Q-87).

NG-7/CON-7/ASMP-1 made structural: every persistence write must target THIS host; the DeepSeek
phrasing call is the ONE allowed egress.

  AC1 table-driven residency_check battery (unit, no network) + a pg-gated recording-wrapper
      battery over the real write surfaces (queue enqueue, DailyLedger increment, pending-
      journal add, trail record_proposal) asserting every psycopg.connect DSN passes
      residency_check.
  AC2 the ONE egress: a real ComposeStage drive over a real bootstrap-wired DeepSeek Model,
      httpx-transport-intercepted — exactly one outbound host, zero elsewhere.
  AC3 adversarial startup: check_config refuses an off-host wombat_pg_dsn (naming the key),
      accepts a same-host non-localhost peer, and serve() aborts before assemble_runtime.
  AC4 token custody: a KeyringTokenStore save/load over a fake vault, httpx-intercepted, never
      puts the token on the wire.
  AC5 the TK-54 consumer seam: residency_check satisfies substrate.ResidencyCheck and
      build_substrate refuses an off-host endpoint before any adapter is imported/constructed.

ALL DB tests (AC1's second half) require a REAL Postgres and are gated on ``WOMBAT_TEST_PG_DSN``
(skipped LOUDLY, never faked, when absent). Spin up a throwaway Postgres locally:

    docker run --rm -d -p 5433:5432 -e POSTGRES_PASSWORD=wombat postgres:16
    WOMBAT_TEST_PG_DSN=postgresql://postgres:wombat@localhost:5433/postgres

Every other test in this module is pure/hermetic — no network, no Postgres.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, cast
from zoneinfo import ZoneInfo

import httpx
import psycopg
import pytest
from cogworx.claims.provenance import Artifact, Provenance
from cogworx.cost.budget import BudgetPolicy
from cogworx.loop.result import Done
from cogworx.loop.stage import StageContext
from cogworx.model.base import Model
from pydantic import SecretStr

import wombat.bootstrap as bootstrap_module
import wombat.runtime as runtime_module
import wombat.safety.local_residency as local_residency
from wombat.compose.templates import TemplateComposer
from wombat.config import WombatConfig
from wombat.domain.daily_ledger import DailyLedger
from wombat.domain.daily_ledger import ensure_schema as ensure_ledger_schema
from wombat.gate.models import ItemKind
from wombat.gate.pending_journal_pg import PgPendingJournal
from wombat.gate.pending_journal_pg import ensure_schema as ensure_journal_schema
from wombat.gate.pending_set import PendingSetAdd
from wombat.queue import EnqueueResult, QueueItem, WombatQueue
from wombat.queue import ensure_schema as ensure_queue_schema
from wombat.safety.local_residency import (
    RemoteStorageConfigError,
    check_config,
    make_residency_check,
    residency_check,
)
from wombat.stages.artifacts import (
    COMPOSE_REQUEST,
    compose_request_to_artifact_data,
    composed_output_from_artifact_data,
)
from wombat.stages.compose import ComposeStage
from wombat.substrate import ResidencyCheck, SubstrateConfig, build_substrate
from wombat.trail.schema import ActionType
from wombat.trail.writer import ActionTrailWriter
from wombat.trail.writer import ensure_schema as ensure_trail_schema

_FIXED_NOW = datetime(2026, 7, 9, 12, 0, 0, tzinfo=UTC)


def _config(
    *, wombat_pg_dsn: str | None = None, deepseek_base_url: str = "https://x.example"
) -> WombatConfig:
    return WombatConfig(
        deepseek_api_key=SecretStr("sk-test"),
        deepseek_base_url=deepseek_base_url,
        wombat_pg_dsn=wombat_pg_dsn,
    )


# ------------------------------------------------------------------------------------------ AC1


@pytest.mark.parametrize(
    ("endpoint", "resolver_table", "local_addrs"),
    [
        # loopback literals — pass with no resolver/local-addrs consultation at all
        ("postgresql://127.0.0.1:5432/wombat", {}, []),
        ("bolt://[::1]:7687", {}, []),
        # the literal "localhost" — pass regardless of local_addrs
        ("postgresql://localhost:5432/wombat", {}, []),
        # unix-domain socket forms — inherently local, no host to check
        ("postgresql:///wombat?host=/var/run/postgresql", {}, []),
        ("/var/run/postgresql/.s.PGSQL.5432", {}, []),
        ("host=/var/run/postgresql dbname=wombat user=wombat", {}, []),
        # a fake service name (Docker-bridge-style) resolving to one of THIS host's own addrs
        (
            "postgresql://wombat-pg:5432/wombat",
            {"wombat-pg": ["10.0.5.7"]},
            ["10.0.5.7"],
        ),
    ],
)
def test_ac1_accepted_endpoints_pass(
    endpoint: str, resolver_table: dict[str, list[str]], local_addrs: list[str]
) -> None:
    check = make_residency_check(
        resolver=lambda host: resolver_table.get(host, []), local_addrs=lambda: local_addrs
    )
    check(endpoint)  # must not raise


@pytest.mark.parametrize(
    ("endpoint", "resolver_table", "local_addrs"),
    [
        # a LAN IP literal that is NOT one of this host's own addresses
        ("postgresql://192.168.50.9:5432/wombat", {}, ["10.0.5.7"]),
        # a hostname resolving to a genuinely different host
        (
            "postgresql://db.example.com:5432/wombat",
            {"db.example.com": ["203.0.113.9"]},
            ["10.0.5.7"],
        ),
    ],
)
def test_ac1_off_host_endpoints_are_refused_naming_the_endpoint(
    endpoint: str, resolver_table: dict[str, list[str]], local_addrs: list[str]
) -> None:
    check = make_residency_check(
        resolver=lambda host: resolver_table.get(host, []), local_addrs=lambda: local_addrs
    )
    with pytest.raises(RemoteStorageConfigError, match=endpoint.split("://")[-1].split("/")[0].split(":")[0]):
        check(endpoint)


# ------------------------------------------------------------------------------- TK-178 (CR2-1)


@pytest.mark.parametrize(
    "endpoint",
    [
        "postgresql://localhost/wombat?hostaddr=8.8.8.8",
        "host=localhost hostaddr=8.8.8.8 dbname=wombat",
        "hostaddr=8.8.8.8 dbname=wombat",
    ],
)
def test_tk178_hostaddr_forms_are_refused_naming_wombat_pg_dsn(endpoint: str) -> None:
    """AC1: the register's three exact repro DSNs — libpq/psycopg dial ``hostaddr=`` when
    present, so a ``host=localhost``/no-host DSN carrying an off-host ``hostaddr=`` must be
    refused, not pass on the strength of ``host`` (or the unix-socket default)."""
    config = _config(wombat_pg_dsn=endpoint)
    with pytest.raises(RemoteStorageConfigError, match="wombat_pg_dsn"):
        check_config(config)


@pytest.mark.parametrize(
    "endpoint",
    [
        "postgresql://localhost/wombat?hostaddr=127.0.0.1",
        "host=localhost hostaddr=127.0.0.1 dbname=wombat",
        "hostaddr=127.0.0.1 dbname=wombat",
    ],
)
def test_tk178_hostaddr_same_host_is_accepted_with_or_without_host(endpoint: str) -> None:
    """AC2: ``hostaddr=127.0.0.1``, with or without a ``host=`` alongside it, is accepted —
    no false refusal of a genuinely same-host endpoint."""
    check_config(_config(wombat_pg_dsn=endpoint))  # no raise


@pytest.mark.parametrize(
    ("endpoint", "expected_host"),
    [
        ("postgresql://localhost/wombat?hostaddr=8.8.8.8", "8.8.8.8"),
        ("host=localhost hostaddr=8.8.8.8 dbname=wombat", "8.8.8.8"),
        ("hostaddr=8.8.8.8 dbname=wombat", "8.8.8.8"),
        ("postgresql://localhost/wombat?hostaddr=127.0.0.1", "127.0.0.1"),
        ("hostaddr=127.0.0.1 dbname=wombat", "127.0.0.1"),
    ],
)
def test_tk178_extract_host_prefers_hostaddr_over_host(
    endpoint: str, expected_host: str
) -> None:
    """The module-private extraction helper resolves ``hostaddr`` (the real dial target) over
    ``host`` — pinned directly so the precedence rule doesn't silently regress."""
    assert local_residency._extract_host(endpoint) == expected_host


_DSN = os.environ.get("WOMBAT_TEST_PG_DSN")

_requires_pg = pytest.mark.skipif(
    not _DSN,
    reason=(
        "WOMBAT_TEST_PG_DSN is not set — skipping the TK-150 pg-touching write-surface "
        "residency battery, which requires a real throwaway Postgres. Start one with:\n"
        "  docker run --rm -d -p 5433:5432 -e POSTGRES_PASSWORD=wombat postgres:16\n"
        "then export WOMBAT_TEST_PG_DSN=postgresql://postgres:wombat@localhost:5433/postgres"
    ),
)


@pytest.fixture
def clean_tables() -> None:
    """Ensure all four surfaces' schemas exist and their tables are empty before each test."""
    assert _DSN is not None
    with psycopg.connect(_DSN) as conn:
        ensure_queue_schema(conn)
        ensure_ledger_schema(conn)
        ensure_journal_schema(conn)
        ensure_trail_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                "TRUNCATE TABLE wombat_queue, daily_ledger, pending_journal, "
                "action_trail_projection"
            )
        conn.commit()


def _recording_psycopg_connect(sink: list[str]) -> Callable[..., psycopg.Connection[Any]]:
    real_connect = psycopg.connect

    def _wrapper(conninfo: str, *args: Any, **kwargs: Any) -> psycopg.Connection[Any]:
        sink.append(conninfo)
        return real_connect(conninfo, *args, **kwargs)

    return _wrapper


@_requires_pg
def test_ac1_every_pg_touching_write_surface_passes_its_connect_dsn_through_residency_check(
    clean_tables: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drives queue enqueue, DailyLedger increment, pending-journal add, and trail
    record_proposal over a recording wrapper around ``psycopg.connect`` — every DSN any of
    them actually connected with must pass ``residency_check`` cleanly."""
    assert _DSN is not None
    recorded: list[str] = []
    monkeypatch.setattr(psycopg, "connect", _recording_psycopg_connect(recorded))

    queue = WombatQueue(_DSN, max_size=10)
    ledger = DailyLedger(_DSN, tz=ZoneInfo("UTC"))
    journal = PgPendingJournal(_DSN)
    trail = ActionTrailWriter(_DSN)
    try:
        result = queue.enqueue(QueueItem(idempotency_key="tk150-k1", payload={}))
        assert result == EnqueueResult.QUEUED
        ledger.increment("tk150:test")
        journal.append(
            PendingSetAdd(item_id="tk150-x", item_kind=ItemKind.GENERIC, urgency=0.1, load=0.1)
        )
        trail.record_proposal(
            action_id="tk150-a1",
            action_type=ActionType.DRAFT_EMAIL,
            human_summary="test",
            target="test@example.com",
            proposed_at=datetime.now(UTC),
        )
    finally:
        queue.close()
        ledger.close()
        journal.close()
        trail.close()

    assert recorded  # at least one real connect happened across the four surfaces
    for dsn in recorded:
        residency_check(dsn)  # must NOT raise — every connect's DSN is same-host


# ------------------------------------------------------------------------------------------ AC2


class _RealModelStageContext:
    """A minimal ``StageContext`` double exposing ONLY ``model``/``last_output``/``clock`` —
    exactly what ``ComposeStage`` touches (per its own module docstring) — backed by a REAL,
    bootstrap-wired ``Model`` chain, so this drives the genuine production call path down to
    the httpx transport (AC2)."""

    def __init__(
        self, *, model: Model, last_output_map: Mapping[str, Artifact | None], now: datetime
    ) -> None:
        self._model = model
        self._last_output_map = last_output_map
        self._now = now
        self.run_id = "tk150-ac2"
        self.session_id = "tk150-ac2"

    @property
    def model(self) -> Model:
        return self._model

    @property
    def clock(self) -> Callable[[], datetime]:
        return lambda: self._now

    async def last_output(self, stage_name: str) -> Artifact | None:
        return self._last_output_map.get(stage_name)


def _canned_chat_completion(request: httpx.Request) -> httpx.Response:
    payload = {
        "id": "chatcmpl-tk150",
        "object": "chat.completion",
        "created": 0,
        "model": "deepseek-chat",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "ok"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }
    return httpx.Response(200, json=payload, request=request)


async def test_ac2_only_the_configured_deepseek_host_is_ever_dialed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real ``ComposeStage`` drive over a real bootstrap-wired Model chain, with the httpx
    transport intercepted (the layer the openai SDK rides) — asserts exactly ONE outbound host
    (``config.deepseek_base_url``'s host) is ever dialed, zero elsewhere, zero live egress."""
    fake_host = "fake-deepseek-tk150.invalid"
    config = _config(deepseek_base_url=f"https://{fake_host}/v1")

    dialed_hosts: list[str | None] = []

    async def fake_handle_async_request(
        _self: httpx.AsyncHTTPTransport, request: httpx.Request
    ) -> httpx.Response:
        dialed_hosts.append(request.url.host)
        return _canned_chat_completion(request)

    monkeypatch.setattr(
        httpx.AsyncHTTPTransport, "handle_async_request", fake_handle_async_request
    )

    registry = bootstrap_module._deepseek_registry(config)
    guard = BudgetPolicy().new_guard()
    model = registry.assemble(bootstrap_module.MODEL_PROFILE, guard=guard)

    compose_request = Artifact(
        kind=COMPOSE_REQUEST,
        produced_by="compose_dispatch",
        provenance=Provenance(source="system", confidence=1.0, recorded_at=_FIXED_NOW),
        data=compose_request_to_artifact_data(
            "i-1", ItemKind.GENERIC, {"subject": "hi", "sender": "a@b.com"}
        ),
    )
    ctx = cast(
        StageContext,
        _RealModelStageContext(
            model=model, last_output_map={"compose_dispatch": compose_request}, now=_FIXED_NOW
        ),
    )
    stage = ComposeStage(config=config, template_composer=TemplateComposer())

    result = await stage.run(ctx)

    assert isinstance(result, Done)
    text, _item_id, _item_kind, degraded = composed_output_from_artifact_data(result.output.data)
    assert degraded is False  # the real (faked) call succeeded — never fell back to the template
    assert text == "ok"
    assert dialed_hosts == [fake_host]  # exactly one outbound host, ZERO to anywhere else


# ------------------------------------------------------------------------------------------ AC3


def test_ac3_check_config_accepts_when_pg_dsn_absent() -> None:
    check_config(_config(wombat_pg_dsn=None))  # no raise


def test_ac3_check_config_refuses_off_host_pg_dsn_naming_the_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_check = make_residency_check(
        resolver=lambda host: {"remote-db.example.com": ["203.0.113.9"]}.get(host, []),
        local_addrs=lambda: ["10.0.0.5"],
    )
    monkeypatch.setattr(local_residency, "residency_check", fake_check)

    config = _config(wombat_pg_dsn="postgresql://remote-db.example.com/wombat")
    with pytest.raises(RemoteStorageConfigError, match="wombat_pg_dsn"):
        check_config(config)


def test_ac3_check_config_accepts_a_same_host_non_localhost_peer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Docker-bridge-to-self peer (resolves to one of THIS host's own addrs, via the
    injected resolver) is ACCEPTED — same-host, not the literal 'localhost' (Q-25)."""
    fake_check = make_residency_check(
        resolver=lambda host: {"wombat-pg.internal": ["172.19.0.3"]}.get(host, []),
        local_addrs=lambda: ["172.19.0.3"],
    )
    monkeypatch.setattr(local_residency, "residency_check", fake_check)

    config = _config(wombat_pg_dsn="postgresql://wombat-pg.internal/wombat")
    check_config(config)  # no raise


async def test_ac3_serve_refuses_before_assemble_runtime_when_check_config_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://x.example")
    monkeypatch.setenv("WOMBAT_PG_DSN", "postgresql://localhost/wombat")

    def _raiser(config: WombatConfig) -> None:
        raise RemoteStorageConfigError("boom")

    monkeypatch.setattr(runtime_module, "check_config", _raiser)

    assembled: list[str] = []
    monkeypatch.setattr(
        runtime_module, "assemble_runtime", lambda **_kw: assembled.append("assembled")
    )

    with pytest.raises(RemoteStorageConfigError, match="boom"):
        await runtime_module.serve()

    assert assembled == []  # check_config's refusal aborted BEFORE assemble_runtime ran


# ------------------------------------------------------------------------------------------ AC4


class _FakeTokenStoreVault:
    """The TK-71 fake-keyring-backend test pattern: an in-memory dict standing in for the OS
    credential vault, wired via ``keyring.set_password``/``get_password`` monkeypatches."""

    def __init__(self) -> None:
        self.vault: dict[tuple[str, str], str] = {}

    def set_password(self, service: str, account: str, password: str) -> None:
        self.vault[(service, account)] = password

    def get_password(self, service: str, account: str) -> str | None:
        return self.vault.get((service, account))


async def test_ac4_token_custody_never_puts_the_token_on_the_wire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import keyring

    from wombat.integrations.gcal.token_store import KeyringTokenStore

    fake_vault = _FakeTokenStoreVault()
    monkeypatch.setattr(keyring, "set_password", fake_vault.set_password)
    monkeypatch.setattr(keyring, "get_password", fake_vault.get_password)

    recorded_requests: list[httpx.Request] = []

    async def fake_handle_async_request(
        _self: httpx.AsyncHTTPTransport, request: httpx.Request
    ) -> httpx.Response:
        recorded_requests.append(request)
        return httpx.Response(200, json={}, request=request)

    monkeypatch.setattr(
        httpx.AsyncHTTPTransport, "handle_async_request", fake_handle_async_request
    )

    sentinel_token = "sentinel-tk150-super-secret-token-9f8e7d6c"  # a known fake, never real
    store = KeyringTokenStore()
    store.save(sentinel_token)
    loaded = store.load()

    assert loaded == sentinel_token
    assert fake_vault.vault  # retrievable from the fake vault ONLY
    token_bytes = sentinel_token.encode("utf-8")
    for request in recorded_requests:
        haystack = bytes(str(request.url), "utf-8") + bytes(str(request.headers), "utf-8")
        haystack += request.content
        assert token_bytes not in haystack


# ------------------------------------------------------------------------------------------ AC5


def test_ac5_residency_check_is_assignable_to_substrate_residency_check() -> None:
    check: ResidencyCheck = residency_check
    assert check is residency_check


def test_ac5_build_substrate_refuses_off_host_before_any_adapter_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reached: list[str] = []

    def _fail_if_reached(**_kwargs: object) -> Any:
        reached.append("adapter constructed")
        raise AssertionError("adapter construction reached past an off-host residency refusal")

    monkeypatch.setattr(
        "cogworx.adapters.timescale_journal.TimescaleJournal", _fail_if_reached
    )
    monkeypatch.setattr("cogworx.adapters.neo4j_graph.Neo4jGraphStore", _fail_if_reached)
    monkeypatch.setattr("cogworx.adapters.pg_latent.PgLatentStore", _fail_if_reached)

    config = SubstrateConfig(
        pg_dsn="postgresql://203.0.113.5/wombat",  # TEST-NET-3 — off-host, no DNS needed
        neo4j_uri="bolt://localhost:7687",
        neo4j_user="neo4j",
        neo4j_password="x",
        latent_dim=384,
    )
    with pytest.raises(RemoteStorageConfigError, match=r"203\.0\.113\.5"):
        build_substrate(config, residency_check=residency_check)

    assert reached == []
