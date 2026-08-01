"""TK-101 acceptance criteria — BriefDeliverStage (Q-78).

All PURE / local-filesystem only: no Postgres, no real network, no real voice provider. Mirrors
``tests/stages/test_brief_compose_stage.py``'s journal-spy pattern for the "never touches
ctx.journal" claim.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn
from zoneinfo import ZoneInfo

import pytest
from cogworx.claims.provenance import Artifact, Provenance
from cogworx.loop.result import Done

from tests.support.stage_context_fake import StageContextFake
from wombat.stages.artifacts import (
    BRIEF_DELIVERED,
    BRIEF_TEXT,
    brief_delivered_from_artifact_data,
    brief_text_to_artifact_data,
)
from wombat.stages.brief_deliver_stage import BriefDeliverStage
from wombat.voice import tts as voice_tts

_UTC_TZ = ZoneInfo("UTC")
# A real non-UTC offset (CST-1/DEC-6) to catch bare-UTC bugs in the delivery header.
_LOCAL_TZ = ZoneInfo("America/Chicago")
_NOW = datetime(2026, 7, 3, 14, 0, tzinfo=UTC)  # 09:00 in America/Chicago (UTC-5 in July, CDT)


def _brief_text_artifact(text: str = "Here's your brief.") -> Artifact:
    return Artifact(
        kind=BRIEF_TEXT,
        produced_by="brief_compose",
        provenance=Provenance(source="system", confidence=1.0, recorded_at=_NOW),
        data=brief_text_to_artifact_data(text, degraded=False, tokens_spent=42),
    )


def _ctx(
    *, run_id: str = "run-1", text: str = "Here's your brief.", now: datetime = _NOW
) -> StageContextFake:
    return StageContextFake(
        now_fn=lambda: now,
        last_output_map={"brief_compose": _brief_text_artifact(text)},
        run_id=run_id,
    )


@dataclass
class _JournalSpyStageContext(StageContextFake):
    """Turns any ``ctx.journal`` access into a loud failure (mirrors TK-100's own test)."""

    journal_accessed: bool = False

    @property
    def journal(self) -> NoReturn:
        self.journal_accessed = True
        msg = "BriefDeliverStage touched ctx.journal — stages never journal directly"
        raise AssertionError(msg)


# --- AC1: file sink appended with a tz-local header; terminal Done -----------------------------


async def test_ac1_appends_tz_local_header_and_text_and_returns_terminal_done(
    tmp_path: Path,
) -> None:
    sink = tmp_path / "brief.txt"
    ctx = _ctx()
    stage = BriefDeliverStage(sink_path=sink, tz=_LOCAL_TZ, voice_enabled=False)

    assert stage.transitions == ()

    result = await stage.run(ctx)

    assert isinstance(result, Done)
    assert result.output.kind == BRIEF_DELIVERED
    assert result.output.produced_by == "brief_deliver"

    content = sink.read_text(encoding="utf-8")
    assert "Here's your brief." in content
    assert "[run=run-1]" in content

    delivered_at, voice_spoken, replay = brief_delivered_from_artifact_data(result.output.data)
    assert replay is False
    assert voice_spoken is False

    # The header timestamp is in the CANONICAL tz, NOT bare UTC (Q-15/DEC-21): _NOW is 14:00 UTC,
    # which is 09:00 in America/Chicago (UTC-5, CDT in July) -- assert against the local hour.
    local_expected = _NOW.astimezone(_LOCAL_TZ)
    assert delivered_at == local_expected.isoformat()
    assert local_expected.hour == 9
    assert "09:00" in content or local_expected.strftime("%H:%M") in content


# --- AC2: voice enabled + speak sink -> spoken exactly once, verbatim --------------------------


async def test_ac2_voice_enabled_speaks_exactly_once_verbatim(tmp_path: Path) -> None:
    sink = tmp_path / "brief.txt"
    spoken: list[str] = []
    ctx = _ctx(text="Speak this exact text.")
    stage = BriefDeliverStage(
        sink_path=sink, tz=_UTC_TZ, voice_enabled=True, speak=spoken.append
    )

    result = await stage.run(ctx)

    assert isinstance(result, Done)
    assert spoken == ["Speak this exact text."]
    _delivered_at, voice_spoken, _replay = brief_delivered_from_artifact_data(result.output.data)
    assert voice_spoken is True


# --- AC3: voice enabled but no speak sink -> text-only, warning, no raise -----------------------


async def test_ac3_voice_enabled_without_speak_sink_is_text_only_and_does_not_raise(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    sink = tmp_path / "brief.txt"
    ctx = _ctx()
    stage = BriefDeliverStage(sink_path=sink, tz=_UTC_TZ, voice_enabled=True, speak=None)

    with caplog.at_level("WARNING"):
        result = await stage.run(ctx)

    assert isinstance(result, Done)
    assert sink.read_text(encoding="utf-8")  # text still written
    _delivered_at, voice_spoken, _replay = brief_delivered_from_artifact_data(result.output.data)
    assert voice_spoken is False
    assert any("speak" in rec.message.lower() for rec in caplog.records)


# --- speak() raises -> warning, text stands, no raise -------------------------------------------


async def test_speak_raises_logs_warning_text_delivery_stands(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    sink = tmp_path / "brief.txt"
    ctx = _ctx(text="Should still be written.")

    def _boom(_text: str) -> None:
        raise RuntimeError("voice provider exploded")

    stage = BriefDeliverStage(sink_path=sink, tz=_UTC_TZ, voice_enabled=True, speak=_boom)

    with caplog.at_level("WARNING"):
        result = await stage.run(ctx)

    assert isinstance(result, Done)
    assert "Should still be written." in sink.read_text(encoding="utf-8")
    _delivered_at, voice_spoken, _replay = brief_delivered_from_artifact_data(result.output.data)
    assert voice_spoken is False
    assert any("speak" in rec.message.lower() for rec in caplog.records)


# --- TK-332 repair (DEC-73e applies to BOTH call sites named in DEC-64): PartialSpeechError -----


async def test_partial_speech_error_played_any_true_counts_as_spoken(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """played_any=True: voice_spoken flips True, on_spoken fires once, ONE loud warning naming
    partial playback — mirrors sinks/speak.py's SAME-named handling."""
    sink = tmp_path / "brief.txt"
    hook_calls: list[tuple[str, str]] = []

    def _partial(_text: str) -> None:
        raise voice_tts.PartialSpeechError(played_any=True)

    stage = BriefDeliverStage(
        sink_path=sink,
        tz=_UTC_TZ,
        voice_enabled=True,
        speak=_partial,
        on_spoken=lambda item_id, text: hook_calls.append((item_id, text)),
    )
    ctx = _ctx(run_id="run-9", text="Fish died mid-stream.")

    with caplog.at_level("WARNING"):
        result = await stage.run(ctx)

    assert isinstance(result, Done)
    assert hook_calls == [("brief:run-9", "Fish died mid-stream.")]
    _delivered_at, voice_spoken, _replay = brief_delivered_from_artifact_data(result.output.data)
    assert voice_spoken is True
    assert "Fish died mid-stream." in sink.read_text(encoding="utf-8")
    assert any("partial" in rec.message.lower() for rec in caplog.records)


async def test_partial_speech_error_played_any_false_matches_plain_failure(tmp_path: Path) -> None:
    """played_any=False: byte-identical posture to any other speak() failure — no on_spoken,
    voice_spoken stays False."""
    sink = tmp_path / "brief.txt"
    hook_calls: list[tuple[str, str]] = []

    def _partial(_text: str) -> None:
        raise voice_tts.PartialSpeechError(played_any=False)

    stage = BriefDeliverStage(
        sink_path=sink,
        tz=_UTC_TZ,
        voice_enabled=True,
        speak=_partial,
        on_spoken=lambda item_id, text: hook_calls.append((item_id, text)),
    )
    ctx = _ctx(text="Nothing was heard.")

    result = await stage.run(ctx)

    assert isinstance(result, Done)
    assert hook_calls == []
    _delivered_at, voice_spoken, _replay = brief_delivered_from_artifact_data(result.output.data)
    assert voice_spoken is False
    assert "Nothing was heard." in sink.read_text(encoding="utf-8")


# --- AC5 (TK-288, DEC-64 gap A): on_spoken fires once, brief-scoped id + text, working case only -


async def test_ac5_on_spoken_fires_once_with_brief_scoped_id_and_text_when_speak_works(
    tmp_path: Path,
) -> None:
    sink = tmp_path / "brief.txt"
    spoken: list[str] = []
    hook_calls: list[tuple[str, str]] = []
    ctx = _ctx(run_id="run-42", text="Here's your brief text.")
    stage = BriefDeliverStage(
        sink_path=sink,
        tz=_UTC_TZ,
        voice_enabled=True,
        speak=spoken.append,
        on_spoken=lambda item_id, text: hook_calls.append((item_id, text)),
    )

    result = await stage.run(ctx)

    assert isinstance(result, Done)
    assert hook_calls == [("brief:run-42", "Here's your brief text.")]
    _delivered_at, voice_spoken, _replay = brief_delivered_from_artifact_data(result.output.data)
    assert voice_spoken is True


async def test_ac5_on_spoken_never_fires_when_speak_raises(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    sink = tmp_path / "brief.txt"
    hook_calls: list[tuple[str, str]] = []
    ctx = _ctx(text="Should still be written.")

    def _boom(_text: str) -> None:
        raise RuntimeError("voice provider exploded")

    stage = BriefDeliverStage(
        sink_path=sink,
        tz=_UTC_TZ,
        voice_enabled=True,
        speak=_boom,
        on_spoken=lambda item_id, text: hook_calls.append((item_id, text)),
    )

    with caplog.at_level("WARNING"):
        result = await stage.run(ctx)

    assert isinstance(result, Done)
    assert hook_calls == []
    assert "Should still be written." in sink.read_text(encoding="utf-8")


async def test_ac5_on_spoken_never_fires_when_voice_disabled(tmp_path: Path) -> None:
    sink = tmp_path / "brief.txt"
    hook_calls: list[tuple[str, str]] = []
    ctx = _ctx(text="Text only.")
    stage = BriefDeliverStage(
        sink_path=sink,
        tz=_UTC_TZ,
        voice_enabled=False,
        on_spoken=lambda item_id, text: hook_calls.append((item_id, text)),
    )

    result = await stage.run(ctx)

    assert isinstance(result, Done)
    assert hook_calls == []
    assert "Text only." in sink.read_text(encoding="utf-8")


async def test_ac5_on_spoken_never_fires_when_no_speak_sink_wired(tmp_path: Path) -> None:
    sink = tmp_path / "brief.txt"
    hook_calls: list[tuple[str, str]] = []
    ctx = _ctx(text="Text only, no speak sink.")
    stage = BriefDeliverStage(
        sink_path=sink,
        tz=_UTC_TZ,
        voice_enabled=True,
        speak=None,
        on_spoken=lambda item_id, text: hook_calls.append((item_id, text)),
    )

    result = await stage.run(ctx)

    assert isinstance(result, Done)
    assert hook_calls == []
    assert "Text only, no speak sink." in sink.read_text(encoding="utf-8")


async def test_ac5_on_spoken_never_fires_on_replay(tmp_path: Path) -> None:
    sink = tmp_path / "brief.txt"
    hook_calls: list[tuple[str, str]] = []
    stage = BriefDeliverStage(
        sink_path=sink,
        tz=_UTC_TZ,
        voice_enabled=True,
        speak=lambda _text: None,
        on_spoken=lambda item_id, text: hook_calls.append((item_id, text)),
    )

    await stage.run(_ctx(run_id="run-1"))
    hook_calls.clear()
    result = await stage.run(_ctx(run_id="run-1"))

    assert isinstance(result, Done)
    _delivered_at, _voice_spoken, replay = brief_delivered_from_artifact_data(result.output.data)
    assert replay is True
    assert hook_calls == []


async def test_ac3_raising_on_spoken_hook_is_caught_logs_one_warning_delivery_unaffected(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    sink = tmp_path / "brief.txt"
    ctx = _ctx(text="Text delivery stands.")

    def _boom_hook(_item_id: str, _text: str) -> None:
        raise RuntimeError("on_spoken exploded")

    stage = BriefDeliverStage(
        sink_path=sink,
        tz=_UTC_TZ,
        voice_enabled=True,
        speak=lambda _text: None,
        on_spoken=_boom_hook,
    )

    with caplog.at_level("WARNING"):
        result = await stage.run(ctx)

    assert isinstance(result, Done)
    assert "Text delivery stands." in sink.read_text(encoding="utf-8")
    _delivered_at, voice_spoken, _replay = brief_delivered_from_artifact_data(result.output.data)
    assert voice_spoken is True  # speak() itself worked; only the hook raised
    warnings = [rec for rec in caplog.records if rec.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "on_spoken" in warnings[0].message


# --- AC6: on_spoken defaults to None -------------------------------------------------------------


async def test_on_spoken_none_default_is_a_silent_no_op(tmp_path: Path) -> None:
    sink = tmp_path / "brief.txt"
    ctx = _ctx()
    stage = BriefDeliverStage(sink_path=sink, tz=_UTC_TZ, voice_enabled=True, speak=lambda _t: None)

    result = await stage.run(ctx)

    assert isinstance(result, Done)


# --- AC4: run-id-keyed marker idempotency (the important one) -----------------------------------


async def test_ac4_same_run_id_replay_does_not_double_append_or_speak_again(
    tmp_path: Path,
) -> None:
    sink = tmp_path / "brief.txt"
    spoken: list[str] = []
    stage = BriefDeliverStage(
        sink_path=sink, tz=_UTC_TZ, voice_enabled=True, speak=spoken.append
    )

    ctx1 = _ctx(run_id="run-1")
    first_result = await stage.run(ctx1)
    assert isinstance(first_result, Done)
    first_content = sink.read_text(encoding="utf-8")
    _delivered_at, _voice_spoken, first_replay = brief_delivered_from_artifact_data(
        first_result.output.data
    )
    assert first_replay is False
    assert spoken == ["Here's your brief."]

    # SAME run_id, same ctx shape -> replay: no second append, no second stdout echo (not
    # asserted directly here, just no content growth), no second speak call.
    ctx1_again = _ctx(run_id="run-1")
    second_result = await stage.run(ctx1_again)
    assert isinstance(second_result, Done)
    second_content = sink.read_text(encoding="utf-8")

    assert second_content == first_content  # NOT appended twice
    assert spoken == ["Here's your brief."]  # speak NOT called again
    _delivered_at2, voice_spoken2, second_replay = brief_delivered_from_artifact_data(
        second_result.output.data
    )
    assert second_replay is True
    assert voice_spoken2 is False

    # A DIFFERENT run_id DOES append a second block.
    ctx2 = _ctx(run_id="run-2")
    third_result = await stage.run(ctx2)
    assert isinstance(third_result, Done)
    third_content = sink.read_text(encoding="utf-8")

    assert third_content != second_content
    assert "[run=run-2]" in third_content
    assert spoken == ["Here's your brief.", "Here's your brief."]
    _delivered_at3, voice_spoken3, third_replay = brief_delivered_from_artifact_data(
        third_result.output.data
    )
    assert third_replay is False
    assert voice_spoken3 is True


async def test_ac4_run_id_prefix_collision_is_not_a_false_positive_replay(
    tmp_path: Path,
) -> None:
    """A run id that is a string-prefix of another (e.g. ``run-1`` vs ``run-10``) must not
    false-positive match via a naive substring scan (the bracketed ``[run=...]`` marker guards
    against this)."""
    sink = tmp_path / "brief.txt"
    stage = BriefDeliverStage(sink_path=sink, tz=_UTC_TZ, voice_enabled=False)

    await stage.run(_ctx(run_id="run-1"))
    result = await stage.run(_ctx(run_id="run-10"))

    assert isinstance(result, Done)
    _delivered_at, _voice_spoken, replay = brief_delivered_from_artifact_data(result.output.data)
    assert replay is False
    content = sink.read_text(encoding="utf-8")
    assert "[run=run-1]" in content
    assert "[run=run-10]" in content


# --- no model call: the stage has no model dependency at all ------------------------------------


def test_stage_has_no_model_dependency() -> None:
    import inspect

    sig = inspect.signature(BriefDeliverStage.__init__)
    assert "model" not in sig.parameters
    # Structural: run() never references ctx.model anywhere in its source.
    source = inspect.getsource(BriefDeliverStage.run)
    assert "ctx.model" not in source


# --- never touches ctx.journal --------------------------------------------------------------------


async def test_stage_never_touches_ctx_journal(tmp_path: Path) -> None:
    sink = tmp_path / "brief.txt"
    ctx = _JournalSpyStageContext(
        now_fn=lambda: _NOW,
        last_output_map={"brief_compose": _brief_text_artifact()},
        run_id="run-1",
    )
    stage = BriefDeliverStage(sink_path=sink, tz=_UTC_TZ, voice_enabled=False)

    await stage.run(ctx)

    assert ctx.journal_accessed is False


# --- no brief_compose output yet -> raises (nothing to deliver) --------------------------------


async def test_no_brief_compose_output_raises(tmp_path: Path) -> None:
    sink = tmp_path / "brief.txt"
    ctx = StageContextFake(now_fn=lambda: _NOW, run_id="run-1")
    stage = BriefDeliverStage(sink_path=sink, tz=_UTC_TZ, voice_enabled=False)

    with pytest.raises(RuntimeError):
        await stage.run(ctx)


# --- pathway wiring sanity ------------------------------------------------------------------------


def test_stage_name_and_terminal_transitions() -> None:
    assert BriefDeliverStage.name == "brief_deliver"
    assert BriefDeliverStage.transitions == ()
