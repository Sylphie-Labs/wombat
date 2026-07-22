"""TK-164 — SpeakSink acceptance criteria (Q-96); TK-267 (DEC-55) updates every voice-on case to
also wire ``speech_shape``'s output, since ``SpeakSink`` now speaks THAT text, never the composed
text.

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
    speech_output_to_artifact_data,
    spoken_output_from_artifact_data,
    spoken_output_to_artifact_data,
)

_FIXED_NOW = datetime(2026, 7, 9, 12, 0, 0, tzinfo=UTC)

_ITEM_ID = "i-1"
_ITEM_KIND = ItemKind.GENERIC
_COMPOSED_TEXT = "**You have a new alert.**"
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


class _RecordingHook:
    """An ``on_spoken`` double that records every ``(item_id, text)`` call verbatim."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, item_id: str, text: str) -> None:
        self.calls.append((item_id, text))


class _RaisingHook:
    """An ``on_spoken`` double that always raises."""

    def __call__(self, item_id: str, text: str) -> None:
        raise RuntimeError("on_spoken exploded")


def _composed_output_artifact(
    *, degraded: bool = False, held_chat: bool = False, voice_turn: bool = False
) -> Artifact:
    return Artifact(
        kind=COMPOSED_OUTPUT,
        produced_by="compose",
        provenance=Provenance(source="system", confidence=1.0, recorded_at=_FIXED_NOW),
        data=composed_output_to_artifact_data(
            _COMPOSED_TEXT,
            _ITEM_ID,
            _ITEM_KIND,
            degraded,
            held_chat=held_chat,
            voice_turn=voice_turn,
        ),
    )


def _speech_output_artifact(*, text: str | None = _TEXT, degraded: bool = False) -> Artifact:
    """The ``speech_shape`` hop's output — the TEXT ``SpeakSink`` actually speaks (TK-267)."""
    return Artifact(
        kind="wombat.speech_output",
        produced_by="speech_shape",
        provenance=Provenance(source="system", confidence=1.0, recorded_at=_FIXED_NOW),
        data=speech_output_to_artifact_data(_ITEM_ID, _ITEM_KIND, text, degraded),
    )


class _Unset:
    """A sentinel distinguishing "caller didn't pass speech_output" from "explicitly None"."""


_UNSET = _Unset()


def _ctx(
    *,
    compose_output: Artifact | None,
    speech_output: Artifact | None | _Unset = _UNSET,
) -> StageContextFake:
    resolved_speech = (
        _speech_output_artifact() if isinstance(speech_output, _Unset) else speech_output
    )
    return StageContextFake(
        now_fn=lambda: _FIXED_NOW,
        last_output_map={"compose": compose_output, "speech_shape": resolved_speech},
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


# --- TK-267 (DEC-55): a degraded/absent speech_shape output degrades SpeakSink, NEVER falls back
# to the composed text --------------------------------------------------------------------------


async def test_degraded_speech_shape_output_degrades_speak_and_never_touches_the_adapter() -> None:
    adapter = _RecordingAdapter()
    stage = SpeakSink(voice_enabled=True, adapter=adapter)
    ctx = _ctx(
        compose_output=_composed_output_artifact(),
        speech_output=_speech_output_artifact(text=None, degraded=True),
    )

    result = await stage.run(ctx)

    assert adapter.calls == []
    assert isinstance(result, Degraded)
    assert result.to is None
    assert "speech_shape" in result.reason
    _item_id, _item_kind, spoken, degraded = spoken_output_from_artifact_data(result.output.data)
    assert spoken is False
    assert degraded is True


async def test_absent_speech_shape_output_degrades_speak_and_never_touches_the_adapter() -> None:
    adapter = _RecordingAdapter()
    stage = SpeakSink(voice_enabled=True, adapter=adapter)
    ctx = _ctx(compose_output=_composed_output_artifact(), speech_output=None)

    result = await stage.run(ctx)

    assert adapter.calls == []
    assert isinstance(result, Degraded)
    assert result.to is None
    assert "speech_shape" in result.reason


# --- TK-279 (DEC-60b): held_chat AND voice_turn speaks — the exact lock-step mirror of
# SpeechShapeStage's own gate ---------------------------------------------------------------------


async def test_held_voice_turn_speaks_the_shaped_text_never_the_composed_text() -> None:
    adapter = _RecordingAdapter()
    stage = SpeakSink(voice_enabled=True, adapter=adapter)
    ctx = _ctx(
        compose_output=_composed_output_artifact(held_chat=True, voice_turn=True),
        speech_output=_speech_output_artifact(text=_TEXT),
    )

    result = await stage.run(ctx)

    assert adapter.calls == [_TEXT]  # the SHAPED text, never _COMPOSED_TEXT
    assert isinstance(result, Done)
    _item_id, _item_kind, spoken, degraded = spoken_output_from_artifact_data(result.output.data)
    assert spoken is True
    assert degraded is False


async def test_held_typed_chat_voice_turn_false_stays_the_quiet_no_op() -> None:
    """AC2: held+typed (voice_turn defaults False) is byte-identical to the pre-TK-279 quiet
    path."""
    adapter = _RecordingAdapter()
    stage = SpeakSink(voice_enabled=True, adapter=adapter)
    ctx = _ctx(compose_output=_composed_output_artifact(held_chat=True))

    result = await stage.run(ctx)

    assert adapter.calls == []
    assert isinstance(result, Done)
    _item_id, _item_kind, spoken, degraded = spoken_output_from_artifact_data(result.output.data)
    assert spoken is False
    assert degraded is False


async def test_held_voice_turn_but_voice_disabled_is_still_the_quiet_no_op() -> None:
    adapter = _RecordingAdapter()
    stage = SpeakSink(voice_enabled=False, adapter=adapter)
    ctx = _ctx(compose_output=_composed_output_artifact(held_chat=True, voice_turn=True))

    result = await stage.run(ctx)

    assert adapter.calls == []
    assert isinstance(result, Done)
    _item_id, _item_kind, spoken, degraded = spoken_output_from_artifact_data(result.output.data)
    assert spoken is False
    assert degraded is False


async def test_held_voice_turn_shaping_failure_degrades_never_speaks_composed_text() -> None:
    """AC4 (load-bearing): a failed/rejected shaping call for a held voice turn returns the
    loud Degraded outcome, never the quiet Done, and never speaks the raw composed text."""
    adapter = _RecordingAdapter()
    stage = SpeakSink(voice_enabled=True, adapter=adapter)
    ctx = _ctx(
        compose_output=_composed_output_artifact(held_chat=True, voice_turn=True),
        speech_output=_speech_output_artifact(text=None, degraded=True),
    )

    result = await stage.run(ctx)

    assert adapter.calls == []
    assert isinstance(result, Degraded)
    assert result.to is None
    _item_id, _item_kind, spoken, degraded = spoken_output_from_artifact_data(result.output.data)
    assert spoken is False
    assert degraded is True


async def test_surfaced_voice_turn_speaks_exactly_once_no_double_speak() -> None:
    """Pin: a SURFACED voice turn (held_chat=False) passes the gate exactly as today — one speak
    per run, no new branch fires twice."""
    adapter = _RecordingAdapter()
    stage = SpeakSink(voice_enabled=True, adapter=adapter)
    ctx = _ctx(compose_output=_composed_output_artifact(held_chat=False, voice_turn=True))

    result = await stage.run(ctx)

    assert adapter.calls == [_TEXT]
    assert isinstance(result, Done)
    _item_id, _item_kind, spoken, degraded = spoken_output_from_artifact_data(result.output.data)
    assert spoken is True
    assert degraded is False


# --- TK-288 (DEC-64 gap A): on_spoken fires exactly once, after the adapter, verbatim -----------


async def test_ac1_on_spoken_fires_once_after_adapter_with_item_id_and_exact_text() -> None:
    adapter = _RecordingAdapter()
    hook = _RecordingHook()
    stage = SpeakSink(voice_enabled=True, adapter=adapter, on_spoken=hook)
    ctx = _ctx(compose_output=_composed_output_artifact())

    result = await stage.run(ctx)

    assert adapter.calls == [_TEXT]
    assert hook.calls == [(_ITEM_ID, _TEXT)]
    assert isinstance(result, Done)
    _item_id, _item_kind, spoken, _degraded = spoken_output_from_artifact_data(result.output.data)
    assert spoken is True


# --- AC2: on_spoken NEVER fires on the silent no-op branches -------------------------------------


async def test_ac2_on_spoken_never_fires_when_voice_disabled() -> None:
    hook = _RecordingHook()
    stage = SpeakSink(voice_enabled=False, adapter=_RecordingAdapter(), on_spoken=hook)
    ctx = _ctx(compose_output=_composed_output_artifact())

    result = await stage.run(ctx)

    assert hook.calls == []
    assert isinstance(result, Done)


async def test_ac2_on_spoken_never_fires_when_no_adapter_wired() -> None:
    hook = _RecordingHook()
    stage = SpeakSink(voice_enabled=True, adapter=None, on_spoken=hook)
    ctx = _ctx(compose_output=_composed_output_artifact())

    result = await stage.run(ctx)

    assert hook.calls == []
    assert isinstance(result, Done)


async def test_ac2_on_spoken_never_fires_on_held_chat_not_voice_turn() -> None:
    hook = _RecordingHook()
    stage = SpeakSink(voice_enabled=True, adapter=_RecordingAdapter(), on_spoken=hook)
    ctx = _ctx(compose_output=_composed_output_artifact(held_chat=True))

    result = await stage.run(ctx)

    assert hook.calls == []
    assert isinstance(result, Done)


# --- AC2: on_spoken NEVER fires on either Degraded branch -----------------------------------------


async def test_ac2_on_spoken_never_fires_when_speech_shape_produced_no_text() -> None:
    hook = _RecordingHook()
    stage = SpeakSink(voice_enabled=True, adapter=_RecordingAdapter(), on_spoken=hook)
    ctx = _ctx(
        compose_output=_composed_output_artifact(),
        speech_output=_speech_output_artifact(text=None, degraded=True),
    )

    result = await stage.run(ctx)

    assert hook.calls == []
    assert isinstance(result, Degraded)


async def test_ac2_on_spoken_never_fires_when_adapter_raises() -> None:
    hook = _RecordingHook()
    adapter = _RaisingAdapter(RuntimeError("engine wedged"))
    stage = SpeakSink(voice_enabled=True, adapter=adapter, on_spoken=hook)
    ctx = _ctx(compose_output=_composed_output_artifact())

    result = await stage.run(ctx)

    assert hook.calls == []
    assert isinstance(result, Degraded)


# --- AC3: a raising on_spoken hook is caught, logged once, Done(spoken=True) unchanged ----------


async def test_ac3_raising_on_spoken_hook_is_caught_logs_one_warning_result_unchanged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    adapter = _RecordingAdapter()
    stage = SpeakSink(voice_enabled=True, adapter=adapter, on_spoken=_RaisingHook())
    ctx = _ctx(compose_output=_composed_output_artifact())

    with caplog.at_level("WARNING"):
        result = await stage.run(ctx)

    assert adapter.calls == [_TEXT]
    assert isinstance(result, Done)
    _item_id, _item_kind, spoken, degraded = spoken_output_from_artifact_data(result.output.data)
    assert spoken is True
    assert degraded is False
    warnings = [rec for rec in caplog.records if rec.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "on_spoken" in warnings[0].message


# --- AC6: on_spoken defaults to None (existing suite above proves the byte-identical behavior) --


async def test_on_spoken_none_default_is_a_silent_no_op() -> None:
    adapter = _RecordingAdapter()
    stage = SpeakSink(voice_enabled=True, adapter=adapter)
    ctx = _ctx(compose_output=_composed_output_artifact())

    result = await stage.run(ctx)

    assert adapter.calls == [_TEXT]
    assert isinstance(result, Done)


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
