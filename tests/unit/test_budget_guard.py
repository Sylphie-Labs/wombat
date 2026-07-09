"""TK-9 — two-layer mouth budget acceptance criteria (Q-68).

Layer 1 (cog-worx's per-drive ``BudgetPolicy``/``BudgetGuard``) and layer 2 (wombat's
``DailySpendLedger``-backed daily token ceiling on ``ComposeStage``). All PURE: no Postgres, no
real network — mirrors ``test_compose_stage.py``'s TK-8 fixture style exactly, plus a fake
``DailyLedger`` double for layer 2.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

import pytest
from cogworx.claims.provenance import Artifact, Provenance
from cogworx.cost.budget import BudgetExceededError, BudgetPolicy
from cogworx.loop.result import Transition
from cogworx.model.base import ModelResponse, Usage
from cogworx.model.guarded import BudgetGuardedModel

from tests.support.stage_context_fake import FakeModel, StageContextFake
from wombat.compose.templates import TemplateComposer
from wombat.config import WombatConfig
from wombat.cost.daily_spend_ledger import DailySpendLedger
from wombat.domain.daily_ledger import DailyLedgerRow
from wombat.gate.models import ItemKind
from wombat.params import load_operating_params
from wombat.stages.artifacts import (
    COMPOSE_REQUEST,
    compose_request_to_artifact_data,
    composed_output_from_artifact_data,
    composed_output_tokens_spent_from_artifact_data,
)
from wombat.stages.compose import ComposeStage

_FIXED_NOW = datetime(2026, 7, 2, 12, 0, 0, tzinfo=UTC)
_FIXED_DATE = date(2026, 7, 2)

_ITEM_ID = "i-1"
_ITEM_KIND = ItemKind.GENERIC
_PAYLOAD = {"subject": "Renewal notice", "sender": "billing@acme.com"}


def _config(api_key: str = "sk-test") -> WombatConfig:
    return WombatConfig(deepseek_api_key=api_key, deepseek_base_url="https://api.deepseek.com")


def _compose_request_artifact() -> Artifact:
    return Artifact(
        kind=COMPOSE_REQUEST,
        produced_by="compose_dispatch",
        provenance=Provenance(source="system", confidence=1.0, recorded_at=_FIXED_NOW),
        data=compose_request_to_artifact_data(_ITEM_ID, _ITEM_KIND, _PAYLOAD),
    )


def _ctx(model: FakeModel) -> StageContextFake:
    return StageContextFake(
        now_fn=lambda: _FIXED_NOW,
        last_output_map={"compose_dispatch": _compose_request_artifact()},
        model_fake=model,
    )


class _FakeDailyLedger:
    """A configurable double of ``DailyLedger``'s two methods, for the ledger fail-closed tests."""

    def __init__(
        self,
        value: int = 0,
        raise_on_read: BaseException | None = None,
        raise_on_write: BaseException | None = None,
    ) -> None:
        self.value = value
        self._raise_on_read = raise_on_read
        self._raise_on_write = raise_on_write

    def current_row(self, ledger_name: str) -> DailyLedgerRow:
        if self._raise_on_read is not None:
            raise self._raise_on_read
        return DailyLedgerRow(ledger_name=ledger_name, wombat_date=_FIXED_DATE, value=self.value)

    def increment(self, ledger_name: str, amount: int = 1) -> DailyLedgerRow:
        if self._raise_on_write is not None:
            raise self._raise_on_write
        self.value += amount
        return DailyLedgerRow(ledger_name=ledger_name, wombat_date=_FIXED_DATE, value=self.value)


def _spend_ledger(**kwargs: object) -> DailySpendLedger:
    return DailySpendLedger(_FakeDailyLedger(**kwargs))  # type: ignore[arg-type]


# --- AC1: at/over the daily token ceiling degrades WITHOUT calling the model ----------------


async def test_ac1_at_or_over_daily_token_ceiling_degrades_without_calling_the_model() -> None:
    model = FakeModel(
        response=ModelResponse(
            text="should never be reached", model_id="deepseek-chat", finish_reason="stop"
        )
    )
    ctx = _ctx(model)
    stage = ComposeStage(
        config=_config(),
        template_composer=TemplateComposer(),
        spend_ledger=_spend_ledger(value=100),
        daily_token_ceiling=100,
    )

    result = await stage.run(ctx)

    assert isinstance(result, Transition)
    assert result.to == "speak"  # TK-164, Q-96: the mouth now transitions onward to the sink
    text, item_id, item_kind, degraded = composed_output_from_artifact_data(result.output.data)
    assert degraded is True
    assert text == TemplateComposer().render(_ITEM_KIND, _PAYLOAD)
    assert item_id == _ITEM_ID
    assert item_kind is _ITEM_KIND
    assert len(model.calls) == 0  # the model was NEVER invoked (AC1)
    assert composed_output_tokens_spent_from_artifact_data(result.output.data) is None


async def test_over_ceiling_also_degrades_without_calling_the_model() -> None:
    model = FakeModel(
        response=ModelResponse(text="nope", model_id="deepseek-chat", finish_reason="stop")
    )
    ctx = _ctx(model)
    stage = ComposeStage(
        config=_config(),
        template_composer=TemplateComposer(),
        spend_ledger=_spend_ledger(value=250),
        daily_token_ceiling=100,
    )

    result = await stage.run(ctx)

    assert isinstance(result, Transition)
    assert result.to == "speak"  # TK-164, Q-96: the mouth now transitions onward to the sink
    _text, _item_id, _item_kind, degraded = composed_output_from_artifact_data(result.output.data)
    assert degraded is True
    assert len(model.calls) == 0


# --- AC2: a successful call records tokens to the ledger AND the artifact carries tokens_spent ---


async def test_ac2_successful_call_records_tokens_and_artifact_carries_tokens_spent() -> None:
    usage = Usage(prompt_tokens=30, completion_tokens=12, cost_usd=0.001, latency_ms=5.0)
    model = FakeModel(
        response=ModelResponse(
            text="phrased!", model_id="deepseek-chat", finish_reason="stop", usage=usage
        )
    )
    ctx = _ctx(model)
    spend_ledger = _spend_ledger(value=0)
    stage = ComposeStage(
        config=_config(),
        template_composer=TemplateComposer(),
        spend_ledger=spend_ledger,
        daily_token_ceiling=100_000,
    )

    result = await stage.run(ctx)

    assert isinstance(result, Transition)
    assert result.to == "speak"  # TK-164, Q-96: the mouth now transitions onward to the sink
    text, _item_id, _item_kind, degraded = composed_output_from_artifact_data(result.output.data)
    assert degraded is False
    assert text == "phrased!"
    assert len(model.calls) == 1

    # the ledger recorded prompt_tokens + completion_tokens (Q-68 source of truth)
    assert spend_ledger.tokens_spent_today() == 42

    # the artifact ADDITIONALLY carries tokens_spent (AC2)
    assert composed_output_tokens_spent_from_artifact_data(result.output.data) == 42

    # json.dumps round-trip regression on the wire (Q-49)
    reloaded = json.loads(json.dumps(result.output.data))
    assert composed_output_tokens_spent_from_artifact_data(reloaded) == 42
    # and the EXISTING 4-tuple accessor is untouched (TK-8 regression guard)
    assert composed_output_from_artifact_data(reloaded) == (text, _item_id, _item_kind, degraded)


async def test_a_degraded_call_carries_no_tokens_spent() -> None:
    model = FakeModel(raises=ConnectionError("503"))
    ctx = _ctx(model)
    stage = ComposeStage(
        config=_config(),
        template_composer=TemplateComposer(),
        spend_ledger=_spend_ledger(value=0),
        daily_token_ceiling=100_000,
    )

    result = await stage.run(ctx)

    assert isinstance(result, Transition)
    assert result.to == "speak"  # TK-164, Q-96: the mouth now transitions onward to the sink
    _text, _item_id, _item_kind, degraded = composed_output_from_artifact_data(result.output.data)
    assert degraded is True
    assert composed_output_tokens_spent_from_artifact_data(result.output.data) is None


# --- fail-closed: a ledger READ failure degrades WITHOUT calling the model -------------------


async def test_fail_closed_ledger_read_failure_degrades_without_calling_the_model() -> None:
    model = FakeModel(
        response=ModelResponse(
            text="should never be reached", model_id="deepseek-chat", finish_reason="stop"
        )
    )
    ctx = _ctx(model)
    stage = ComposeStage(
        config=_config(),
        template_composer=TemplateComposer(),
        spend_ledger=_spend_ledger(raise_on_read=RuntimeError("pg unavailable")),
        daily_token_ceiling=100_000,
    )

    result = await stage.run(ctx)

    assert isinstance(result, Transition)
    assert result.to == "speak"  # TK-164, Q-96: the mouth now transitions onward to the sink
    text, _item_id, _item_kind, degraded = composed_output_from_artifact_data(result.output.data)
    assert degraded is True
    assert text == TemplateComposer().render(_ITEM_KIND, _PAYLOAD)
    assert len(model.calls) == 0  # fail CLOSED — no model call while accounting is blind


# --- a ledger WRITE failure logs loud but the already-composed output stands -----------------


async def test_ledger_write_failure_logs_loud_but_composed_output_stands() -> None:
    usage = Usage(prompt_tokens=10, completion_tokens=5)
    model = FakeModel(
        response=ModelResponse(
            text="phrased!", model_id="deepseek-chat", finish_reason="stop", usage=usage
        )
    )
    ctx = _ctx(model)
    stage = ComposeStage(
        config=_config(),
        template_composer=TemplateComposer(),
        spend_ledger=_spend_ledger(raise_on_write=RuntimeError("pg unavailable")),
        daily_token_ceiling=100_000,
    )

    result = await stage.run(ctx)

    assert isinstance(result, Transition)
    assert result.to == "speak"  # TK-164, Q-96: the mouth now transitions onward to the sink
    text, _item_id, _item_kind, degraded = composed_output_from_artifact_data(result.output.data)
    assert degraded is False  # the call already spent — the composed output stands
    assert text == "phrased!"
    assert composed_output_tokens_spent_from_artifact_data(result.output.data) == 15


# --- layer 2 disabled (spend_ledger/daily_token_ceiling not wired) preserves TK-8 exactly ----


async def test_layer_2_not_wired_preserves_tk8_behavior() -> None:
    model = FakeModel(
        response=ModelResponse(text="phrased!", model_id="deepseek-chat", finish_reason="stop")
    )
    ctx = _ctx(model)
    stage = ComposeStage(config=_config(), template_composer=TemplateComposer())

    result = await stage.run(ctx)

    assert isinstance(result, Transition)
    assert result.to == "speak"  # TK-164, Q-96: the mouth now transitions onward to the sink
    text, _item_id, _item_kind, degraded = composed_output_from_artifact_data(result.output.data)
    assert degraded is False
    assert text == "phrased!"
    assert len(model.calls) == 1
    # tokens_spent is still computed from a successful response's usage (0 here — the FakeModel
    # response didn't set one) even with no ledger wired to record it against; only a DEGRADED
    # call carries None (see test_a_degraded_call_carries_no_tokens_spent).
    assert composed_output_tokens_spent_from_artifact_data(result.output.data) == 0


# --- AC4: layer 1 is REAL config — a real BudgetPolicy wired from OperatingParams exhausts ----


async def test_ac4_real_budget_policy_from_operating_params_exhausts_and_compose_degrades() -> (
    None
):
    """Proves layer 1 is LIVE config (OperatingParams' mouth_max_calls_per_drive), not the
    unbounded ``BudgetPolicy()`` bootstrap default: exhausting a REAL guard's call ceiling
    raises ``BudgetExceededError``, and wiring that guarded model into ``ComposeStage`` degrades
    it to the template — the inner model is NEVER invoked (S11 pre-call ceiling)."""
    op = load_operating_params()
    policy = BudgetPolicy(
        max_usd_per_drive=op.mouth_max_usd_per_drive,
        max_calls_per_drive=op.mouth_max_calls_per_drive,
    )
    guard = policy.new_guard()

    for _ in range(op.mouth_max_calls_per_drive):
        guard.start_call()
    with pytest.raises(BudgetExceededError):
        guard.start_call()  # the REAL, finite ceiling — not BudgetPolicy()'s unbounded default

    inner = FakeModel(
        response=ModelResponse(
            text="should never be reached", model_id="deepseek-chat", finish_reason="stop"
        )
    )
    guarded_model = BudgetGuardedModel(inner, guard, estimator=lambda _messages, _tier: 0.0)
    ctx = StageContextFake(
        now_fn=lambda: _FIXED_NOW,
        last_output_map={"compose_dispatch": _compose_request_artifact()},
        model_fake=guarded_model,  # type: ignore[arg-type]  # duck-typed Model, not a FakeModel
    )
    stage = ComposeStage(config=_config(), template_composer=TemplateComposer())

    result = await stage.run(ctx)

    assert isinstance(result, Transition)
    assert result.to == "speak"  # TK-164, Q-96: the mouth now transitions onward to the sink
    text, _item_id, _item_kind, degraded = composed_output_from_artifact_data(result.output.data)
    assert degraded is True
    assert text == TemplateComposer().render(_ITEM_KIND, _PAYLOAD)
    assert len(inner.calls) == 0  # the guard rejects BEFORE the inner model is ever invoked
