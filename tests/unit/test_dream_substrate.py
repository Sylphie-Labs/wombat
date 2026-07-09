"""TK-54 — dream substrate provider acceptance criteria (EP-13).

Covers:
  AC1  cold-boot build_dream_substrate returns all four non-None collaborators; both TK-47
       consumers (CoherenceReconciler, ClaimExtractor) construct over them; zero network.
  AC2  (a) budget is REAL and pre-network — an exhausted guard raises BudgetExceededError
           before the client is ever invoked.
       (b) the model endpoint is exempt from residency — dream_substrate.py imports nothing
           from wombat.safety.local_residency (AST import-scan).

TK-180 (CR2-3) adds:
  AC1'  the register's exact repro — the budget ceiling is PER-NIGHT, not per-process-lifetime.
        20 completes on wombat-night N exhaust the default max_calls=20 ceiling; the 21st call on
        the SAME night N is refused, but the FIRST call on wombat-night N+1 succeeds (an injected
        clock drives the night key deterministically — the two-successive-drives test).
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime
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


def _make_raw_response(*, prompt_tokens: int = 100, completion_tokens: int = 50) -> MagicMock:
    """Build a MagicMock mimicking an OpenAI ``ChatCompletion`` — enough for
    ``OpenAICompatModel`` to parse a SUCCESSFUL completion (mirrors cog-worx's own
    ``test_openai_compat_model.py::_make_raw_response`` fixture)."""
    raw = MagicMock()
    raw.usage = MagicMock()
    raw.usage.prompt_tokens = prompt_tokens
    raw.usage.completion_tokens = completion_tokens
    message = MagicMock()
    message.content = "hello"
    message.tool_calls = None
    choice = MagicMock()
    choice.message = message
    choice.finish_reason = "stop"
    raw.choices = [choice]
    return raw


def _success_client() -> MagicMock:
    """A canned client whose ``chat.completions.create`` always resolves to a valid completion
    (never raises) — the TK-180 register repro needs REAL successful calls to accumulate the
    per-night ceiling, not just a pre-network refusal."""
    client = MagicMock()
    client.chat.completions.create = AsyncMock(return_value=_make_raw_response())
    return client


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


# --- TK-180 (CR2-3) — the budget ceiling is per-night, not per-process-lifetime ----------


async def test_ac1_prime_budget_renews_per_night_not_process_lifetime() -> None:
    """The register's exact repro: default params (max_calls=20) over a canned SUCCESS client.
    20 completes on wombat-night N exhaust the ceiling; the 21st call on the SAME night N is
    refused; the FIRST call on wombat-night N+1 SUCCEEDS — the two-successive-drives test, driven
    by an injectable clock rather than a real sleep."""
    entity_kg = InMemoryEntityKG()
    client = _success_client()
    current_instant = datetime(2026, 7, 8, 3, 0, tzinfo=UTC)

    def _clock() -> datetime:
        return current_instant

    substrate = build_dream_substrate(
        entity_kg=entity_kg,
        spec=_spec(),
        params=_params(),
        client=client,
        clock=_clock,
    )

    for _ in range(20):
        response = await substrate.model.complete(
            messages=[ChatMessage(role="user", content="hi")], tier="flash"
        )
        assert response.text == "hello"

    with pytest.raises(BudgetExceededError):
        await substrate.model.complete(
            messages=[ChatMessage(role="user", content="hi")], tier="flash"
        )

    # Advance to the next wombat-night (TK-52's once-per-night fence means one dream drive
    # per night in production; here the injected clock stands in for the elapsed night).
    current_instant = datetime(2026, 7, 9, 3, 0, tzinfo=UTC)

    response = await substrate.model.complete(
        messages=[ChatMessage(role="user", content="hi")], tier="flash"
    )
    assert response.text == "hello"


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
