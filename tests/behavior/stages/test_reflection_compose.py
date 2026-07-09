"""TK-114 — ReflectionComposeStage acceptance criteria (EP-22, Q-102b-f).

All PURE: no Postgres, no real network. ``support.stage_context_fake`` is importable via the
``pythonpath = ["tests"]`` pytest setting. Uses the REAL packaged ``load_psychology_kb()`` /
``rapid_context_switching`` seed pattern (mirrors TK-118's own AC1 idiom) so the hint content is
never a hand-rolled fixture.

  AC1: a compose_dispatch COMPOSE_REQUEST artifact carrying the TK-113 payload shape -> model.
      complete called EXACTLY once; the assembled messages contain ONE system message holding
      BOTH the forbidden-language instruction AND the hint text; the tail user message carries
      payload-derived text only (kind/date, never pattern_id/window_ref); returns Done with
      COMPOSED_OUTPUT, item_kind REFLECTION.
  AC2: an automated classifier over the rendered string on BOTH paths (stub-model output and the
      fallback) finds none of the forbidden clinical/motive/diagnosis terms.
  AC3: a model exception, a model timeout, and a context-assembly failure ALL degrade to a
      deterministic non-blank fallback with degraded=True — run() never raises.
  AC4: kb=[] proceeds with the safe default prompt, non-blank output, no crash; and with hints
      present + a stub model, the hint strings never appear verbatim in the rendered output (also
      discharges TK-118's AC4).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from cogworx.claims.provenance import Artifact, Provenance
from cogworx.context.assembler import ContextAssembler
from cogworx.context.errors import ContextAssemblyError
from cogworx.context.types import ContextRequest
from cogworx.loop.result import Done
from cogworx.model.base import ModelResponse

from tests.support.stage_context_fake import FakeModel, StageContextFake
from wombat.behavior.stages.reflection_compose import ReflectionComposeStage
from wombat.gate.models import ItemKind
from wombat.kb.loader import load_psychology_kb
from wombat.kb.phrasing_hints import extract_phrasing_hints
from wombat.stages.artifacts import (
    COMPOSE_REQUEST,
    COMPOSED_OUTPUT,
    compose_request_to_artifact_data,
    composed_output_from_artifact_data,
)

_FIXED_NOW = datetime(2026, 7, 9, 6, 0, 0, tzinfo=UTC)

_ITEM_ID = "wombat.reflection:2026-07-09"
_DATE = "2026-07-09"
_SEED_PATTERN_ID = "rapid_context_switching"
_WINDOW_REF = f"productivity_window:{_DATE}"

# The forbidden classifier terms (NG-1/NG-2/NG-3, case-insensitive).
_FORBIDDEN_TERMS = (
    "diagnos",
    "disorder",
    "symptom",
    "pattern indicates",
    "you seem to",
    "you tend to",
    "because you",
    "due to your",
)


def _assert_language_clean(text: str) -> None:
    lowered = text.lower()
    for term in _FORBIDDEN_TERMS:
        assert term not in lowered, f"forbidden term {term!r} found in rendered text: {text!r}"


def _payload(pattern_id: str | None = _SEED_PATTERN_ID) -> dict[str, Any]:
    return {
        "item_kind": "reflection",
        "event_class": "reflection",
        "kind": "pattern_reflection",
        "pattern_id": pattern_id,
        "window_ref": _WINDOW_REF,
        "date": _DATE,
    }


def _compose_request_artifact(pattern_id: str | None = _SEED_PATTERN_ID) -> Artifact:
    return Artifact(
        kind=COMPOSE_REQUEST,
        produced_by="compose_dispatch",
        provenance=Provenance(source="system", confidence=1.0, recorded_at=_FIXED_NOW),
        data=compose_request_to_artifact_data(_ITEM_ID, ItemKind.REFLECTION, _payload(pattern_id)),
    )


def _ctx(
    model: FakeModel | None = None, pattern_id: str | None = _SEED_PATTERN_ID
) -> StageContextFake:
    return StageContextFake(
        now_fn=lambda: _FIXED_NOW,
        last_output_map={"compose_dispatch": _compose_request_artifact(pattern_id)},
        model_fake=model,
    )


# --- AC1: success path — model called once, prompt carries instruction + hints -------------------


async def test_ac1_success_path_assembles_instruction_and_hints_calls_model_once() -> None:
    kb = load_psychology_kb()
    hints = extract_phrasing_hints(_SEED_PATTERN_ID, kb)
    assert hints, "seed pattern_id must have >=1 phrasing hint for this test to mean anything"

    model = FakeModel(
        response=ModelResponse(
            text="a quiet reflection.", model_id="deepseek-chat", finish_reason="stop"
        )
    )
    ctx = _ctx(model)
    stage = ReflectionComposeStage(kb=kb)

    result = await stage.run(ctx)

    assert isinstance(result, Done)
    assert result.output.kind == COMPOSED_OUTPUT
    assert result.output.produced_by == "reflection_compose"
    text, item_id, item_kind, degraded = composed_output_from_artifact_data(result.output.data)
    assert text == "a quiet reflection."
    assert item_id == _ITEM_ID
    assert item_kind is ItemKind.REFLECTION
    assert degraded is False

    # the fake model was called exactly once
    assert len(model.calls) == 1
    captured = model.calls[0]

    system_msgs = [m for m in captured if m.role == "system"]
    user_msgs = [m for m in captured if m.role == "user"]
    assert len(system_msgs) == 1
    assert len(user_msgs) == 1

    system_content = system_msgs[0].content
    # BOTH the forbidden-language instruction AND the hint text ride the ONE system message.
    assert "diagnos" in system_content.lower()  # the instruction names what to avoid
    assert "No preamble" in system_content
    for hint in hints:
        assert hint in system_content

    # the tail user message carries payload-derived text only (kind/date) — never
    # pattern_id/window_ref, which stay KB/queue-internal (CON-1/Q-50 boundary).
    tail_content = user_msgs[0].content
    assert "pattern_reflection" in tail_content
    assert _DATE in tail_content
    assert _SEED_PATTERN_ID not in tail_content
    assert _WINDOW_REF not in tail_content


# --- AC2: classifier finds none of the forbidden terms on EITHER path -----------------------------


async def test_ac2_stub_model_output_passes_language_classifier() -> None:
    kb = load_psychology_kb()
    model = FakeModel(
        response=ModelResponse(
            text="a quiet reflection on today.", model_id="deepseek-chat", finish_reason="stop"
        )
    )
    stage = ReflectionComposeStage(kb=kb)

    result = await stage.run(_ctx(model))

    assert isinstance(result, Done)
    text, _item_id, _item_kind, degraded = composed_output_from_artifact_data(result.output.data)
    assert degraded is False
    _assert_language_clean(text)


async def test_ac2_fallback_output_passes_language_classifier() -> None:
    kb = load_psychology_kb()
    model = FakeModel(raises=ConnectionError("503 Service Unavailable"))
    stage = ReflectionComposeStage(kb=kb)

    result = await stage.run(_ctx(model))

    assert isinstance(result, Done)
    text, _item_id, _item_kind, degraded = composed_output_from_artifact_data(result.output.data)
    assert degraded is True
    assert text != ""
    _assert_language_clean(text)


# --- AC3: degrade paths — model error / timeout / assembly failure, never raises ------------------


async def test_ac3_model_error_degrades_to_fallback_not_raises() -> None:
    kb = load_psychology_kb()
    model = FakeModel(raises=ConnectionError("boom"))
    stage = ReflectionComposeStage(kb=kb)

    result = await stage.run(_ctx(model))

    assert isinstance(result, Done)
    text, item_id, item_kind, degraded = composed_output_from_artifact_data(result.output.data)
    assert degraded is True
    assert text != ""
    assert item_id == _ITEM_ID
    assert item_kind is ItemKind.REFLECTION


async def test_ac3_model_timeout_degrades_to_fallback_within_bound() -> None:
    import time

    kb = load_psychology_kb()
    model = FakeModel(sleep_seconds=5.0)  # far longer than the tiny timeout below
    stage = ReflectionComposeStage(kb=kb, timeout_seconds=0.05)

    start = time.monotonic()
    result = await stage.run(_ctx(model))
    elapsed = time.monotonic() - start

    assert elapsed < 1.0  # bounded by wait_for's 0.05s timeout, not the model's 5s sleep
    assert isinstance(result, Done)
    _text, _item_id, _item_kind, degraded = composed_output_from_artifact_data(result.output.data)
    assert degraded is True


async def test_ac3_blank_response_text_degrades_to_fallback() -> None:
    kb = load_psychology_kb()
    model = FakeModel(
        response=ModelResponse(text="   ", model_id="deepseek-chat", finish_reason="stop")
    )
    stage = ReflectionComposeStage(kb=kb)

    result = await stage.run(_ctx(model))

    assert isinstance(result, Done)
    text, _item_id, _item_kind, degraded = composed_output_from_artifact_data(result.output.data)
    assert degraded is True
    assert text != ""


async def test_ac3_context_assembly_failure_degrades_to_fallback_model_never_called(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kb = load_psychology_kb()

    async def _boom(self: ContextAssembler, request: ContextRequest) -> object:
        raise ContextAssemblyError(slot_name="instructions", cause=RuntimeError("boom"))

    monkeypatch.setattr(ContextAssembler, "assemble", _boom)

    # An UNCONFIGURED FakeModel (neither response= nor raises=) — if the stage ever reached the
    # model call despite the assembly failure, complete() would raise NotImplementedError and
    # this test would fail loudly rather than silently passing.
    model = FakeModel()
    stage = ReflectionComposeStage(kb=kb)

    result = await stage.run(_ctx(model))

    assert isinstance(result, Done)
    text, _item_id, _item_kind, degraded = composed_output_from_artifact_data(result.output.data)
    assert degraded is True
    assert text != ""
    assert model.calls == []  # never reached


# --- AC4: kb=[] proceeds safely; hints never leak verbatim into the rendered output ---------------


async def test_ac4_empty_kb_proceeds_with_safe_default_prompt_no_crash() -> None:
    model = FakeModel(
        response=ModelResponse(text="a quiet note.", model_id="deepseek-chat", finish_reason="stop")
    )
    stage = ReflectionComposeStage(kb=[])

    result = await stage.run(_ctx(model))

    assert isinstance(result, Done)
    text, _item_id, item_kind, degraded = composed_output_from_artifact_data(result.output.data)
    assert text != ""
    assert degraded is False
    assert item_kind is ItemKind.REFLECTION


async def test_ac4_hints_never_appear_verbatim_in_rendered_output() -> None:
    kb = load_psychology_kb()
    hints = extract_phrasing_hints(_SEED_PATTERN_ID, kb)
    assert hints, "seed pattern_id must have >=1 phrasing hint for this test to mean anything"

    model = FakeModel(
        response=ModelResponse(
            text="a quiet reflection, phrased fresh.",
            model_id="deepseek-chat",
            finish_reason="stop",
        )
    )
    stage = ReflectionComposeStage(kb=kb)

    result = await stage.run(_ctx(model))

    assert isinstance(result, Done)
    text, _item_id, _item_kind, degraded = composed_output_from_artifact_data(result.output.data)
    assert degraded is False
    for hint in hints:
        assert hint not in text


# --- structural: terminal stage, no compose_dispatch output yet raises ----------------------------


def test_reflection_compose_stage_is_terminal_with_no_transitions() -> None:
    stage = ReflectionComposeStage(kb=[])
    assert stage.transitions == ()
    assert stage.name == "reflection_compose"


async def test_run_raises_runtime_error_when_no_compose_dispatch_output() -> None:
    stage = ReflectionComposeStage(kb=[])
    ctx = StageContextFake(now_fn=lambda: _FIXED_NOW)

    with pytest.raises(RuntimeError, match="compose_dispatch"):
        await stage.run(ctx)
