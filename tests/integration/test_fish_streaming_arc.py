"""tests/integration/test_fish_streaming_arc.py — TK-333 acceptance criteria (DEC-73 done-bar,
EP-31).

Closes the DEC-73 latency arc on the fully-landed streaming path: TK-330 (``transport.stream``),
TK-331 (``voice.stream_playback.StreamingAudioWriter``, ``STREAM_SAMPLE_RATE=44100``), TK-332
(``FishAudioTTSAdapter``'s streaming half, ``PartialSpeechError``, ``SpeakSink``'s
``spoken=True/degraded=True`` partial ruling). TESTS ONLY — no ``src`` edits. ZERO live network
calls and ZERO real audio playback throughout (DEF-7); every test rides a fake ``VoiceTransport``/
``StreamingVoiceTransport`` + a fake ``AudioOutputStream``/``AudioPlayer``.

(1) ORDERING E2E: ``test_ordering_e2e_...`` — a scripted chunk iterator and a recording fake
writer share ONE emission log, proving TIME-TO-FIRST-SOUND by event ordering (never a wall-clock
sleep): the first ``writer.write`` lands after the first chunk is produced and BEFORE the
transport's log reaches its LAST chunk; every chunk plays in arrival order; ``speak()`` returns
only after the writer drains.

(2) PARTIAL-FAILURE E2E: ``test_partial_failure_e2e_...`` — a mid-stream transport death after k
chunks, driven through the REAL streaming ``FishAudioTTSAdapter`` wired into the REAL
``SpeakSink``: ``on_spoken`` fires exactly once plus ONE loud WARNING, text delivery (the composed
artifact) is unaffected. A pre-audio death (before any chunk ever arrives) takes today's plain
adapter-failure degrade instead — no ``on_spoken``. ``..._through_assembled_fallback_adapter_...``
re-drives the SAME mid-stream scenario through the REAL ``voice.select.FallbackTTSAdapter``
wrapper (the ISS-39 reachability lesion, TK-332 AC5) — every Fish primary is always wrapped this
way in production, and the wrapper must re-raise ``PartialSpeechError`` unchanged, never attempting
a duplicate local-fallback speech.

(3) DEC-72 INTERPLAY PIN: ``test_dec72_interplay_e2e_...`` — a subset-tagged shaped reply is
validated WHOLE-TEXT by ``SpeechShapeStage`` before any streaming byte ever leaves (validate-
then-send stands in phase 1: streaming is transport-level, the emission-policy validator gates the
complete text); the validated bracket text then streams verbatim. An out-of-set opening tag is
rejected to silence before ``speak()`` is ever called, so a streaming-wired adapter sees ZERO
transport calls either — DEC-72i holds under streaming too.

(4) BUFFERED BYTE-IDENTITY: ``test_buffered_byte_identity_...`` — with the streaming dependency
genuinely absent (a forced ``ImportError`` on ``sounddevice``, the exact condition ``stream_
playback.streaming_available()`` probes), a ``FishAudioTTSAdapter`` built WITHOUT a
``writer_factory`` speaks via the ORIGINAL buffered path: the request bytes, the TK-262/TK-264
sentinel-normalize-then-validate handling, and the ``winsound.PlaySound`` call are byte-identical
to the pre-arc baseline (``tests/voice/test_tts_fish.py``'s own AC1 shape).
"""

from __future__ import annotations

import io
import logging
import sys
import wave
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, datetime
from importlib.abc import MetaPathFinder
from importlib.machinery import ModuleSpec
from pathlib import Path
from types import ModuleType

import pytest
from cogworx.claims.provenance import Artifact, Provenance
from cogworx.loop.result import Degraded, Done, Transition
from cogworx.model.base import ModelResponse, Usage

from tests.support.stage_context_fake import FakeModel, StageContextFake
from wombat.config import WombatConfig
from wombat.gate.models import ItemKind
from wombat.sinks.speak import SpeakSink
from wombat.stages.artifacts import (
    COMPOSED_OUTPUT,
    composed_output_to_artifact_data,
    speech_output_from_artifact_data,
    speech_output_to_artifact_data,
    spoken_output_from_artifact_data,
)
from wombat.stages.speech_shape import SpeechShapeStage
from wombat.voice.expressive import ALLOWED_TAGS, find_disallowed_token
from wombat.voice.playback import WinsoundPlayer
from wombat.voice.select import FallbackTTSAdapter
from wombat.voice.stream_playback import (
    STREAM_SAMPLE_RATE,
    StreamingAudioWriter,
    streaming_available,
)
from wombat.voice.transport import VoiceTransportError
from wombat.voice.tts import FISH_AUDIO_TTS_URL, FishAudioTTSAdapter

_FIXED_NOW = datetime(2026, 7, 31, 12, 0, 0, tzinfo=UTC)
_ITEM_ID = "gate-item-streaming-arc-1"
_ITEM_KIND = ItemKind.GENERIC
_COMPOSED_TEXT = "Your first meeting is at nine and nothing else needs you before then."
_SPOKEN_TEXT = "You have a new alert."
_VOICE_ID = "voice-jims-clone"
_FISH_API_KEY = "fish-secret-arc-key"


@pytest.fixture(autouse=True)
def _no_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """TK-202/Q-103: chdir off the repo root so pydantic-settings' ``env_file=".env"`` resolution
    can never pick up the operator's populated .env — mirrors ``tests/integration/test_fish_
    expressive_arc.py``'s own fixture, autouse since every ``WombatConfig`` built here must stay
    isolated from Jim's real voice-provider settings."""
    monkeypatch.chdir(tmp_path)


# ------------------------------------------------------------------------------------------ fakes


class _RecordingFakeTransport:
    """Records the ONE ``post()`` call and returns a caller-supplied body — never touches the
    network (DEF-7)."""

    def __init__(self, *, body: bytes) -> None:
        self._body = body
        self.calls: list[dict[str, object]] = []

    def post(
        self,
        url: str,
        *,
        headers: dict[str, str],
        content: bytes | None = None,
        json: dict[str, object] | None = None,
        data: dict[str, str] | None = None,
        files: dict[str, tuple[str, bytes]] | None = None,
    ) -> tuple[int, bytes]:
        self.calls.append({"url": url, "headers": headers, "content": content, "json": json})
        return 200, self._body


class _RecordingFakePlayer:
    """Records every ``play()`` call — never touches real audio hardware."""

    def __init__(self) -> None:
        self.calls: list[bytes] = []

    def play(self, wav_bytes: bytes) -> None:
        self.calls.append(wav_bytes)


class _RecordingFakeLocalTTS:
    """Stands in for the local fallback adapter (``Pyttsx3Adapter``'s shape) inside an assembled
    ``voice.select.FallbackTTSAdapter`` — records every ``speak()`` call so a test can prove it was
    NEVER reached (TK-332 AC5: partial playback must never trigger a duplicate fallback speech)."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def speak(self, text: str) -> None:
        self.calls.append(text)


class _ScriptedStreamTransport:
    """A fake satisfying ``StreamingVoiceTransport`` — records the ONE ``stream()`` call and hands
    back a caller-scripted generator; ``post()`` always raises (the streaming path must never fall
    back to it). Mirrors ``tests/voice/test_tts_fish.py``'s own fake (DEF-7: never touches the
    network)."""

    def __init__(self, stream_fn: Callable[[], Iterator[bytes]]) -> None:
        self._stream_fn = stream_fn
        self.calls: list[dict[str, object]] = []

    def post(
        self,
        url: str,
        *,
        headers: dict[str, str],
        content: bytes | None = None,
        json: dict[str, object] | None = None,
        data: dict[str, str] | None = None,
        files: dict[str, tuple[str, bytes]] | None = None,
    ) -> tuple[int, bytes]:
        raise AssertionError("the streaming path must never call post()")

    def stream(
        self, url: str, *, headers: dict[str, str], json: dict[str, object] | None = None
    ) -> Iterator[bytes]:
        self.calls.append({"url": url, "headers": headers, "json": json})
        yield from self._stream_fn()


class _RecordingFakeStreamWithLog:
    """A fake ``AudioOutputStream`` (``voice.stream_playback``'s protocol) that logs every write
    into a SHARED emission log — the SAME log the transport's own scripted chunk iterator appends
    to — so the two sides' timing can be compared by pure event ordering (never a wall-clock
    sleep, TK-333)."""

    def __init__(self, events: list[str]) -> None:
        self._events = events
        self.writes: list[bytes] = []

    def write(self, data: bytes) -> None:
        index = len(self.writes)
        self.writes.append(data)
        self._events.append(f"write:{index}")

    def stop(self) -> None:
        self._events.append("stop")

    def abort(self) -> None:
        self._events.append("abort")

    def close(self) -> None:
        self._events.append("close")


def _scripted_chunks_with_log(chunks: Sequence[bytes], events: list[str]) -> Iterator[bytes]:
    """Yields ``chunks`` in order, logging ``"chunk:N"`` into the SHARED ``events`` list
    immediately BEFORE each chunk is handed to the caller — records exactly when the transport
    produced each chunk, interleaved with the writer's own write-log entries."""
    for index, chunk in enumerate(chunks):
        events.append(f"chunk:{index}")
        yield chunk


def _dying_after_k_chunks(chunks: Sequence[bytes], k: int) -> Iterator[bytes]:
    """Yields the first ``k`` of ``chunks`` then raises ``VoiceTransportError`` — the real
    mid-stream-death shape ``HttpxVoiceTransport.stream`` produces."""
    yield from chunks[:k]
    raise VoiceTransportError("voice transport stream POST ... failed mid-stream (simulated)")


def _dies_before_first_chunk() -> Iterator[bytes]:
    """A transport death BEFORE any chunk ever arrives — the real non-2xx-before-first-chunk
    shape."""
    raise VoiceTransportError("voice transport stream POST ... returned 401 (simulated)")
    yield b""  # pragma: no cover - unreachable; keeps this a generator function


def _config(**overrides: object) -> WombatConfig:
    values: dict[str, object] = {
        "deepseek_api_key": "sk-test",
        "deepseek_base_url": "https://api.deepseek.com",
    }
    values.update(overrides)
    return WombatConfig(**values)  # type: ignore[arg-type]


def _compose_artifact(text: str = _COMPOSED_TEXT) -> Artifact:
    return Artifact(
        kind=COMPOSED_OUTPUT,
        produced_by="compose",
        provenance=Provenance(source="system", confidence=1.0, recorded_at=_FIXED_NOW),
        data=composed_output_to_artifact_data(text, _ITEM_ID, _ITEM_KIND, False),
    )


def _speech_output_artifact(text: str | None, *, degraded: bool = False) -> Artifact:
    return Artifact(
        kind="wombat.speech_output",
        produced_by="speech_shape",
        provenance=Provenance(source="system", confidence=1.0, recorded_at=_FIXED_NOW),
        data=speech_output_to_artifact_data(_ITEM_ID, _ITEM_KIND, text, degraded),
    )


def _response(text: str) -> ModelResponse:
    return ModelResponse(
        text=text,
        model_id="deepseek-chat",
        finish_reason="stop",
        usage=Usage(prompt_tokens=10, completion_tokens=5),
    )


# ----------------------------------------------------------------------------- (1) ORDERING E2E


def test_ordering_e2e_first_sound_precedes_last_chunk_all_chunks_play_in_order() -> None:
    """AC1: drives the ASSEMBLED ``FishAudioTTSAdapter`` streaming path (real transport.stream +
    real ``StreamingAudioWriter``) with a scripted chunk iterator and a recording fake writer
    sharing ONE emission log. The FIRST ``writer.write`` lands after the first chunk is produced
    and BEFORE the transport's log reaches its LAST chunk — time-to-first-sound proven by event
    ordering, never a wall-clock sleep. Every chunk plays in arrival order; ``speak()`` returns
    only after the writer drains."""
    events: list[str] = []
    chunks = [b"\x01\x00\x02\x00", b"\x03\x00\x04\x00", b"\x05\x00\x06\x00", b"\x07\x00\x08\x00"]
    fake_stream = _RecordingFakeStreamWithLog(events)
    writer = StreamingAudioWriter(stream_factory=lambda: fake_stream)
    transport = _ScriptedStreamTransport(
        stream_fn=lambda: _scripted_chunks_with_log(chunks, events)
    )
    player = _RecordingFakePlayer()
    adapter = FishAudioTTSAdapter(
        "fish-secret",
        voice_id="voice-abc123",
        model="s2.1-pro",
        transport=transport,
        player=player,
        writer_factory=lambda: writer,
    )

    adapter.speak("Hello streaming world.")

    assert fake_stream.writes == chunks  # every chunk plays, in arrival order
    first_write_index = events.index("write:0")
    first_chunk_index = events.index("chunk:0")
    last_chunk_index = events.index(f"chunk:{len(chunks) - 1}")
    assert first_chunk_index < first_write_index  # a write always follows its own chunk's arrival
    assert first_write_index < last_chunk_index  # TIME-TO-FIRST-SOUND: playback starts before the
    #                                               transport has even produced its final chunk
    assert events[-1] == "close"  # speak() returns only after finish() drains, then closes
    assert player.calls == []  # the buffered player is never touched on the streaming path


# ------------------------------------------------------------------------ (2) PARTIAL-FAILURE E2E


async def test_partial_failure_e2e_mid_stream_death_fires_on_spoken_once_with_loud_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC2: a mid-stream transport death after k chunks, driven through the REAL streaming
    ``FishAudioTTSAdapter`` wired into the REAL ``SpeakSink`` — ``on_spoken`` fires EXACTLY once,
    ONE loud WARNING is logged, and text delivery (the composed artifact) is unaffected."""
    events: list[str] = []
    fake_stream = _RecordingFakeStreamWithLog(events)
    writer = StreamingAudioWriter(stream_factory=lambda: fake_stream)
    chunks = [b"\x01\x00\x02\x00", b"\x03\x00\x04\x00", b"\x05\x00\x06\x00"]
    transport = _ScriptedStreamTransport(stream_fn=lambda: _dying_after_k_chunks(chunks, 2))
    adapter = FishAudioTTSAdapter(
        "fish-secret",
        voice_id="voice-abc123",
        model="s2.1-pro",
        transport=transport,
        player=_RecordingFakePlayer(),
        writer_factory=lambda: writer,
    )
    spoken_calls: list[tuple[str, str]] = []
    stage = SpeakSink(
        voice_enabled=True,
        adapter=adapter,
        on_spoken=lambda item_id, text: spoken_calls.append((item_id, text)),
    )
    compose_artifact = _compose_artifact()
    snapshot = compose_artifact.model_copy(deep=True)
    ctx = StageContextFake(
        now_fn=lambda: _FIXED_NOW,
        last_output_map={
            "compose": compose_artifact,
            "speech_shape": _speech_output_artifact(_SPOKEN_TEXT),
        },
    )

    with caplog.at_level(logging.WARNING):
        result = await stage.run(ctx)

    assert isinstance(result, Degraded)
    assert result.to is None
    _item_id, _item_kind, spoken, degraded = spoken_output_from_artifact_data(result.output.data)
    assert spoken is True  # played-partial-counts-as-spoken, DEC-73e
    assert degraded is True
    assert spoken_calls == [(_ITEM_ID, _SPOKEN_TEXT)]  # fired EXACTLY once
    warning_count = sum("partial" in record.message.lower() for record in caplog.records)
    assert warning_count == 1  # ONE loud WARNING, not zero, not several
    assert compose_artifact == snapshot  # text delivery unaffected


async def test_partial_failure_e2e_through_assembled_fallback_adapter_still_fires_on_spoken_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """ISS-39 reachability lesion (TK-332 AC5 / TK-333): in the real assembled runtime, every Fish
    primary is wrapped in ``voice.select.FallbackTTSAdapter`` BEFORE ``SpeakSink`` ever sees it —
    driving the SAME mid-stream-death scenario through that wrapper (rather than handing SpeakSink
    a bare ``FishAudioTTSAdapter``, as the test above does) proves ``PartialSpeechError`` re-raises
    through the wrapper unchanged, with NO fallback speech attempted, so ``SpeakSink``'s own
    dedicated partial-speech handling still runs in production."""
    events: list[str] = []
    fake_stream = _RecordingFakeStreamWithLog(events)
    writer = StreamingAudioWriter(stream_factory=lambda: fake_stream)
    chunks = [b"\x01\x00\x02\x00", b"\x03\x00\x04\x00", b"\x05\x00\x06\x00"]
    transport = _ScriptedStreamTransport(stream_fn=lambda: _dying_after_k_chunks(chunks, 2))
    primary = FishAudioTTSAdapter(
        "fish-secret",
        voice_id="voice-abc123",
        model="s2.1-pro",
        transport=transport,
        player=_RecordingFakePlayer(),
        writer_factory=lambda: writer,
    )
    fallback = _RecordingFakeLocalTTS()
    adapter = FallbackTTSAdapter(primary, fallback=fallback)
    spoken_calls: list[tuple[str, str]] = []
    stage = SpeakSink(
        voice_enabled=True,
        adapter=adapter,
        on_spoken=lambda item_id, text: spoken_calls.append((item_id, text)),
    )
    compose_artifact = _compose_artifact()
    snapshot = compose_artifact.model_copy(deep=True)
    ctx = StageContextFake(
        now_fn=lambda: _FIXED_NOW,
        last_output_map={
            "compose": compose_artifact,
            "speech_shape": _speech_output_artifact(_SPOKEN_TEXT),
        },
    )

    with caplog.at_level(logging.WARNING):
        result = await stage.run(ctx)

    assert isinstance(result, Degraded)
    assert result.to is None
    _item_id, _item_kind, spoken, degraded = spoken_output_from_artifact_data(result.output.data)
    assert spoken is True  # played-partial-counts-as-spoken, even through the wrapper
    assert degraded is True
    assert spoken_calls == [(_ITEM_ID, _SPOKEN_TEXT)]  # fired EXACTLY once
    assert fallback.calls == []  # no duplicate fallback speech (TK-332 AC5, ISS-39 f1)
    warning_count = sum("partial" in record.message.lower() for record in caplog.records)
    assert warning_count == 1  # ONE loud WARNING, not zero, not several
    assert compose_artifact == snapshot  # text delivery unaffected


async def test_partial_failure_e2e_pre_audio_death_degrades_without_on_spoken() -> None:
    """AC2: a transport death BEFORE any chunk ever arrives propagates unwrapped from the adapter,
    so ``SpeakSink`` takes today's plain adapter-failure degrade — NO ``on_spoken``,
    ``spoken=False``."""
    events: list[str] = []
    fake_stream = _RecordingFakeStreamWithLog(events)
    writer = StreamingAudioWriter(stream_factory=lambda: fake_stream)
    transport = _ScriptedStreamTransport(stream_fn=_dies_before_first_chunk)
    adapter = FishAudioTTSAdapter(
        "fish-secret",
        voice_id="voice-abc123",
        model="s2.1-pro",
        transport=transport,
        player=_RecordingFakePlayer(),
        writer_factory=lambda: writer,
    )
    spoken_calls: list[tuple[str, str]] = []
    stage = SpeakSink(
        voice_enabled=True,
        adapter=adapter,
        on_spoken=lambda item_id, text: spoken_calls.append((item_id, text)),
    )
    compose_artifact = _compose_artifact()
    ctx = StageContextFake(
        now_fn=lambda: _FIXED_NOW,
        last_output_map={
            "compose": compose_artifact,
            "speech_shape": _speech_output_artifact(_SPOKEN_TEXT),
        },
    )

    result = await stage.run(ctx)

    assert isinstance(result, Degraded)
    assert result.to is None
    _item_id, _item_kind, spoken, degraded = spoken_output_from_artifact_data(result.output.data)
    assert spoken is False  # today's degrade, byte-identical to a plain adapter failure
    assert degraded is True
    assert spoken_calls == []  # NO on_spoken
    assert events == []  # the writer was never touched at all


# --------------------------------------------------------------------------- (3) DEC-72 INTERPLAY


async def test_dec72_interplay_e2e_validated_tagged_reply_streams_verbatim() -> None:
    """AC3: validate-then-send stands under streaming too (DEC-72i) — ``SpeechShapeStage``
    validates the WHOLE tagged reply before ``speech_shape`` ever returns it, and only THEN does
    the streaming ``FishAudioTTSAdapter`` send it: the validated bracket text rides
    ``transport.stream()`` VERBATIM (format ``"pcm"``, the buffered player untouched)."""
    tagged_reply = (
        "[soft tone] Your first meeting is at nine. [break] Nothing else needs you before then."
    )
    model = FakeModel(response=_response(tagged_reply))
    events: list[str] = []
    fake_stream = _RecordingFakeStreamWithLog(events)
    writer = StreamingAudioWriter(stream_factory=lambda: fake_stream)
    transport = _ScriptedStreamTransport(stream_fn=lambda: iter([b"\x01\x00\x02\x00"]))
    player = _RecordingFakePlayer()
    adapter = FishAudioTTSAdapter(
        _FISH_API_KEY,
        voice_id=_VOICE_ID,
        model="s2.1-pro",
        transport=transport,
        player=player,
        writer_factory=lambda: writer,
    )
    compose_artifact = _compose_artifact()

    shape_stage = SpeechShapeStage(
        config=_config(), voice_enabled=True, adapter_present=True, expressive_tags=True
    )
    shape_ctx = StageContextFake(
        now_fn=lambda: _FIXED_NOW, model_fake=model, last_output_map={"compose": compose_artifact}
    )
    shape_result = await shape_stage.run(shape_ctx)
    assert isinstance(shape_result, Transition)
    # DEC-72i re-proven independently: the exact text speech_shape validated is what will be sent.
    assert find_disallowed_token(tagged_reply, ALLOWED_TAGS) is None

    speak_stage = SpeakSink(voice_enabled=True, adapter=adapter)
    speak_ctx = StageContextFake(
        now_fn=lambda: _FIXED_NOW,
        last_output_map={"compose": compose_artifact, "speech_shape": shape_result.output},
    )
    speak_result = await speak_stage.run(speak_ctx)

    assert isinstance(speak_result, Done)
    assert len(transport.calls) == 1
    call = transport.calls[0]
    assert call["json"] == {
        "text": tagged_reply,
        "reference_id": _VOICE_ID,
        "format": "pcm",
        "sample_rate": STREAM_SAMPLE_RATE,
        "latency": "low",
    }
    assert player.calls == []  # streaming, not buffered


async def test_dec72_interplay_e2e_out_of_set_tag_zero_transport_calls_under_streaming() -> None:
    """AC3, no-placebo half: an out-of-set opening tag is rejected to silence by
    ``SpeechShapeStage`` BEFORE ``speak()`` is ever called — ZERO transport calls (neither
    ``post()`` nor ``stream()``) even though the adapter is streaming-wired (DEC-72i under
    streaming); text delivery is unaffected."""
    out_of_set_reply = "[screaming] Your first meeting is at nine."
    model = FakeModel(response=_response(out_of_set_reply))
    events: list[str] = []
    fake_stream = _RecordingFakeStreamWithLog(events)
    writer = StreamingAudioWriter(stream_factory=lambda: fake_stream)
    transport = _ScriptedStreamTransport(stream_fn=_dies_before_first_chunk)
    adapter = FishAudioTTSAdapter(
        _FISH_API_KEY,
        voice_id=_VOICE_ID,
        model="s2.1-pro",
        transport=transport,
        player=_RecordingFakePlayer(),
        writer_factory=lambda: writer,
    )
    compose_artifact = _compose_artifact()
    snapshot = compose_artifact.model_copy(deep=True)

    shape_stage = SpeechShapeStage(
        config=_config(), voice_enabled=True, adapter_present=True, expressive_tags=True
    )
    shape_ctx = StageContextFake(
        now_fn=lambda: _FIXED_NOW, model_fake=model, last_output_map={"compose": compose_artifact}
    )
    shape_result = await shape_stage.run(shape_ctx)
    assert isinstance(shape_result, Transition)
    _sp_item_id, _sp_item_kind, sp_text, sp_degraded = speech_output_from_artifact_data(
        shape_result.output.data
    )
    assert sp_text is None
    assert sp_degraded is True  # rejected to silence — DEC-55f no-placebo posture

    speak_stage = SpeakSink(voice_enabled=True, adapter=adapter)
    speak_ctx = StageContextFake(
        now_fn=lambda: _FIXED_NOW,
        last_output_map={"compose": compose_artifact, "speech_shape": shape_result.output},
    )
    speak_result = await speak_stage.run(speak_ctx)

    assert isinstance(speak_result, Degraded)
    assert speak_result.to is None
    assert transport.calls == []  # ZERO provider contact — post() AND stream() both untouched
    assert events == []  # the writer was never touched
    assert compose_artifact == snapshot  # text delivery unaffected


# ----------------------------------------------------------------------- (4) BUFFERED BYTE-IDENTITY


class _BlockedFinder(MetaPathFinder):
    """A meta-path finder that fails the import of one named module (and its submodules)."""

    def __init__(self, blocked: str) -> None:
        self._blocked = blocked

    def find_spec(
        self, fullname: str, path: Sequence[str] | None, target: ModuleType | None = None
    ) -> ModuleSpec | None:
        if fullname == self._blocked or fullname.startswith(f"{self._blocked}."):
            raise ModuleNotFoundError(f"No module named {fullname!r} (simulated absence, TK-333)")
        return None


def _simulate_absent(monkeypatch: pytest.MonkeyPatch, module_name: str) -> None:
    """Simulate ``module_name`` being genuinely not installed (TK-202/Q-103), robust to the
    module actually being present."""
    monkeypatch.delitem(sys.modules, module_name, raising=False)
    monkeypatch.setattr(sys, "meta_path", [_BlockedFinder(module_name), *sys.meta_path])


def _make_wav_bytes(*, nframes: int = 8, nchannels: int = 1, sampwidth: int = 2) -> bytes:
    """A small, well-formed WAV buffer, built via the stdlib ``wave`` module (never hand-rolled)."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav_file:
        wav_file.setnchannels(nchannels)
        wav_file.setsampwidth(sampwidth)
        wav_file.setframerate(8000)
        wav_file.writeframes(b"\x00" * (nframes * nchannels * sampwidth))
    return buf.getvalue()


@pytest.mark.skipif(sys.platform != "win32", reason="winsound is Windows-only (CST-1)")
def test_buffered_byte_identity_streaming_dep_absent_matches_pre_arc_baseline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC4: with ``sounddevice`` genuinely absent (a forced ``ImportError`` — the exact condition
    ``stream_playback.streaming_available()`` probes), a ``FishAudioTTSAdapter`` built WITHOUT a
    ``writer_factory`` speaks via the ORIGINAL buffered path: request bytes (``format: "wav"``),
    TK-262/TK-264 sentinel-normalize-then-validate handling, and the ``winsound.PlaySound`` call
    are byte-identical to the pre-arc baseline (``tests/voice/test_tts_fish.py``'s own AC1)."""
    import winsound

    _simulate_absent(monkeypatch, "sounddevice")
    assert streaming_available() is False

    wav_bytes = _make_wav_bytes(nframes=100)
    transport = _RecordingFakeTransport(body=wav_bytes)
    playsound_calls: list[tuple[bytes, int]] = []
    monkeypatch.setattr(
        winsound, "PlaySound", lambda sound, flags: playsound_calls.append((sound, flags))
    )
    player = WinsoundPlayer()
    # No writer_factory passed at all — mirrors exactly what voice.select builds when the
    # streaming dependency is absent (tests/voice/test_select.py pins that wiring decision;
    # this test proves the RESULTING speak() call is byte-identical to the pre-arc baseline).
    adapter = FishAudioTTSAdapter(
        "fish-secret",
        voice_id="voice-abc123",
        model="s2.1-pro",
        transport=transport,
        player=player,
    )
    assert adapter._writer_factory is None

    adapter.speak("You have a new alert.")

    assert transport.calls == [
        {
            "url": FISH_AUDIO_TTS_URL,
            "headers": {"Authorization": "Bearer fish-secret", "model": "s2.1-pro"},
            "content": None,
            "json": {
                "text": "You have a new alert.",
                "reference_id": "voice-abc123",
                "format": "wav",
            },
        }
    ]
    assert playsound_calls == [(wav_bytes, winsound.SND_MEMORY)]
