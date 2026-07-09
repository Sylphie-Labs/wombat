"""TK-165 — SpeakSink gate-only tests (Q-96 as-amended).

Locks TK-164's guarantees with binary stub-count tests: no audio hardware, no ``pyttsx3``
required in CI. All PURE: ``support.stage_context_fake.StageContextFake`` is the only ``ctx``
double, importable via the ``pythonpath = ["tests"]`` pytest setting (TK-15 convention: tests
live in a real package mirroring ``src/wombat/sinks/``, never as a bare top-level module).

Hold-silence is STRUCTURAL (Q-96): a held item is acked at ``ReviewOrSpeakStage`` and never
routed to ``compose``/``speak`` at all — that guarantee is pinned by the existing
``tests/unit/test_review_or_speak.py`` regression suite (its AC2,
``test_ac2_hold_returns_done_hold_report_and_acks_once``), cited here and NOT duplicated. This
module only proves ``SpeakSink``'s own half of the contract: driven directly with
``ctx.last_output('compose')`` returning ``None`` (the shape a hold structurally produces,
since a held item never reaches ``compose``), the stub is never touched and the stage fails
loud instead of silently no-op'ing.
"""

from __future__ import annotations

from datetime import UTC, datetime

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
)

_FIXED_NOW = datetime(2026, 7, 9, 12, 0, 0, tzinfo=UTC)

_ITEM_ID = "gate-item-1"
_ITEM_KIND = ItemKind.GENERIC
_TEXT = "You have a new alert."


class _StubAdapter:
    """A ``TTSAdapter`` double: a binary, recording stub — no real TTS engine."""

    def __init__(self) -> None:
        self.call_count = 0
        self.calls: list[str] = []

    def speak(self, text: str) -> None:
        self.call_count += 1
        self.calls.append(text)


class _RaisingAdapter:
    """A ``TTSAdapter`` double whose ``speak()`` always raises the injected exception."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc
        self.call_count = 0

    def speak(self, text: str) -> None:
        self.call_count += 1
        raise self._exc


def _composed_output_artifact() -> Artifact:
    """A gate-surfaced item's composed output — the ONLY thing ``SpeakSink`` ever reads."""
    return Artifact(
        kind=COMPOSED_OUTPUT,
        produced_by="compose",
        provenance=Provenance(source="system", confidence=1.0, recorded_at=_FIXED_NOW),
        data=composed_output_to_artifact_data(_TEXT, _ITEM_ID, _ITEM_KIND, False),
    )


def _ctx(*, compose_output: Artifact | None) -> StageContextFake:
    return StageContextFake(
        now_fn=lambda: _FIXED_NOW,
        last_output_map={"compose": compose_output},
    )


# --- AC1: gate-surfaced composed output + voice on -> speaks exactly once, verbatim -----------


async def test_ac1_gate_surfaced_composed_output_speaks_once_verbatim() -> None:
    stub = _StubAdapter()
    stage = SpeakSink(voice_enabled=True, adapter=stub)
    ctx = _ctx(compose_output=_composed_output_artifact())

    result = await stage.run(ctx)

    assert stub.call_count == 1
    assert stub.calls == [_TEXT]
    assert isinstance(result, Done)
    assert result.output.kind == SPOKEN_OUTPUT
    item_id, item_kind, spoken, degraded = spoken_output_from_artifact_data(result.output.data)
    assert item_id == _ITEM_ID
    assert item_kind is _ITEM_KIND
    assert spoken is True
    assert degraded is False


# --- AC2: structural hold-silence (Q-96-amended) -----------------------------------------------
#
# A held item never reaches ``compose``/``speak`` at all (acked at ``ReviewOrSpeakStage`` — pinned
# by tests/unit/test_review_or_speak.py::test_ac2_hold_returns_done_hold_report_and_acks_once,
# cited and NOT duplicated here). This test proves SpeakSink's own half: driven with no compose
# output available, it never touches the stub and fails loud instead of silently no-op'ing.


async def test_ac2_no_compose_output_never_speaks_and_fails_loud() -> None:
    stub = _StubAdapter()
    stage = SpeakSink(voice_enabled=True, adapter=stub)
    ctx = _ctx(compose_output=None)

    with pytest.raises(RuntimeError):
        await stage.run(ctx)

    assert stub.call_count == 0


# --- AC3: voice_enabled=false -> silent no-op, text path unaffected (additive-only) ------------


async def test_ac3_voice_disabled_never_speaks_but_returns_done() -> None:
    stub = _StubAdapter()
    stage = SpeakSink(voice_enabled=False, adapter=stub)
    ctx = _ctx(compose_output=_composed_output_artifact())

    result = await stage.run(ctx)

    assert stub.call_count == 0
    assert isinstance(result, Done)
    _item_id, _item_kind, spoken, degraded = spoken_output_from_artifact_data(result.output.data)
    assert spoken is False
    assert degraded is False


# --- AC4: adapter failure degrades cleanly, never raises, text path (wire) untouched ------------


@pytest.mark.parametrize(
    "exc",
    [
        pytest.param(ImportError("No module named 'pyttsx3'"), id="lazy-import-failure"),
        pytest.param(RuntimeError("engine wedged"), id="generic-adapter-failure"),
    ],
)
async def test_ac4_adapter_speak_failure_degrades_without_raising(exc: BaseException) -> None:
    stub = _RaisingAdapter(exc)
    stage = SpeakSink(voice_enabled=True, adapter=stub)
    ctx = _ctx(compose_output=_composed_output_artifact())

    result = await stage.run(ctx)  # must not raise

    assert stub.call_count == 1
    assert isinstance(result, Degraded)
    assert result.to is None
    assert result.reason
    _item_id, _item_kind, spoken, degraded = spoken_output_from_artifact_data(result.output.data)
    assert spoken is False
    assert degraded is True


async def test_ac4_adapter_failure_leaves_the_composed_output_wire_untouched() -> None:
    """Companion to AC4: the upstream ``compose`` artifact the fake ctx returned is unmodified by
    a speak failure — text delivery is exercised/unaffected (CON-3, additive-only)."""
    compose_artifact = _composed_output_artifact()
    snapshot = compose_artifact.model_copy(deep=True)
    stub = _RaisingAdapter(RuntimeError("engine wedged"))
    stage = SpeakSink(voice_enabled=True, adapter=stub)
    ctx = _ctx(compose_output=compose_artifact)

    result = await stage.run(ctx)

    assert isinstance(result, Degraded)
    assert compose_artifact == snapshot  # the wire artifact itself is untouched
