"""TK-297 — DreamFactsStage acceptance criteria (EP-13, DEC-65g).

In-memory/monkeypatched substrate, ZERO network: mirrors ``tests/pathways/
test_dream_persona_stage.py``'s own idiom — ``chat_turns``/``user_facts`` are REAL
``ChatTurnStore``/``UserFactsStore`` instances over an unreachable DSN (lazy — never actually
connects) with their public methods monkeypatched to recording/canned/raising doubles; ``model``
is TK-8's ``FakeModel``. The genuine pg round-trips for both stores live in their own pg-gated test
modules (``tests/unit/test_chat_turns.py`` style / ``tests/unit/test_user_facts.py`` style); this
module is about ``DreamFactsStage``'s own read/extract/filter/write logic.

  AC1 (custody): a mixed model proposal (7 distinct valid facts, an over-long line, a
      forbidden-token line, and a line duplicating an already-known fact) lands AT MOST 5 new
      facts, each with ``source="dream"`` and a stable deterministic key; the over-long,
      forbidden, and duplicate lines are each dropped with a loud log line; one INFO journal line
      per accepted fact.
  AC2 (idle night): zero chat turns means the model is NEVER called and the stage still
      transitions unchanged.
  AC3 (never-block): a raising ``ChatTurnStore``, a raising model, and a raising
      ``UserFactsStore.upsert_fact`` (each case separately) are all caught, logged loud, and the
      stage STILL transitions — a mid-batch ``upsert_fact`` failure never corrupts the facts
      already landed before it.
  AC5 (instruction shape): the rendered system message carries the reflection mouth's own CON-6
      guard suffix VERBATIM plus the one-fact-per-line/third-person request.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import pytest
from cogworx.loop.result import Transition
from cogworx.model.base import ModelResponse

from tests.support.stage_context_fake import FakeModel, StageContextFake
from wombat.behavior.stages.dream_facts import DreamFactsStage, _fact_key, _parse_candidates
from wombat.chat_turns import ChatTurnStore
from wombat.persona.builder import Mouth
from wombat.persona.expression import guard_suffix
from wombat.user_facts import UserFactsStore

_NOW = datetime(2026, 7, 29, 3, 0, 0, tzinfo=UTC)
_UNREACHABLE_DSN = "postgresql://nonexistent-host-should-never-be-dialed:1/db"


def _fake_chat_turns(
    monkeypatch: pytest.MonkeyPatch,
    turns: list[dict[str, Any]],
    *,
    raises: BaseException | None = None,
) -> tuple[ChatTurnStore, list[datetime]]:
    cutoffs: list[datetime] = []

    def _turns_since(self: ChatTurnStore, cutoff: datetime) -> list[dict[str, Any]]:
        cutoffs.append(cutoff)
        if raises is not None:
            raise raises
        return turns

    monkeypatch.setattr(ChatTurnStore, "turns_since", _turns_since)
    return ChatTurnStore(_UNREACHABLE_DSN), cutoffs


def _fake_user_facts(
    monkeypatch: pytest.MonkeyPatch,
    *,
    existing: dict[str, str] | None = None,
    raises_upsert_on_call: int | None = None,
) -> tuple[UserFactsStore, list[tuple[str, str, str]]]:
    """A stateful in-memory double: ``existing`` seeds pre-known ``{fact_key: fact_text}`` rows;
    ``raises_upsert_on_call`` (1-indexed) makes exactly that ``upsert_fact`` call raise — every
    OTHER call (before or after) still lands normally, proving a mid-batch failure never corrupts
    the facts already written (AC3)."""
    rows: dict[str, str] = dict(existing or {})
    calls: list[tuple[str, str, str]] = []
    call_index = {"n": 0}

    def _count(self: UserFactsStore) -> int:
        return len(rows)

    def _list_facts(self: UserFactsStore, limit: int) -> list[dict[str, Any]]:
        return [{"fact_key": key, "fact": text} for key, text in list(rows.items())[:limit]]

    def _upsert_fact(self: UserFactsStore, fact_key: str, fact: str, source: str) -> None:
        call_index["n"] += 1
        calls.append((fact_key, fact, source))
        if raises_upsert_on_call is not None and call_index["n"] == raises_upsert_on_call:
            raise RuntimeError(f"simulated upsert_fact failure on call {call_index['n']} — AC3")
        rows[fact_key] = fact

    monkeypatch.setattr(UserFactsStore, "count", _count)
    monkeypatch.setattr(UserFactsStore, "list_facts", _list_facts)
    monkeypatch.setattr(UserFactsStore, "upsert_fact", _upsert_fact)
    return UserFactsStore(_UNREACHABLE_DSN), calls


def _turn(text: str) -> dict[str, Any]:
    return {"id": 1, "text": text, "voice": False, "captured_at": _NOW}


# ================================================================================================
# AC1: mixed proposal caps at 5 NEW facts; over-long/forbidden/duplicate each dropped loudly
# ================================================================================================


async def test_ac1_mixed_proposal_lands_at_most_five_new_facts_dropping_the_rest_loudly(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    chat_turns, _cutoffs = _fake_chat_turns(monkeypatch, [_turn("hi there")])

    duplicate_text = "The user's dog is named Biscuit."
    seeded_key = _fact_key(duplicate_text)
    user_facts, upsert_calls = _fake_user_facts(monkeypatch, existing={seeded_key: duplicate_text})

    valid_lines = [
        "The user prefers tea over coffee.",
        "The user's sister is named Ana.",
        "The user works from a home office on Fridays.",
        "The user is training for a 10k in October.",
        "The user's favorite band is playing next month.",
        "The user just adopted a cat named Waffles.",
        "The user always jokes about Mondays being cursed.",
    ]
    over_long_line = "x" * 250
    forbidden_line = "This is a clinical observation about the user's disorder."
    duplicate_line = "the user's dog is named   biscuit."  # casefold/whitespace variant of seeded

    raw_text = "\n".join([duplicate_line, *valid_lines, over_long_line, forbidden_line])
    model = FakeModel(response=ModelResponse(text=raw_text, model_id="fake", finish_reason="stop"))

    stage = DreamFactsStage(model=model, chat_turns=chat_turns, user_facts=user_facts)

    with caplog.at_level(logging.INFO, logger="wombat.behavior.stages.dream_facts"):
        result = await stage.run(StageContextFake(now_fn=lambda: _NOW))

    assert isinstance(result, Transition)
    assert result.to == "dream_behavior_log"
    assert result.output.data == {"new_facts": 5}

    # Exactly the first 5 of the 7 valid lines landed (duplicate consumed no cap slot).
    landed_facts = [fact for _key, fact, _source in upsert_calls]
    assert landed_facts == valid_lines[:5]
    assert all(source == "dream" for _key, _fact, source in upsert_calls)
    assert all(key == _fact_key(fact) for key, fact, _source in upsert_calls)

    warning_messages = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("over-long" in m for m in warning_messages)
    assert any("forbidden-token" in m for m in warning_messages)

    info_messages = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert any("duplicate" in m for m in info_messages)
    accepted_lines = [m for m in info_messages if "accepted new fact" in m]
    assert len(accepted_lines) == 5


# ================================================================================================
# AC1 (CON-6 regression): the forbidden-token screen must catch THIRD-PERSON motive/pattern
# phrasing too — the extraction instruction demands third person, so a screen restricted to the
# reflection mouth's second-person wording ("you tend to", "because you", ...) would let a
# model that honors the instruction slip motive-inference facts straight into the durable store.
# ================================================================================================


def test_ac1_forbidden_token_screen_catches_third_person_motive_and_pattern_phrasing() -> None:
    third_person_motive_lines = [
        "The user tends to skip breakfast because they are stressed about work.",
        "The user's low mood on Mondays indicates a pattern of burnout.",
        "The user seems to avoid conflict with their sister.",
        "The user is quiet on calls due to their anxiety.",
    ]
    raw_text = "\n".join(third_person_motive_lines)

    assert _parse_candidates(raw_text) == []


async def test_ac2_zero_turns_makes_no_model_call_and_transitions_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chat_turns, cutoffs = _fake_chat_turns(monkeypatch, [])
    user_facts, upsert_calls = _fake_user_facts(monkeypatch)
    model = FakeModel(raises=AssertionError("zero turns must never call the mouth"))

    stage = DreamFactsStage(model=model, chat_turns=chat_turns, user_facts=user_facts)
    result = await stage.run(StageContextFake(now_fn=lambda: _NOW))

    assert isinstance(result, Transition)
    assert result.to == "dream_behavior_log"
    assert result.output.data == {"new_facts": 0}
    assert model.calls == []
    assert upsert_calls == []
    assert len(cutoffs) == 1  # the 36h-lookback read still happened


# ================================================================================================
# AC3: never-block — a raising ChatTurnStore / model / UserFactsStore each caught, loud, no
# corruption beyond already-upserted facts
# ================================================================================================


async def test_ac3_raising_chat_turn_store_is_caught_loud_and_still_transitions(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    chat_turns, _cutoffs = _fake_chat_turns(
        monkeypatch, [], raises=RuntimeError("simulated turns_since failure — AC3")
    )
    user_facts, upsert_calls = _fake_user_facts(monkeypatch)
    model = FakeModel(raises=AssertionError("a raising turns_since must never reach the mouth"))
    stage = DreamFactsStage(model=model, chat_turns=chat_turns, user_facts=user_facts)

    with caplog.at_level(logging.ERROR, logger="wombat.behavior.stages.dream_facts"):
        result = await stage.run(StageContextFake(now_fn=lambda: _NOW))

    assert isinstance(result, Transition)
    assert result.to == "dream_behavior_log"
    assert result.output.data == {"new_facts": 0}
    assert model.calls == []
    assert upsert_calls == []
    assert any(r.levelno == logging.ERROR for r in caplog.records)


async def test_ac3_raising_model_is_caught_loud_and_still_transitions(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    chat_turns, _cutoffs = _fake_chat_turns(monkeypatch, [_turn("hi there")])
    user_facts, upsert_calls = _fake_user_facts(monkeypatch)
    model = FakeModel(raises=RuntimeError("simulated model.complete failure — AC3"))
    stage = DreamFactsStage(model=model, chat_turns=chat_turns, user_facts=user_facts)

    with caplog.at_level(logging.ERROR, logger="wombat.behavior.stages.dream_facts"):
        result = await stage.run(StageContextFake(now_fn=lambda: _NOW))

    assert isinstance(result, Transition)
    assert result.to == "dream_behavior_log"
    assert result.output.data == {"new_facts": 0}
    assert upsert_calls == []
    assert any(r.levelno == logging.ERROR for r in caplog.records)


async def test_ac3_raising_upsert_fact_mid_batch_loses_only_that_one_fact(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    chat_turns, _cutoffs = _fake_chat_turns(monkeypatch, [_turn("hi there")])
    user_facts, upsert_calls = _fake_user_facts(monkeypatch, raises_upsert_on_call=2)

    raw_text = "\n".join(
        [
            "The user prefers tea over coffee.",
            "The user's sister is named Ana.",
            "The user works from a home office on Fridays.",
        ]
    )
    model = FakeModel(response=ModelResponse(text=raw_text, model_id="fake", finish_reason="stop"))
    stage = DreamFactsStage(model=model, chat_turns=chat_turns, user_facts=user_facts)

    with caplog.at_level(logging.ERROR, logger="wombat.behavior.stages.dream_facts"):
        result = await stage.run(StageContextFake(now_fn=lambda: _NOW))

    assert isinstance(result, Transition)
    assert result.to == "dream_behavior_log"
    # The 2nd candidate's upsert raised — the 1st and 3rd still landed (no corruption beyond the
    # one failed write); new_facts counts only the SUCCESSFUL upserts.
    assert result.output.data == {"new_facts": 2}
    assert len(upsert_calls) == 3  # all three were attempted
    assert any(r.levelno == logging.ERROR for r in caplog.records)


# ================================================================================================
# AC5: the rendered extraction instruction carries the CON-6 bar verbatim + the format request
# ================================================================================================


async def test_ac5_rendered_instruction_carries_the_con6_bar_and_the_format_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chat_turns, _cutoffs = _fake_chat_turns(monkeypatch, [_turn("hi there")])
    user_facts, _upsert_calls = _fake_user_facts(monkeypatch)
    model = FakeModel(response=ModelResponse(text="", model_id="fake", finish_reason="stop"))
    stage = DreamFactsStage(model=model, chat_turns=chat_turns, user_facts=user_facts)

    await stage.run(StageContextFake(now_fn=lambda: _NOW))

    assert len(model.calls) == 1
    messages = model.calls[0]
    system_message = messages[0]
    assert system_message.role == "system"

    # The CON-6 bar, imported verbatim from the reflection mouth's own guard suffix — never a
    # re-typed copy that could drift.
    assert guard_suffix(Mouth.REFLECTION) in system_message.content

    lowered = system_message.content.lower()
    assert "one line" in lowered
    assert "third person" in lowered
