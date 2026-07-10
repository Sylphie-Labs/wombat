"""TK-100 acceptance criteria — BriefComposeStage (Q-77).

All PURE: no Postgres, no real network. Mirrors ``tests/unit/test_compose_stage.py``'s AC2-style
degrade coverage exactly (parity with ``ComposeStage``'s catch-set), plus the TK-99 journal-spy
pattern from ``tests/stages/test_brief_force_flush_stage.py`` for the "never touches ctx.journal"
claim.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn
from zoneinfo import ZoneInfo

import pytest
from cogworx.claims.provenance import Artifact, Provenance
from cogworx.cost.budget import BudgetExceededError
from cogworx.loop.result import Transition
from cogworx.model.base import ModelResponse, Usage

from tests.support.stage_context_fake import FakeModel, StageContextFake
from wombat.calendar.models import CalendarEvent
from wombat.compose.brief_template import render_brief_lines
from wombat.config import ConfigurationError, WombatConfig
from wombat.cost.daily_spend_ledger import DailySpendLedger
from wombat.domain.brief_decision_artifact import BriefBucket, BriefDecisionArtifact
from wombat.domain.brief_payload import GmailBriefItem
from wombat.integrations.gmail.triage import PriorityBand
from wombat.stages.artifacts import BRIEF_DECISION, BRIEF_TEXT, brief_text_from_artifact_data
from wombat.stages.brief_compose_stage import BriefComposeStage

_TZ = ZoneInfo("UTC")
_NOW = datetime(2026, 7, 3, 8, 0, tzinfo=UTC)

_INTERNAL_KEYS = ("urgency_score", "priority_band", "matched_rules", "event_class")


def _config(api_key: str = "sk-test") -> WombatConfig:
    return WombatConfig(deepseek_api_key=api_key, deepseek_base_url="https://api.deepseek.com")


def _event(event_id: str, title: str) -> CalendarEvent:
    return CalendarEvent(
        event_id=event_id,
        title=title,
        start=datetime(2026, 7, 3, 13, 0, tzinfo=UTC),
        end=datetime(2026, 7, 3, 14, 0, tzinfo=UTC),
        all_day=False,
    )


def _gmail(message_id: str, subject: str) -> GmailBriefItem:
    return GmailBriefItem(
        message_id=message_id,
        subject=subject,
        sender="sender@example.com",
        received_at=_NOW,
        urgency_score=0.9,
        priority_band=PriorityBand.HIGH,
        matched_rules=("vip_sender",),
    )


def _conflict(incumbent_id: str, movable_id: str) -> dict[str, object]:
    return {
        "event_class": "calendar_conflict",
        "day": "2026-07-03",
        "incumbent_event_id": incumbent_id,
        "incumbent_title": "Standup",
        "movable_event_id": movable_id,
        "movable_title": "1:1 with Sam",
    }


def _sealed_artifact() -> BriefDecisionArtifact:
    """2 recap + 1 conflict + 1 prep (AC1's fixture shape)."""
    return BriefDecisionArtifact(
        bucket=BriefBucket(
            recap=(_gmail("m-1", "Renewal notice"), _gmail("m-2", "Team update")),
            conflict=(_conflict("evt-1", "evt-2"),),
            prep=(_event("evt-1", "Standup"),),
        ),
        calendar_unavailable=False,
        gmail_unavailable=False,
    )


def _decision_artifact(artifact: BriefDecisionArtifact) -> Artifact:
    return Artifact(
        kind=BRIEF_DECISION,
        produced_by="brief_force_flush",
        provenance=Provenance(source="system", confidence=1.0, recorded_at=_NOW),
        data=artifact.to_payload(),
    )


def _ctx(model: FakeModel, artifact: BriefDecisionArtifact | None = None) -> StageContextFake:
    sealed = artifact if artifact is not None else _sealed_artifact()
    return StageContextFake(
        now_fn=lambda: _NOW,
        last_output_map={"brief_force_flush": _decision_artifact(sealed)},
        model_fake=model,
    )


@dataclass
class _JournalSpyStageContext(StageContextFake):
    """Turns any ``ctx.journal`` access into a loud failure (mirrors TK-99's own test)."""

    journal_accessed: bool = False

    @property
    def journal(self) -> NoReturn:
        self.journal_accessed = True
        msg = "BriefComposeStage touched ctx.journal — stages never journal directly"
        raise AssertionError(msg)


# --- AC1/AC2: model called exactly once; prompt carries exactly the sealed set ---------------


async def test_ac1_model_called_exactly_once_and_prompt_contains_exactly_sealed_items() -> None:
    model = FakeModel(
        response=ModelResponse(
            text="Here's your brief.", model_id="deepseek-chat", finish_reason="stop"
        )
    )
    sealed = _sealed_artifact()
    ctx = _ctx(model, sealed)
    stage = BriefComposeStage(config=_config(), tz=_TZ)

    result = await stage.run(ctx)

    assert isinstance(result, Transition)
    assert result.to == "brief_deliver"
    assert result.output.kind == BRIEF_TEXT
    assert result.output.produced_by == "brief_compose"

    # AC1: the fake mouth is called EXACTLY once.
    assert len(model.calls) == 1
    system_msg, user_msg = model.calls[0]
    assert system_msg.role == "system"
    assert user_msg.role == "user"

    # AC1/AC2: the captured user message == render_brief_lines(sealed) exactly -- the full sealed
    # set, no additions/removals -- and it's the same string the S8 fallback would use.
    expected_body = render_brief_lines(sealed, tz=_TZ)
    assert user_msg.content == expected_body
    assert "Standup" in user_msg.content
    assert "1:1 with Sam" in user_msg.content
    assert "Renewal notice" in user_msg.content
    assert "Team update" in user_msg.content

    # Nothing outside the artifact (Q-50 internals never cross the wire).
    full_prompt_text = system_msg.content + "\n" + user_msg.content
    for internal_key in _INTERNAL_KEYS:
        assert internal_key not in full_prompt_text

    text, degraded, tokens_spent = brief_text_from_artifact_data(result.output.data)
    assert text == "Here's your brief."
    assert degraded is False
    # No spend ledger wired in this test -> layer 2 is disabled, but tokens_spent still reflects
    # the (default-zero) usage on the fake's response.
    assert tokens_spent == 0


# --- AC3: mouth failure degrades to a template brief, never raises, transitions onward --------


async def test_ac3_model_network_error_degrades_to_template_brief_without_raising() -> None:
    model = FakeModel(raises=ConnectionError("503 Service Unavailable"))
    sealed = _sealed_artifact()
    ctx = _ctx(model, sealed)
    stage = BriefComposeStage(config=_config(), tz=_TZ)

    result = await stage.run(ctx)

    assert isinstance(result, Transition)
    assert result.to == "brief_deliver"
    text, degraded, tokens_spent = brief_text_from_artifact_data(result.output.data)
    assert degraded is True
    assert text == render_brief_lines(sealed, tz=_TZ)
    assert tokens_spent == 0


# --- BudgetExceededError degrades, no raise ----------------------------------------------------


async def test_budget_exceeded_error_degrades_not_raises() -> None:
    model = FakeModel(raises=BudgetExceededError("call ceiling reached"))
    sealed = _sealed_artifact()
    ctx = _ctx(model, sealed)
    stage = BriefComposeStage(config=_config(), tz=_TZ)

    result = await stage.run(ctx)

    assert isinstance(result, Transition)
    text, degraded, tokens_spent = brief_text_from_artifact_data(result.output.data)
    assert degraded is True
    assert text == render_brief_lines(sealed, tz=_TZ)
    assert tokens_spent == 0


# --- blank/whitespace model text degrades ------------------------------------------------------


@pytest.mark.parametrize("blank_text", [None, "", "   "])
async def test_blank_response_text_degrades(blank_text: str | None) -> None:
    model = FakeModel(
        response=ModelResponse(text=blank_text, model_id="deepseek-chat", finish_reason="stop")
    )
    sealed = _sealed_artifact()
    ctx = _ctx(model, sealed)
    stage = BriefComposeStage(config=_config(), tz=_TZ)

    result = await stage.run(ctx)

    assert isinstance(result, Transition)
    text, degraded, tokens_spent = brief_text_from_artifact_data(result.output.data)
    assert degraded is True
    assert text == render_brief_lines(sealed, tz=_TZ)
    assert tokens_spent == 0


# --- layer-2 ceiling-read failure: no model call, template stands -------------------------------


async def test_ceiling_read_failure_degrades_without_calling_the_model() -> None:
    model = FakeModel(
        response=ModelResponse(
            text="should never be used", model_id="deepseek-chat", finish_reason="stop"
        )
    )
    sealed = _sealed_artifact()
    ctx = _ctx(model, sealed)

    class _BrokenLedger(DailySpendLedger):
        def __init__(self) -> None:  # no super().__init__ — never touches a real DailyLedger
            pass

        def tokens_spent_today(self) -> int:
            raise RuntimeError("ledger read failed")

    stage = BriefComposeStage(
        config=_config(), tz=_TZ, spend_ledger=_BrokenLedger(), daily_token_ceiling=1000
    )

    result = await stage.run(ctx)

    assert isinstance(result, Transition)
    _text, degraded, tokens_spent = brief_text_from_artifact_data(result.output.data)
    assert degraded is True
    assert _text == render_brief_lines(sealed, tz=_TZ)
    assert tokens_spent == 0
    assert len(model.calls) == 0  # the model must never be called while accounting is blind


# --- layer-2 ceiling reached: no model call, template stands -------------------------------------


async def test_ceiling_reached_degrades_without_calling_the_model() -> None:
    model = FakeModel(
        response=ModelResponse(
            text="should never be used", model_id="deepseek-chat", finish_reason="stop"
        )
    )
    sealed = _sealed_artifact()
    ctx = _ctx(model, sealed)

    class _MaxedLedger(DailySpendLedger):
        def __init__(self) -> None:
            pass

        def tokens_spent_today(self) -> int:
            return 1000

    stage = BriefComposeStage(
        config=_config(), tz=_TZ, spend_ledger=_MaxedLedger(), daily_token_ceiling=1000
    )

    result = await stage.run(ctx)

    assert isinstance(result, Transition)
    _text, degraded, tokens_spent = brief_text_from_artifact_data(result.output.data)
    assert degraded is True
    assert tokens_spent == 0
    assert len(model.calls) == 0


# --- layer-2 success path: post-call accounting records the real spend --------------------------


async def test_successful_call_records_tokens_spent_on_the_ledger() -> None:
    model = FakeModel(
        response=ModelResponse(
            text="Here's your brief.",
            model_id="deepseek-chat",
            finish_reason="stop",
            usage=Usage(prompt_tokens=40, completion_tokens=10),
        )
    )
    sealed = _sealed_artifact()
    ctx = _ctx(model, sealed)

    added: list[int] = []

    class _RecordingLedger(DailySpendLedger):
        def __init__(self) -> None:
            pass

        def tokens_spent_today(self) -> int:
            return 0

        def add_tokens(self, amount: int) -> int:
            added.append(amount)
            return amount

    stage = BriefComposeStage(
        config=_config(), tz=_TZ, spend_ledger=_RecordingLedger(), daily_token_ceiling=1000
    )

    result = await stage.run(ctx)

    assert isinstance(result, Transition)
    _text, degraded, tokens_spent = brief_text_from_artifact_data(result.output.data)
    assert degraded is False
    assert tokens_spent == 50
    assert added == [50]


# --- ConfigurationError at CONSTRUCTION, not first call ------------------------------------------


def test_blank_deepseek_api_key_raises_configuration_error_at_construction() -> None:
    with pytest.raises(ConfigurationError):
        BriefComposeStage(config=_config(api_key=""), tz=_TZ)


def test_whitespace_only_deepseek_api_key_raises_configuration_error_at_construction() -> None:
    with pytest.raises(ConfigurationError):
        BriefComposeStage(config=_config(api_key="   "), tz=_TZ)


# --- TK-194: assistant name threads into the system instruction only -----------------------------


async def test_tk194_default_assistant_name_renders_in_system_instruction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)  # TK-202 hermeticity: no real .env can leak an override in
    model = FakeModel(
        response=ModelResponse(
            text="Here's your brief.", model_id="deepseek-chat", finish_reason="stop"
        )
    )
    sealed = _sealed_artifact()
    ctx = _ctx(model, sealed)
    stage = BriefComposeStage(config=_config(), tz=_TZ)

    await stage.run(ctx)

    system_msg, _user_msg = model.calls[0]
    assert system_msg.content.startswith("You are Steward, a quiet steward")


async def test_tk194_configured_assistant_name_renders_in_system_instruction_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)  # TK-202 hermeticity: no real .env can leak an override in
    config = WombatConfig(
        deepseek_api_key="sk-test",
        deepseek_base_url="https://api.deepseek.com",
        wombat_assistant_name="Marvin",
    )
    model = FakeModel(
        response=ModelResponse(
            text="Here's your brief.", model_id="deepseek-chat", finish_reason="stop"
        )
    )
    sealed = _sealed_artifact()
    ctx = _ctx(model, sealed)
    stage = BriefComposeStage(config=config, tz=_TZ)

    await stage.run(ctx)

    system_msg, user_msg = model.calls[0]
    assert "Marvin" in system_msg.content
    # Structural non-goal: the name is display/persona only -- name-free everywhere else.
    assert "Marvin" not in user_msg.content


# --- never touches ctx.journal --------------------------------------------------------------------


async def test_stage_never_touches_ctx_journal() -> None:
    model = FakeModel(
        response=ModelResponse(
            text="Here's your brief.", model_id="deepseek-chat", finish_reason="stop"
        )
    )
    sealed = _sealed_artifact()
    ctx = _JournalSpyStageContext(
        now_fn=lambda: _NOW,
        last_output_map={"brief_force_flush": _decision_artifact(sealed)},
        model_fake=model,
    )
    stage = BriefComposeStage(config=_config(), tz=_TZ)

    result = await stage.run(ctx)

    assert isinstance(result, Transition)
    assert ctx.journal_accessed is False
