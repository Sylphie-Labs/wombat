"""TK-191 acceptance criteria — Fish Audio cloud TTS pattern-setter (EP-31, Q-100, Q-104).

AC1 (success + request shape + exactly-once playback): ``test_speak_sends_expected_request_and_
plays_returned_bytes_exactly_once``, ``test_fish_audio_tts_adapter_satisfies_ttsadapter_
protocol``.
AC2 (transport/player failure raises, then SpeakSink end-to-end degrade, TK-165/CON-3 parity):
``test_speak_raises_on_transport_or_player_failure``,
``test_speak_sink_degrades_to_terminal_on_adapter_failure_text_unaffected``.
AC3 (clean-checkout import bar / lazy httpx+winsound): ``test_tts_module_imports_without_httpx_
installed``, ``test_fish_audio_tts_adapter_construction_with_default_transport_raises_without_
httpx``.

TK-326 (DEC-71a/DEC-72a): every construction now passes ``model="s2.1-pro"`` — the request-shape
assertion in ``test_speak_sends_expected_request_and_plays_returned_bytes_exactly_once`` proves the
``model`` HTTP header rides alongside the untouched ``Authorization`` header and the JSON body
stays byte-identical.

TK-332 (DEC-73a/d/e) adds the Fish streaming speak path — AC1
(``test_speak_streams_via_writer_and_returns_only_after_finish``: request shape + arrival-order
chunk delivery + call-order — every write precedes finish, return follows finish), AC2 (partial
failure: ``test_speak_streaming_mid_stream_transport_death_aborts_and_raises_partial_speech_
error``, ``test_speak_streaming_pre_audio_transport_death_propagates_unwrapped``,
``test_speak_streaming_writer_failure_before_any_write_propagates_unwrapped``).

TK-329 (DEC-72f) adds ONE arming-var-gated LIVE ear-proof:
``test_live_fish_speaks_one_pinned_expressive_utterance`` — armed ONLY when
``WOMBAT_TEST_FISH_LIVE=1`` AND a real ``WOMBAT_FISH_API_KEY``/``WOMBAT_TTS_VOICE_ID`` (Jim's
reference id) resolve via ``load_config()``; LOUD-SKIPS otherwise (the ``_LIVE_ENV`` idiom
precedent: ``tests/integration/test_capability_honesty_live.py``). Speaks exactly one pinned
utterance through the REAL transport/player — costs API credit, NEVER runs in the plain suite.

TK-333 (DEC-73 done-bar) adds a MEASUREMENT mode on the SAME arming var/idiom:
``test_live_fish_measures_time_to_first_sound_buffered_vs_streaming`` — speaks the SAME pinned
utterance BUFFERED then STREAMING through the REAL adapter, timing wall-clock time-to-first-sound
for each half and printing both side by side (Jim's operator evidence for the streaming win; total-
duration throughput is explicitly out of scope). Costs API credit TWICE — NEVER runs in the plain
suite.

Every OTHER test rides a fake ``VoiceTransport`` + fake ``AudioPlayer`` — ZERO live network calls
and ZERO real audio playback (DEF-7).
"""

from __future__ import annotations

import importlib
import os
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, datetime
from importlib.abc import MetaPathFinder
from importlib.machinery import ModuleSpec
from types import ModuleType

import pytest
from cogworx.claims.provenance import Artifact, Provenance
from cogworx.loop.result import Degraded

from tests.support.stage_context_fake import StageContextFake
from wombat.config import ConfigurationError, load_config
from wombat.gate.models import ItemKind
from wombat.sinks.speak import SpeakSink
from wombat.sinks.tts_adapter import TTSAdapter
from wombat.stages.artifacts import (
    COMPOSED_OUTPUT,
    composed_output_to_artifact_data,
    spoken_output_from_artifact_data,
)
from wombat.voice.playback import AudioPlayer, WinsoundPlayer
from wombat.voice.stream_playback import (
    STREAM_SAMPLE_RATE,
    StreamingAudioWriter,
    streaming_available,
)
from wombat.voice.transport import VoiceTransport, VoiceTransportError
from wombat.voice.tts import FISH_AUDIO_TTS_URL, FishAudioTTSAdapter, PartialSpeechError

_WAV_BYTES = b"RIFF....WAVEfmt returned-audio"


class _RecordingFakeTransport:
    """A fake ``VoiceTransport`` that records the ONE call made to it and returns canned WAV
    bytes (AC1) — never touches the network (DEF-7)."""

    def __init__(self, *, status_code: int = 200, body: bytes = _WAV_BYTES) -> None:
        self._status_code = status_code
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
        return self._status_code, self._body


class _RaisingFakeTransport:
    """A fake ``VoiceTransport`` that simulates the real ``HttpxVoiceTransport`` non-2xx
    contract: raises ``VoiceTransportError`` rather than returning a failure status (AC2)."""

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
        raise VoiceTransportError(f"voice transport POST {url} returned 401: 'unauthorized'")


class _RecordingFakePlayer:
    """A fake ``AudioPlayer`` that records every ``play()`` call (AC1) — never touches real audio
    hardware."""

    def __init__(self) -> None:
        self.calls: list[bytes] = []

    def play(self, wav_bytes: bytes) -> None:
        self.calls.append(wav_bytes)


class _RaisingFakePlayer:
    """A fake ``AudioPlayer`` whose ``play()`` always raises (AC2)."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc
        self.calls: list[bytes] = []

    def play(self, wav_bytes: bytes) -> None:
        self.calls.append(wav_bytes)
        raise self._exc


# --- AC1: success + request shape + exactly-once playback --------------------------------------


def test_speak_sends_expected_request_and_plays_returned_bytes_exactly_once() -> None:
    transport = _RecordingFakeTransport()
    player = _RecordingFakePlayer()
    adapter = FishAudioTTSAdapter(
        "fish-secret",
        voice_id="voice-abc123",
        model="s2.1-pro",
        transport=transport,
        player=player,
    )

    adapter.speak("You have a new alert.")

    assert len(transport.calls) == 1
    call = transport.calls[0]
    assert call["url"] == FISH_AUDIO_TTS_URL
    assert call["headers"] == {"Authorization": "Bearer fish-secret", "model": "s2.1-pro"}
    assert call["json"] == {
        "text": "You have a new alert.",
        "reference_id": "voice-abc123",
        "format": "wav",
    }
    assert player.calls == [_WAV_BYTES]


def test_fish_audio_tts_adapter_satisfies_ttsadapter_protocol() -> None:
    """Structural ``TTSAdapter`` conformance via a typed assignment (mypy-checked) — the same
    idiom TK-189's ``DeepgramTranscriber`` test uses."""
    transport = _RecordingFakeTransport()
    player = _RecordingFakePlayer()
    adapter: TTSAdapter = FishAudioTTSAdapter(
        "fish-secret",
        voice_id="voice-abc123",
        model="s2.1-pro",
        transport=transport,
        player=player,
    )
    adapter.speak("hello")
    assert player.calls == [_WAV_BYTES]


# --- AC2: transport/player failure raises, then SpeakSink end-to-end degrade -------------------


@pytest.mark.parametrize(
    ("transport", "player"),
    [
        pytest.param(_RaisingFakeTransport(), _RecordingFakePlayer(), id="transport-failure"),
        pytest.param(
            _RecordingFakeTransport(),
            _RaisingFakePlayer(RuntimeError("playback device busy")),
            id="player-failure",
        ),
    ],
)
def test_speak_raises_on_transport_or_player_failure(
    transport: VoiceTransport, player: _RecordingFakePlayer | _RaisingFakePlayer
) -> None:
    adapter = FishAudioTTSAdapter(
        "fish-secret",
        voice_id="voice-abc123",
        model="s2.1-pro",
        transport=transport,
        player=player,
    )
    with pytest.raises(Exception):  # noqa: B017 — either VoiceTransportError or RuntimeError
        adapter.speak("hello")


_FIXED_NOW = datetime(2026, 7, 9, 12, 0, 0, tzinfo=UTC)
_ITEM_ID = "gate-item-1"
_ITEM_KIND = ItemKind.GENERIC
_TEXT = "You have a new alert."


def _composed_output_artifact() -> Artifact:
    return Artifact(
        kind=COMPOSED_OUTPUT,
        produced_by="compose",
        provenance=Provenance(source="system", confidence=1.0, recorded_at=_FIXED_NOW),
        data=composed_output_to_artifact_data(_TEXT, _ITEM_ID, _ITEM_KIND, False),
    )


@pytest.mark.parametrize(
    ("transport", "player"),
    [
        pytest.param(_RaisingFakeTransport(), _RecordingFakePlayer(), id="transport-failure"),
        pytest.param(
            _RecordingFakeTransport(),
            _RaisingFakePlayer(RuntimeError("playback device busy")),
            id="player-failure",
        ),
    ],
)
async def test_speak_sink_degrades_to_terminal_on_adapter_failure_text_unaffected(
    transport: VoiceTransport, player: _RecordingFakePlayer | _RaisingFakePlayer
) -> None:
    """AC2 end-to-end: the real ``SpeakSink`` wired to a ``FishAudioTTSAdapter`` whose transport
    or player fails degrades to a terminal ``Degraded(to=None)`` carrying ``spoken=False,
    degraded=True`` — the composed text itself is untouched (TK-165 parity, CON-3)."""
    adapter = FishAudioTTSAdapter(
        "fish-secret",
        voice_id="voice-abc123",
        model="s2.1-pro",
        transport=transport,
        player=player,
    )
    stage = SpeakSink(voice_enabled=True, adapter=adapter)
    compose_artifact = _composed_output_artifact()
    snapshot = compose_artifact.model_copy(deep=True)
    ctx = StageContextFake(
        now_fn=lambda: _FIXED_NOW, last_output_map={"compose": compose_artifact}
    )

    result = await stage.run(ctx)

    assert isinstance(result, Degraded)
    assert result.to is None
    assert result.reason
    _item_id, _item_kind, spoken, degraded = spoken_output_from_artifact_data(result.output.data)
    assert spoken is False
    assert degraded is True
    assert compose_artifact == snapshot  # the composed text wire artifact is untouched


# --- TK-332 (DEC-73a/d/e): the Fish streaming speak path -----------------------------------------
#
# NOTE: this section MUST stay ABOVE "AC3: clean-checkout import bar" below — that section's
# ``importlib.reload(wombat.voice.tts)`` mutates the SAME module namespace in place, which would
# otherwise leave ``PartialSpeechError``/``FishAudioTTSAdapter`` (imported once, at collection
# time, at the top of this file) referring to a class object distinct from the one the reloaded
# module's own methods raise/construct — breaking ``isinstance``/``pytest.raises`` identity for
# every test below it. Running before the reload sidesteps the hazard entirely.


class _RecordingFakeStream:
    """A fake ``AudioOutputStream`` (``voice.stream_playback``'s protocol) that records every
    call into a SHARED event log — proves call ordering (every write precedes finish/stop, abort
    never drains) without touching real audio hardware."""

    def __init__(self, events: list[str]) -> None:
        self._events = events
        self.writes: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.writes.append(data)
        self._events.append(f"write:{data!r}")

    def stop(self) -> None:
        self._events.append("stop")

    def abort(self) -> None:
        self._events.append("abort")

    def close(self) -> None:
        self._events.append("close")


class _RaisingOnWriteFakeStream:
    """A fake ``AudioOutputStream`` whose ``write`` always raises — simulates a writer failure on
    the very FIRST chunk, i.e. BEFORE any audio has actually played."""

    def write(self, data: bytes) -> None:
        raise RuntimeError("playback device busy (simulated writer failure, TK-332)")

    def stop(self) -> None:  # pragma: no cover - never reached, no chunk ever writes cleanly
        pass

    def abort(self) -> None:  # pragma: no cover - never reached, played_any stays False
        pass

    def close(self) -> None:  # pragma: no cover - never reached
        pass


class _ScriptedStreamTransport:
    """A fake satisfying ``StreamingVoiceTransport`` (both ``post`` and ``stream``) — records the
    ONE ``stream()`` call and hands back a caller-scripted generator (DEF-7: never touches the
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


def test_speak_streams_via_writer_and_returns_only_after_finish() -> None:
    """AC1: the request carries ``format: "pcm"``, ``sample_rate`` IDENTITY with ``STREAM_SAMPLE_
    RATE``, ``latency: "low"``, the ``model`` header intact; chunks flow to the writer in arrival
    order; ``speak`` returns only after ``finish()`` (call-order assert: every write precedes the
    drain, drain precedes return). The buffered player is never touched."""
    events: list[str] = []
    fake_stream = _RecordingFakeStream(events)
    writer = StreamingAudioWriter(stream_factory=lambda: fake_stream)
    chunks = [b"\x01\x00\x02\x00", b"\x03\x00\x04\x00", b"\x05\x00\x06\x00"]
    transport = _ScriptedStreamTransport(stream_fn=lambda: iter(chunks))
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

    assert len(transport.calls) == 1
    call = transport.calls[0]
    assert call["url"] == FISH_AUDIO_TTS_URL
    assert call["headers"] == {"Authorization": "Bearer fish-secret", "model": "s2.1-pro"}
    assert call["json"] == {
        "text": "Hello streaming world.",
        "reference_id": "voice-abc123",
        "format": "pcm",
        "sample_rate": STREAM_SAMPLE_RATE,
        "latency": "low",
    }
    assert fake_stream.writes == chunks  # arrival order preserved
    write_indices = [i for i, e in enumerate(events) if e.startswith("write:")]
    stop_index = events.index("stop")
    assert write_indices and all(i < stop_index for i in write_indices)  # every write before drain
    assert events[-1] == "close"  # finish() drains (stop) THEN closes, and only then does speak()
    assert "abort" not in events
    assert player.calls == []  # the buffered player is never touched on the streaming path


def test_speak_streaming_mid_stream_transport_death_aborts_and_raises_partial_speech_error() -> (
    None
):
    """AC2: a mid-stream transport death after k chunks aborts the writer (never drains) and
    raises ``PartialSpeechError(played_any=True)``."""
    events: list[str] = []
    fake_stream = _RecordingFakeStream(events)
    writer = StreamingAudioWriter(stream_factory=lambda: fake_stream)

    def _dying_stream() -> Iterator[bytes]:
        yield b"\x01\x00\x02\x00"
        yield b"\x03\x00\x04\x00"
        raise VoiceTransportError("voice transport stream POST ... failed mid-stream")

    transport = _ScriptedStreamTransport(stream_fn=_dying_stream)
    adapter = FishAudioTTSAdapter(
        "fish-secret",
        voice_id="voice-abc123",
        model="s2.1-pro",
        transport=transport,
        player=_RecordingFakePlayer(),
        writer_factory=lambda: writer,
    )

    with pytest.raises(PartialSpeechError) as excinfo:
        adapter.speak("hello")

    assert excinfo.value.played_any is True
    assert fake_stream.writes == [b"\x01\x00\x02\x00", b"\x03\x00\x04\x00"]
    assert "abort" in events
    assert "stop" not in events  # abort, never a drain-finish


def test_speak_streaming_pre_audio_transport_death_propagates_unwrapped() -> None:
    """AC2: a transport death BEFORE any chunk ever arrives (the real non-2xx-before-first-chunk
    shape) propagates the underlying ``VoiceTransportError`` UNCHANGED — never wrapped, never
    touches the writer."""
    events: list[str] = []
    fake_stream = _RecordingFakeStream(events)
    writer = StreamingAudioWriter(stream_factory=lambda: fake_stream)

    def _dies_before_first_chunk() -> Iterator[bytes]:
        raise VoiceTransportError("voice transport stream POST ... returned 401: 'unauthorized'")
        yield b""  # pragma: no cover - unreachable; keeps this a generator function

    transport = _ScriptedStreamTransport(stream_fn=_dies_before_first_chunk)
    adapter = FishAudioTTSAdapter(
        "fish-secret",
        voice_id="voice-abc123",
        model="s2.1-pro",
        transport=transport,
        player=_RecordingFakePlayer(),
        writer_factory=lambda: writer,
    )

    with pytest.raises(VoiceTransportError):
        adapter.speak("hello")

    assert events == []  # the writer was never touched at all


def test_speak_streaming_writer_failure_before_any_write_propagates_unwrapped() -> None:
    """AC2: a writer failure on the FIRST chunk (transport succeeded, playback itself failed
    before any audio actually played) propagates the writer's own exception UNCHANGED, exactly
    like a pre-audio transport death."""
    writer = StreamingAudioWriter(stream_factory=lambda: _RaisingOnWriteFakeStream())
    transport = _ScriptedStreamTransport(
        stream_fn=lambda: iter([b"\x01\x00\x02\x00", b"\x03\x00\x04\x00"])
    )
    adapter = FishAudioTTSAdapter(
        "fish-secret",
        voice_id="voice-abc123",
        model="s2.1-pro",
        transport=transport,
        player=_RecordingFakePlayer(),
        writer_factory=lambda: writer,
    )

    with pytest.raises(RuntimeError, match="playback device busy"):
        adapter.speak("hello")


# --- AC3: clean-checkout import bar --------------------------------------------------------------


class _BlockedFinder(MetaPathFinder):
    """A meta-path finder that fails the import of one named module (and its submodules)."""

    def __init__(self, blocked: str) -> None:
        self._blocked = blocked

    def find_spec(
        self, fullname: str, path: Sequence[str] | None, target: ModuleType | None = None
    ) -> ModuleSpec | None:
        if fullname == self._blocked or fullname.startswith(f"{self._blocked}."):
            raise ModuleNotFoundError(f"No module named {fullname!r} (simulated absence, TK-202)")
        return None


def _simulate_absent(monkeypatch: pytest.MonkeyPatch, module_name: str) -> None:
    """Simulate ``module_name`` being genuinely not installed (TK-202/Q-103), robust to the
    module actually being present."""
    monkeypatch.delitem(sys.modules, module_name, raising=False)
    monkeypatch.setattr(sys, "meta_path", [_BlockedFinder(module_name), *sys.meta_path])


def test_tts_module_imports_without_httpx_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC3: importing ``wombat.voice.tts`` never touches ``httpx`` — only constructing the
    default ``HttpxVoiceTransport`` does."""
    _simulate_absent(monkeypatch, "httpx")
    assert "httpx" not in sys.modules
    importlib.reload(importlib.import_module("wombat.voice.tts"))
    assert "httpx" not in sys.modules


def test_fish_audio_tts_adapter_construction_with_default_transport_raises_without_httpx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC3: constructing a ``FishAudioTTSAdapter`` WITHOUT an explicit ``transport`` (the default
    arg, which lazily builds a real ``HttpxVoiceTransport``) raises ``ImportError`` when the
    ``voice-cloud`` extra is absent — the real, unmocked lazy-import-failure path. An explicit
    ``player`` fake is supplied so only the transport's lazy import is exercised."""
    _simulate_absent(monkeypatch, "httpx")
    with pytest.raises(ImportError):
        FishAudioTTSAdapter(
            "fish-secret",
            voice_id="voice-abc123",
            model="s2.1-pro",
            player=_RecordingFakePlayer(),
        )


# --- TK-329 (DEC-72f): the armed LIVE ear-proof --------------------------------------------------

_LIVE_ENV = "WOMBAT_TEST_FISH_LIVE"

# Pinned per DEC-72f — Jim's operator ear-check judges [break]/[long-break] efficacy on s2.1-pro
# by listening to exactly this utterance; if the pause markers prove inert by ear they drop at
# recalibration (recorded, not guessed).
_LIVE_UTTERANCE = (
    "[soft tone] Your first meeting is at nine. [break] Nothing else needs you before then."
)


def _missing_fish_live_requirements() -> tuple[str, ...]:
    """What's missing to arm the live smoke, resolved LAZILY at each test's SETUP time via the
    ``skipif`` STRING condition below — never at import/collection time (mirrors ``tests/
    integration/test_capability_honesty_live.py``'s ``_missing_live_requirements`` exactly).
    Short-circuits before ever calling ``load_config()`` when ``WOMBAT_TEST_FISH_LIVE`` itself is
    unset (the default, unarmed case)."""
    if not os.environ.get(_LIVE_ENV):
        return (_LIVE_ENV,)
    missing: list[str] = []
    try:
        config = load_config()
    except ConfigurationError:
        missing.append("WOMBAT_FISH_API_KEY/WOMBAT_TTS_VOICE_ID (load_config() failed)")
    else:
        if config.wombat_fish_api_key is None or not (
            config.wombat_fish_api_key.get_secret_value().strip()
        ):
            missing.append("WOMBAT_FISH_API_KEY")
        if not (config.wombat_tts_voice_id or "").strip():
            missing.append("WOMBAT_TTS_VOICE_ID")
    return tuple(missing)


def _fish_live_unarmed() -> bool:
    """The ``skipif`` condition, evaluated by pytest as a STRING at each item's SETUP time — runs
    strictly before any fixture is instantiated."""
    return bool(_missing_fish_live_requirements())


_requires_fish_live = pytest.mark.skipif(
    "_fish_live_unarmed()",
    reason=(
        f"missing {_LIVE_ENV} and/or WOMBAT_FISH_API_KEY/WOMBAT_TTS_VOICE_ID — skipping the live "
        "Fish ear-proof (TK-329, DEC-72f). Export WOMBAT_TEST_FISH_LIVE=1 plus real creds (env "
        "or repo-root .env) to arm this harness — costs API credit, NEVER runs in the plain suite."
    ),
)


@_requires_fish_live
def test_live_fish_speaks_one_pinned_expressive_utterance() -> None:
    """DEC-72f: ONE armed live speak of the pinned utterance through the REAL
    ``FishAudioTTSAdapter`` (real transport, real playback, ``config.wombat_fish_model`` — the
    pinned ``s2.1-pro`` default unless overridden). Jim's ear-check on [break]/[long-break]
    efficacy on s2.1-pro is the operator step; this smoke only proves the call completes without
    raising. Costs API credit — gated behind ``WOMBAT_TEST_FISH_LIVE``, never in the plain suite."""
    config = load_config()
    api_key = config.wombat_fish_api_key
    assert api_key is not None
    adapter = FishAudioTTSAdapter(
        api_key.get_secret_value(),
        voice_id=config.wombat_tts_voice_id or "",
        model=config.wombat_fish_model,
    )

    adapter.speak(_LIVE_UTTERANCE)


# --- TK-333 (DEC-73 done-bar): armed LIVE measurement mode, buffered vs streaming --------------


class _TimingPlayer:
    """Wraps a REAL ``AudioPlayer``, timestamping the FIRST ``play()`` call relative to a
    caller-supplied start time (TK-333) — real playback fires through unchanged; this class exists
    only to observe WHEN it fires."""

    def __init__(self, inner: AudioPlayer, start: float) -> None:
        self._inner = inner
        self._start = start
        self.first_sound_seconds: float | None = None

    def play(self, wav_bytes: bytes) -> None:
        if self.first_sound_seconds is None:
            self.first_sound_seconds = time.perf_counter() - self._start
        self._inner.play(wav_bytes)


class _TimingWriter(StreamingAudioWriter):
    """A REAL ``StreamingAudioWriter`` subclass that timestamps the FIRST ``write()`` call
    relative to a caller-supplied start time (TK-333) — real streamed playback fires through
    unchanged via ``super().write()``."""

    def __init__(self, *, start: float) -> None:
        super().__init__()
        self._start = start
        self.first_sound_seconds: float | None = None

    def write(self, chunk: bytes) -> None:
        if self.first_sound_seconds is None:
            self.first_sound_seconds = time.perf_counter() - self._start
        super().write(chunk)


@_requires_fish_live
def test_live_fish_measures_time_to_first_sound_buffered_vs_streaming() -> None:
    """DEC-73 done-bar (TK-333): speaks the SAME pinned utterance BUFFERED then STREAMING through
    the REAL adapter/transport/player, timing wall-clock time-to-first-sound for each half and
    printing both side by side — Jim's operator evidence for the streaming win (time-to-first-sound
    is the metric; total-duration throughput is explicitly out of scope). Costs API credit TWICE;
    gated behind ``WOMBAT_TEST_FISH_LIVE``, never in the plain suite."""
    config = load_config()
    api_key = config.wombat_fish_api_key
    assert api_key is not None
    voice_id = config.wombat_tts_voice_id or ""
    model = config.wombat_fish_model

    buffered_start = time.perf_counter()
    buffered_player = _TimingPlayer(WinsoundPlayer(), buffered_start)
    buffered_adapter = FishAudioTTSAdapter(
        api_key.get_secret_value(), voice_id=voice_id, model=model, player=buffered_player
    )
    buffered_adapter.speak(_LIVE_UTTERANCE)
    assert buffered_player.first_sound_seconds is not None

    if not streaming_available():
        pytest.skip(
            f"buffered time-to-first-sound={buffered_player.first_sound_seconds:.3f}s; "
            "sounddevice (voice-cloud extra) is not installed -- cannot measure the streaming half"
        )

    streaming_start = time.perf_counter()
    timing_writer = _TimingWriter(start=streaming_start)
    streaming_adapter = FishAudioTTSAdapter(
        api_key.get_secret_value(),
        voice_id=voice_id,
        model=model,
        writer_factory=lambda: timing_writer,
    )
    streaming_adapter.speak(_LIVE_UTTERANCE)
    assert timing_writer.first_sound_seconds is not None

    print(
        "\nTK-333 (DEC-73 done-bar) time-to-first-sound -- "
        f"buffered={buffered_player.first_sound_seconds:.3f}s "
        f"streaming={timing_writer.first_sound_seconds:.3f}s"
    )
