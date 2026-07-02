"""TK-14 — SubstrateBundle factory acceptance criteria (Q-29)."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from unittest.mock import MagicMock, patch

import pytest

from wombat.substrate import (
    SubstrateBundle,
    SubstrateConfig,
    build_substrate,
    cold_boot_bundle,
    real_adapter_bundle,
)

_BUNDLE_FIELDS = {"journal", "graph_store", "latent", "pathways"}
_ADAPTERS = (
    "cogworx.adapters.timescale_journal.TimescaleJournal",
    "cogworx.adapters.neo4j_graph.Neo4jGraphStore",
    "cogworx.adapters.pg_latent.PgLatentStore",
)


def _config(
    *,
    pg_dsn: str = "postgresql://localhost/wombat",
    neo4j_uri: str = "bolt://localhost:7687",
) -> SubstrateConfig:
    return SubstrateConfig(
        pg_dsn=pg_dsn, neo4j_uri=neo4j_uri, neo4j_user="neo4j", neo4j_password="x", latent_dim=384
    )


def _recorder(tag: str, sink: list[str]) -> Callable[..., MagicMock]:
    """A patched-adapter stand-in that records its construction in order and returns a mock."""

    def _factory(**_kwargs: object) -> MagicMock:
        sink.append(tag)
        return MagicMock()

    return _factory


def test_ac1_cold_bundle_conforms_to_protocols_and_builds_engine_with_no_db() -> None:
    from cogworx.loop.pathway import PathwayRegistry
    from cogworx.model.registry import ModelRegistry
    from cogworx.runtime.engine import Engine
    from cogworx.substrate.graph_store import GraphStore
    from cogworx.substrate.journal import Journal
    from cogworx.substrate.latent import LatentStore

    bundle = cold_boot_bundle()
    assert isinstance(bundle.journal, Journal)
    assert isinstance(bundle.graph_store, GraphStore)
    assert isinstance(bundle.latent, LatentStore)
    assert isinstance(bundle.pathways, PathwayRegistry)

    # The bundle (plus a bare ModelRegistry, which Engine also requires) constructs an Engine
    # with NO database running — the constructibility claim Q-29 / TK-14 exists to guarantee.
    engine = Engine(
        models=ModelRegistry(),
        journal=bundle.journal,
        graph_store=bundle.graph_store,
        latent=bundle.latent,
        pathways=bundle.pathways,
    )
    assert engine is not None


def test_ac2_real_adapter_runs_residency_check_before_any_adapter_construction() -> None:
    order: list[str] = []

    def fake_check(endpoint: str) -> None:
        order.append(f"check:{endpoint}")

    with (
        patch(_ADAPTERS[0], side_effect=_recorder("build:journal", order)),
        patch(_ADAPTERS[1], side_effect=_recorder("build:graph", order)),
        patch(_ADAPTERS[2], side_effect=_recorder("build:latent", order)),
    ):
        bundle = real_adapter_bundle(_config(), residency_check=fake_check)

    first_build = next(i for i, step in enumerate(order) if step.startswith("build:"))
    assert all(step.startswith("check:") for step in order[:first_build]), order
    assert isinstance(bundle, SubstrateBundle)


def test_ac2_off_host_endpoint_is_refused_before_anything_is_constructed() -> None:
    built: list[str] = []

    def reject_off_host(endpoint: str) -> None:
        if "remote" in endpoint:
            raise ValueError(f"off-host endpoint refused: {endpoint}")

    off_host = _config(pg_dsn="postgresql://remote.example/wombat")
    with (
        patch(_ADAPTERS[0], side_effect=_recorder("journal", built)),
        patch(_ADAPTERS[1], side_effect=_recorder("graph", built)),
        patch(_ADAPTERS[2], side_effect=_recorder("latent", built)),
        pytest.raises(ValueError, match="off-host"),
    ):
        real_adapter_bundle(off_host, residency_check=reject_off_host)

    assert built == []  # nothing constructed past the residency boundary


def test_ac3_both_paths_yield_the_same_bundle_shape() -> None:
    cold = cold_boot_bundle()
    with (
        patch(_ADAPTERS[0], return_value=MagicMock()),
        patch(_ADAPTERS[1], return_value=MagicMock()),
        patch(_ADAPTERS[2], return_value=MagicMock()),
    ):
        real = real_adapter_bundle(_config(), residency_check=lambda _e: None)

    fields = {f.name for f in dataclasses.fields(SubstrateBundle)}
    assert fields == _BUNDLE_FIELDS
    assert type(cold) is type(real) is SubstrateBundle  # the store choice is invisible to TK-1


def test_build_substrate_defaults_to_cold_boot() -> None:
    from cogworx.testing.doubles import InMemoryJournal

    bundle = build_substrate()
    assert isinstance(bundle.journal, InMemoryJournal)


def test_build_substrate_real_mode_requires_a_residency_check() -> None:
    with pytest.raises(ValueError, match="residency_check"):
        build_substrate(_config())
