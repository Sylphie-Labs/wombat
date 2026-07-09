"""TK-164 — SpeakSink acceptance criteria (Q-96).

All PURE: no Postgres, no real network, no real TTS engine. ``support.stage_context_fake`` is
importable via the ``pythonpath = ["tests"]`` pytest setting. Mirrors
``tests/unit/test_compose_stage.py``'s structure (fixed clock, a fake mouth-shaped double, AC-per-
section comments).
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

import pytest
from cogworx.claims.provenance import Artifact, Provenance
from cogworx.loop.result import Degraded, Done

from tests.support.stage_context_fake import StageContextFake
from wombat.gate.models import ItemKind
from wombat.sinks.speak import SpeakSink
from wombat.stages.artifacts import (
    COMPOSED_OUTPUT,
    SPOKEN_OUTPUT,
    composed_output_to_artifact_data,
    spoken_output_from_artifact_data,
    spoken_output_to_artifact_data,
)

_FIXED_NOW = datetime(2026, 7, 9, 12, 0, 0, tzinfo=UTC)

_ITEM_ID = "i-1"
_ITEM_KIND = ItemKind.GENERIC
_TEXT = "You have a new alert."


class _RecordingAdapter:
    """A ``TTSAdapter`` double that records every ``speak()`` call verbatim."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def speak(self, text: str) -> None:
        self.calls.append(text)


class _RaisingAdapter:
    """A ``TTSAdapter`` double whose ``speak()`` always raises the injected exception."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def speak(self, text: str) -> None:
        raise self._exc


def _composed_output_artifact(*, degraded: bool = False) -> Artifact:
    return Artifact(
        kind=COMPOSED_OUTPUT,
        produced_by="compose",
        provenance=Provenance(source="system", confidence=1.0, recorded_at=_FIXED_NOW),
        data=composed_output_to_artifact_data(_TEXT, _ITEM_ID, _ITEM_KIND, degraded),
    )


def _ctx(*, compose_output: Artifact | None) -> StageContextFake:
    return StageContextFake(
        now_fn=lambda: _FIXED_NOW,
        last_output_map={"compose": compose_output},
    )


# --- AC1: speaks once, verbatim -------------------------------------------------------------------


async def test_ac1_voice_enabled_speaks_once_verbatim_and_returns_spoken_output() -> None:
    adapter = _RecordingAdapter()
    stage = SpeakSink(voice_enabled=True, adapter=adapter)
    ctx = _ctx(compose_output=_composed_output_artifact())

    result = await stage.run(ctx)

    assert adapter.calls == [_TEXT]  # exactly once, verbatim
    assert isinstance(result, Done)
    assert result.output.kind == SPOKEN_OUTPUT
    assert result.output.produced_by == "speak"
    item_id, item_kind, spoken, degraded = spoken_output_from_artifact_data(result.output.data)
    assert item_id == _ITEM_ID
    assert item_kind is _ITEM_KIND
    assert spoken is True
    assert degraded is False


# --- AC2: voice-off is a silent no-op --------------------------------------------------------


async def test_ac2_voice_disabled_never_calls_adapter_and_returns_spoken_false() -> None:
    adapter = _RecordingAdapter()
    stage = SpeakSink(voice_enabled=False, adapter=adapter)
    ctx = _ctx(compose_output=_composed_output_artifact())

    result = await stage.run(ctx)

    assert adapter.calls == []  # never called
    assert isinstance(result, Done)
    _item_id, _item_kind, spoken, degraded = spoken_output_from_artifact_data(result.output.data)
    assert spoken is False
    assert degraded is False


async def test_ac2_no_adapter_wired_is_also_a_silent_no_op_even_if_voice_enabled() -> None:
    stage = SpeakSink(voice_enabled=True, adapter=None)
    ctx = _ctx(compose_output=_composed_output_artifact())

    result = await stage.run(ctx)

    assert isinstance(result, Done)
    _item_id, _item_kind, spoken, degraded = spoken_output_from_artifact_data(result.output.data)
    assert spoken is False
    assert degraded is False


# --- AC3: adapter failure degrades, never raises -----------------------------------------------


async def test_ac3_adapter_speak_raises_degrades_to_terminal_degraded() -> None:
    adapter = _RaisingAdapter(RuntimeError("engine wedged"))
    stage = SpeakSink(voice_enabled=True, adapter=adapter)
    ctx = _ctx(compose_output=_composed_output_artifact())

    result = await stage.run(ctx)

    assert isinstance(result, Degraded)
    assert result.to is None  # terminal, exactly like Done (runtime/engine.py)
    assert "engine wedged" in result.reason
    _item_id, _item_kind, spoken, degraded = spoken_output_from_artifact_data(result.output.data)
    assert spoken is False
    assert degraded is True


async def test_ac3_lazy_import_failure_path_also_degrades_to_terminal_degraded() -> None:
    """Simulates the adapter-construction-time ImportError surfacing at speak() (the OTHER named
    failure mode, ``ANY adapter failure (lazy import fails, engine init fails, speak raises)``)."""
    adapter = _RaisingAdapter(ImportError("No module named 'pyttsx3'"))
    stage = SpeakSink(voice_enabled=True, adapter=adapter)
    ctx = _ctx(compose_output=_composed_output_artifact())

    result = await stage.run(ctx)

    assert isinstance(result, Degraded)
    assert result.to is None
    _item_id, _item_kind, spoken, degraded = spoken_output_from_artifact_data(result.output.data)
    assert spoken is False
    assert degraded is True


async def test_cancelled_error_is_never_swallowed() -> None:
    adapter = _RaisingAdapter(asyncio.CancelledError())
    stage = SpeakSink(voice_enabled=True, adapter=adapter)
    ctx = _ctx(compose_output=_composed_output_artifact())

    with pytest.raises(asyncio.CancelledError):
        await stage.run(ctx)


# --- upstream wiring: no compose output yet -> raise loud (TK-101 precedent) --------------------


async def test_no_compose_output_yet_raises_runtime_error() -> None:
    stage = SpeakSink(voice_enabled=True, adapter=_RecordingAdapter())
    ctx = _ctx(compose_output=None)

    with pytest.raises(RuntimeError):
        await stage.run(ctx)


# --- structural: SpeakSink is the new drain-graph terminal ---------------------------------------


def test_speak_sink_is_terminal() -> None:
    stage = SpeakSink(voice_enabled=False, adapter=None)
    assert stage.name == "speak"
    assert stage.transitions == ()


# --- wire round-trip: json.dumps + inverse must be lossless (Q-49 regression) -------------------


def test_spoken_output_artifact_data_is_json_native_and_round_trips() -> None:
    data: dict[str, Any] = spoken_output_to_artifact_data(_ITEM_ID, ItemKind.DRAFT, True, False)

    serialized = json.dumps(data)
    assert spoken_output_from_artifact_data(json.loads(serialized)) == (
        _ITEM_ID,
        ItemKind.DRAFT,
        True,
        False,
    )
