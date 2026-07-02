"""SubstrateBundle — the four cog-worx Engine substrate seams, assembled for TK-1 (Q-29).

``Engine.__init__`` requires four seams with no defaults: ``journal``, ``graph_store``,
``latent`` (substrate Protocols) and ``pathways`` (a ``PathwayRegistry``). This module
constructs them as ONE ``SubstrateBundle`` so the composition root (TK-1) stays pure wiring.

Two factories behind the same bundle shape:
  - ``cold_boot_bundle()`` — cog-worx in-memory doubles, ZERO infra (the v1 default).
  - ``real_adapter_bundle(config, residency_check=...)`` — the durable Timescale/Neo4j/Pg
    adapters, endpoint values supplied by TK-13's OperatingParams, each passed through an
    injected residency check (TK-150 owns the predicate) BEFORE any adapter is built.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from cogworx.loop.pathway import PathwayRegistry
from cogworx.substrate.graph_store import GraphStore
from cogworx.substrate.journal import Journal
from cogworx.substrate.latent import LatentStore

# Raises if the endpoint does not resolve to the same host (TK-150 supplies the real predicate).
ResidencyCheck = Callable[[str], None]


@dataclass(frozen=True, slots=True)
class SubstrateBundle:
    """The single substrate input TK-1 consumes. Both factories return this exact shape."""

    journal: Journal
    graph_store: GraphStore
    latent: LatentStore
    pathways: PathwayRegistry


@dataclass(frozen=True, slots=True)
class SubstrateConfig:
    """Endpoint values for the real-adapter path. Owned/persisted by TK-13's OperatingParams.

    Deliberately NOT cog-worx's ``adapters.config.SubstrateSettings`` (a BaseSettings that reads
    env directly) — that would bypass TK-13 as the single param owner. ``pg_dsn`` backs both the
    Timescale journal and the pgvector latent store (same Postgres host).
    """

    pg_dsn: str
    neo4j_uri: str
    neo4j_user: str
    neo4j_password: str
    latent_dim: int


def cold_boot_bundle() -> SubstrateBundle:
    """The v1 default: cog-worx in-memory doubles + an empty PathwayRegistry. Zero infra."""
    from cogworx.testing.doubles import (
        InMemoryGraphStore,
        InMemoryJournal,
        InMemoryLatentStore,
    )

    return SubstrateBundle(
        journal=InMemoryJournal(),
        graph_store=InMemoryGraphStore(),
        latent=InMemoryLatentStore(),
        pathways=PathwayRegistry(),
    )


def real_adapter_bundle(
    config: SubstrateConfig, *, residency_check: ResidencyCheck
) -> SubstrateBundle:
    """The durable path. Runs residency_check on every endpoint BEFORE constructing any adapter."""
    from cogworx.adapters.neo4j_graph import Neo4jGraphStore
    from cogworx.adapters.pg_latent import PgLatentStore
    from cogworx.adapters.timescale_journal import TimescaleJournal

    # Residency gate first — refuse an off-host endpoint before anything is constructed/connected.
    for endpoint in (config.pg_dsn, config.neo4j_uri):
        residency_check(endpoint)

    return SubstrateBundle(
        journal=TimescaleJournal(dsn=config.pg_dsn),
        graph_store=Neo4jGraphStore(
            uri=config.neo4j_uri, user=config.neo4j_user, password=config.neo4j_password
        ),
        latent=PgLatentStore(dsn=config.pg_dsn, dim=config.latent_dim),
        pathways=PathwayRegistry(),
    )


def build_substrate(
    config: SubstrateConfig | None = None, *, residency_check: ResidencyCheck | None = None
) -> SubstrateBundle:
    """Mode selector. No config -> cold-boot (the default); a config -> the real-adapter path."""
    if config is None:
        return cold_boot_bundle()
    if residency_check is None:
        raise ValueError("real-adapter mode requires a residency_check (TK-150 predicate)")
    return real_adapter_bundle(config, residency_check=residency_check)
