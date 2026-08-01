"""TK-165 — SpeakSink gate-only tests (Q-96 as-amended); TK-267 (DEC-55) updates every voice-on
case to also wire ``speech_shape``'s output, since ``SpeakSink`` now speaks THAT text, never the
composed text.

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

TK-332 (DEC-73e): ``PartialSpeechError`` is caught in its own ``except`` clause, ahead of the
broad ``except Exception`` above — ``played_any=True`` fires ``on_spoken`` once plus one loud
WARNING and a ``spoken=True, degraded=True`` terminal ``Degraded`` (the heard world);
``played_any=False`` matches today's plain adapter-failure degrade byte-identically (no
``on_spoken``).
"""

from __future__ import annotations

import logging
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
    speech_output_to_artifact_data,
    spoken_output_from_artifact_data,
)
from wombat.voice.tts import PartialSpeechError

_FIXED_NOW = datetime(2026, 7, 9, 12, 0, 0, tzinfo=UTC)

_ITEM_ID = "gate-item-1"
_ITEM_KIND = ItemKind.GENERIC
_COMPOSED_TEXT = "**You have a new alert.** [see here](https://example.com)"
_SPEECH_TEXT = "You have a new alert."


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


class _PartialSpeechRaisingAdapter:
    """A ``TTSAdapter`` double whose ``speak()`` always raises ``PartialSpeechError`` with a
    caller-chosen ``played_any`` (TK-332)."""

    def __init__(self, *, played_any: bool) -> None:
        self._played_any = played_any
        self.call_count = 0

    def speak(self, text: str) -> None:
        self.call_count += 1
        raise PartialSpeechError(played_any=self._played_any)


def _composed_output_artifact(*, held_chat: bool = False) -> Artifact:
    """A gate-surfaced item's composed output — read ONLY for item identity now (TK-267)."""
    return Artifact(
        kind=COMPOSED_OUTPUT,
        produced_by="compose",
        provenance=Provenance(source="system", confidence=1.0, recorded_at=_FIXED_NOW),
        data=composed_output_to_artifact_data(
            _COMPOSED_TEXT, _ITEM_ID, _ITEM_KIND, False, held_chat=held_chat
        ),
    )


def _speech_output_artifact(*, text: str | None = _SPEECH_TEXT, degraded: bool = False) -> Artifact:
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


# --- AC1: gate-surfaced speech_shape output + voice on -> speaks exactly once, the SHAPED text --
# (TK-267, DEC-55c never-verbatim): SpeakSink speaks speech_shape's summary, never the composed
# text — proven by the two texts differing and only the speech text reaching the stub.


async def test_ac1_speech_shape_output_speaks_once_never_the_composed_text() -> None:
    stub = _StubAdapter()
    stage = SpeakSink(voice_enabled=True, adapter=stub)
    ctx = _ctx(compose_output=_composed_output_artifact())

    result = await stage.run(ctx)

    assert stub.call_count == 1
    assert stub.calls == [_SPEECH_TEXT]
    assert _SPEECH_TEXT != _COMPOSED_TEXT  # the fixture itself proves the two differ
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


# --- TK-267 (DEC-55): a degraded/absent speech_shape output degrades SpeakSink, NEVER falls back
# to the composed text ------------------------------------------------------------------------


async def test_degraded_speech_shape_output_degrades_speak_and_never_touches_the_adapter() -> None:
    stub = _StubAdapter()
    stage = SpeakSink(voice_enabled=True, adapter=stub)
    ctx = _ctx(
        compose_output=_composed_output_artifact(),
        speech_output=_speech_output_artifact(text=None, degraded=True),
    )

    result = await stage.run(ctx)

    assert stub.call_count == 0  # never speaks anything, let alone the composed text
    assert isinstance(result, Degraded)
    assert result.to is None
    assert "speech_shape" in result.reason
    _item_id, _item_kind, spoken, degraded = spoken_output_from_artifact_data(result.output.data)
    assert spoken is False
    assert degraded is True


async def test_absent_speech_shape_output_degrades_speak_and_never_touches_the_adapter() -> None:
    stub = _StubAdapter()
    stage = SpeakSink(voice_enabled=True, adapter=stub)
    ctx = _ctx(compose_output=_composed_output_artifact(), speech_output=None)

    result = await stage.run(ctx)

    assert stub.call_count == 0
    assert isinstance(result, Degraded)
    assert result.to is None
    assert "speech_shape" in result.reason
    _item_id, _item_kind, spoken, degraded = spoken_output_from_artifact_data(result.output.data)
    assert spoken is False
    assert degraded is True


async def test_held_chat_never_speaks_and_never_degrades() -> None:
    """DEC-57/TK-272: held_chat=True takes the SAME silent Done(spoken=False, degraded=False)
    shape as voice-off, even with voice fully wired on — quiet-by-design is not degradation, so
    this must NEVER fall through to the speech-text-None Degraded branch."""
    stub = _StubAdapter()
    stage = SpeakSink(voice_enabled=True, adapter=stub)
    ctx = _ctx(compose_output=_composed_output_artifact(held_chat=True), speech_output=None)

    result = await stage.run(ctx)

    assert stub.call_count == 0  # never speaks
    assert isinstance(result, Done)  # never Degraded
    _item_id, _item_kind, spoken, degraded = spoken_output_from_artifact_data(result.output.data)
    assert spoken is False
    assert degraded is False


async def test_voice_disabled_never_reads_speech_shape_output_stays_byte_identical() -> None:
    """AC voice-off: the voice-off Done branch is unaffected even with a degraded/absent
    speech_shape output — the check gates BEFORE speech_shape is ever read (TK-267)."""
    stub = _StubAdapter()
    stage = SpeakSink(voice_enabled=False, adapter=stub)
    ctx = _ctx(compose_output=_composed_output_artifact(), speech_output=None)

    result = await stage.run(ctx)

    assert stub.call_count == 0
    assert isinstance(result, Done)
    _item_id, _item_kind, spoken, degraded = spoken_output_from_artifact_data(result.output.data)
    assert spoken is False
    assert degraded is False


# --- TK-332 (DEC-73e): PartialSpeechError played-partial-counts-as-spoken -----------------------


async def test_partial_speech_error_played_any_true_fires_on_spoken_once_with_loud_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC3: ``played_any=True`` fires ``on_spoken`` exactly once (the register holds the full
    intended speech text) plus ONE loud WARNING naming partial playback, and the terminal
    ``Degraded`` reflects the heard world (``spoken=True, degraded=True, to=None``)."""
    stub = _PartialSpeechRaisingAdapter(played_any=True)
    spoken_calls: list[tuple[str, str]] = []
    stage = SpeakSink(
        voice_enabled=True,
        adapter=stub,
        on_spoken=lambda item_id, text: spoken_calls.append((item_id, text)),
    )
    ctx = _ctx(compose_output=_composed_output_artifact())

    with caplog.at_level(logging.WARNING):
        result = await stage.run(ctx)

    assert stub.call_count == 1
    assert spoken_calls == [(_ITEM_ID, _SPEECH_TEXT)]
    assert any("partial" in record.message.lower() for record in caplog.records)
    assert isinstance(result, Degraded)
    assert result.to is None
    assert "partial" in result.reason.lower()
    _item_id, _item_kind, spoken, degraded = spoken_output_from_artifact_data(result.output.data)
    assert spoken is True
    assert degraded is True


async def test_partial_speech_error_played_any_false_matches_todays_plain_degrade_byte_identically() -> (  # noqa: E501
    None
):
    """AC3: ``played_any=False`` fires NO ``on_spoken`` and matches today's plain adapter-failure
    degrade shape exactly (``spoken=False, degraded=True, to=None``)."""
    stub = _PartialSpeechRaisingAdapter(played_any=False)
    spoken_calls: list[tuple[str, str]] = []
    stage = SpeakSink(
        voice_enabled=True,
        adapter=stub,
        on_spoken=lambda item_id, text: spoken_calls.append((item_id, text)),
    )
    ctx = _ctx(compose_output=_composed_output_artifact())

    result = await stage.run(ctx)

    assert stub.call_count == 1
    assert spoken_calls == []  # NO on_spoken
    assert isinstance(result, Degraded)
    assert result.to is None
    _item_id, _item_kind, spoken, degraded = spoken_output_from_artifact_data(result.output.data)
    assert spoken is False
    assert degraded is True


async def test_plain_adapter_failure_still_never_fires_on_spoken_with_hook_wired() -> None:
    """Regression companion: a plain (non-``PartialSpeechError``) adapter failure never fires
    ``on_spoken`` even when a hook IS wired — proves the broad ``except Exception`` branch below
    ``PartialSpeechError`` in ``run()`` is unaffected (existing speak-sink suites green
    unmodified)."""
    stub = _RaisingAdapter(RuntimeError("engine wedged"))
    spoken_calls: list[tuple[str, str]] = []
    stage = SpeakSink(
        voice_enabled=True,
        adapter=stub,
        on_spoken=lambda item_id, text: spoken_calls.append((item_id, text)),
    )
    ctx = _ctx(compose_output=_composed_output_artifact())

    result = await stage.run(ctx)

    assert spoken_calls == []
    assert isinstance(result, Degraded)
    _item_id, _item_kind, spoken, degraded = spoken_output_from_artifact_data(result.output.data)
    assert spoken is False
    assert degraded is True
