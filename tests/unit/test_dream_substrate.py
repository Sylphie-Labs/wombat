"""TK-54 — dream substrate provider acceptance criteria (EP-13).

Covers:
  AC1  cold-boot build_dream_substrate returns all four non-None collaborators; both TK-47
       consumers (CoherenceReconciler, ClaimExtractor) construct over them; zero network.
  AC2  (a) budget is REAL and pre-network — an exhausted guard raises BudgetExceededError
           before the client is ever invoked.
       (b) the model endpoint is exempt from residency — dream_substrate.py imports nothing
           from wombat.safety.local_residency (AST import-scan).
"""

from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from cogworx.coherence.reconciler import CoherenceReconciler
from cogworx.cost.budget import BudgetExceededError
from cogworx.model.base import ChatMessage, ModelCapabilities
from cogworx.model.providers.config import PriceTable, ProviderConfig
from cogworx.model.registry import ModelSpec
from cogworx.runtime.claim_extractor import ClaimExtractor
from cogworx.testing.doubles import InMemoryEntityKG, InMemoryJournal

from wombat.params import OperatingParams, load_operating_params
from wombat.pathways.dream_substrate import DreamSubstrate, build_dream_substrate

_MODULE_PATH = (
    Path(__file__).resolve().parents[2] / "src" / "wombat" / "pathways" / "dream_substrate.py"
)


def _spec() -> ModelSpec:
    """The SAME shape as bootstrap._deepseek_registry's descriptor (an openai_compat endpoint
    MUST declare capabilities statically, S9), plus a NON-ZERO price table — the default
    ZERO_PRICE_TABLE projects every call at $0.00, which would never trip a max_usd=0.0 ceiling
    pre-call (AC2a needs a genuine positive projected cost to prove the pre-network block)."""
    return ModelSpec(
        provider="openai_compat",
        config=ProviderConfig(
            api_key="test-key",
            base_url="https://api.deepseek.com/v1",
            model_pro="deepseek-chat",
            model_flash="deepseek-chat",
            price_per_mtok=PriceTable(
                pro_input_usd_per_mtok=1.0,
                pro_output_usd_per_mtok=1.0,
                flash_input_usd_per_mtok=1.0,
                flash_output_usd_per_mtok=1.0,
            ),
        ),
        capabilities=ModelCapabilities(structured_output=True, streaming=True, tools=True),
    )


def _spy_client() -> MagicMock:
    """A canned client satisfying OpenAICompatModel's narrow calling contract — never actually
    invoked in these tests, only asserted against for zero-network proof."""
    client = MagicMock()
    client.chat.completions.create = AsyncMock()
    return client


def _params(**overrides: object) -> OperatingParams:
    return load_operating_params().model_copy(update=overrides)


# --- AC1 -------------------------------------------------------------------------------


def test_ac1_constructible_with_all_four_collaborators_and_zero_network() -> None:
    entity_kg = InMemoryEntityKG()
    client = _spy_client()

    substrate = build_dream_substrate(
        entity_kg=entity_kg, spec=_spec(), params=_params(), client=client
    )

    assert isinstance(substrate, DreamSubstrate)
    assert substrate.store is not None
    assert substrate.oracle is not None
    assert substrate.model is not None
    assert substrate.source_registry is not None

    reconciler = CoherenceReconciler(
        entity_kg=entity_kg, store=substrate.store, oracle=substrate.oracle
    )
    assert reconciler is not None

    extractor = ClaimExtractor(
        journal=InMemoryJournal(),
        entity_kg=entity_kg,
        model=substrate.model,
        source_registry=substrate.source_registry,
    )
    assert extractor is not None

    client.chat.completions.create.assert_not_called()


# --- AC2(a) — budget is REAL and pre-network --------------------------------------------


async def test_ac2a_zero_max_calls_raises_before_the_client_is_invoked() -> None:
    entity_kg = InMemoryEntityKG()
    client = _spy_client()

    substrate = build_dream_substrate(
        entity_kg=entity_kg,
        spec=_spec(),
        params=_params(dream_budget_max_calls=0),
        client=client,
    )

    with pytest.raises(BudgetExceededError):
        await substrate.model.complete(
            messages=[ChatMessage(role="user", content="hi")], tier="flash"
        )

    client.chat.completions.create.assert_not_called()


async def test_ac2a_zero_max_usd_raises_before_the_client_is_invoked() -> None:
    entity_kg = InMemoryEntityKG()
    client = _spy_client()

    substrate = build_dream_substrate(
        entity_kg=entity_kg,
        spec=_spec(),
        params=_params(dream_budget_max_usd=0.0),
        client=client,
    )

    with pytest.raises(BudgetExceededError):
        await substrate.model.complete(
            messages=[ChatMessage(role="user", content="hi")], tier="flash"
        )

    client.chat.completions.create.assert_not_called()


# --- AC2(b) — the model endpoint is exempt from residency --------------------------------


def test_ac2b_dream_substrate_imports_nothing_from_local_residency() -> None:
    """An import-scan (AST) proof: dream_substrate.py never imports wombat.safety.local_
    residency — the DeepSeek endpoint is the ONE allowed egress (ASMP-1/Q-87)."""
    tree = ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and "local_residency" in node.module:
            offenders.append(node.module)
        elif isinstance(node, ast.Import):
            offenders.extend(alias.name for alias in node.names if "local_residency" in alias.name)
    assert not offenders, f"dream_substrate.py must not import local_residency: {offenders}"
