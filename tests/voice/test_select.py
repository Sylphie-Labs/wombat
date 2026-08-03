"""TK-193 acceptance criteria — provider selection + cloud-to-local fallback (EP-31, Q-105(d)).

CI tests use fakes ONLY: an in-memory ``VoiceKeyStore`` fake and monkeypatched provider classes
— NEVER the real keyring (Q-57(a)) and NEVER a live network call (DEF-7). Every ``WombatConfig``
is built under the autouse ``_no_env_file`` fixture (TK-202/Q-103 precedent, mirroring ``tests/
unit/test_runtime.py``'s own fixture): the repo-root ``.env`` carries Jim's REAL
``WOMBAT_TTS_PROVIDER``/``WOMBAT_FISH_API_KEY`` (the whole point of this ticket), which must never
leak into a test process.

  AC1 (local default: exact existing wirings, no cloud construction, no key read):
      ``test_local_stt_provider_builds_faster_whisper_no_cloud_no_key_read``,
      ``test_local_tts_provider_builds_pyttsx3_no_cloud_no_key_read``.
  AC2 (cloud success wraps in Fallback*, degrades to local exactly once on primary failure,
      structural cloud->local-only direction):
      ``test_cloud_stt_wraps_primary_and_degrades_to_local_exactly_once_on_failure``,
      ``test_cloud_tts_wraps_primary_and_degrades_to_local_exactly_once_on_failure``,
      ``test_local_provider_returns_bare_local_instance_never_wrapped``,
      ``test_stt_fallback_slot_is_always_local_type_or_none``,
      ``test_tts_fallback_slot_is_always_local_type_or_none``.
  AC3 (unresolvable key / missing required voice_id / absent voice-cloud extra -> ONE loud log,
      local default used, boot never fails):
      ``test_cloud_stt_unresolvable_key_falls_back_to_local_with_loud_log``,
      ``test_cloud_tts_unresolvable_key_falls_back_to_local_with_loud_log``,
      ``test_cloud_tts_missing_required_voice_id_falls_back_to_local_with_loud_log``,
      ``test_cloud_stt_absent_voice_cloud_extra_falls_back_to_local_with_loud_log``,
      ``test_boot_never_fails_for_a_cloud_voice_misconfiguration``.

TK-326 (DEC-71a/DEC-72a) — the fish branch threads ``config.wombat_fish_model`` into the adapter's
``model`` ctor param; deepgram/elevenlabs/local paths are byte-untouched:
    ``test_cloud_tts_fish_provider_threads_configured_model_into_adapter``.

TK-217 (CR4-1) — the failed-local-fallback warning is contextualized when it fills the fallback
slot of an already-healthy cloud primary (never claims voice is disabled when the cloud primary
still works), while the local-primary path's exact historical message is byte-preserved:
    ``test_tk217_cloud_tts_healthy_primary_contextualizes_local_fallback_failure``,
    ``test_tk217_local_tts_primary_failure_preserves_voice_output_disabled_message``,
    ``test_tk217_cloud_stt_healthy_primary_contextualizes_local_fallback_failure``.

TK-328 (ruling v2.187 r1) — the additive ``build_tts_adapter_with_info`` companion and
``FallbackTTSAdapter`` fallback-branch tag hygiene (AC2; the bootstrap-matrix AC1/AC3 live in
``tests/unit/test_bootstrap.py``):
    ``test_build_tts_adapter_with_info_fish_primary_true_when_actually_constructed``,
    ``test_build_tts_adapter_with_info_degrade_paths_yield_false_none``,
    ``test_build_tts_adapter_with_info_fish_import_error_yields_false_none``,
    ``test_build_tts_adapter_is_thin_wrapper_over_with_info``,
    ``test_fallback_tts_adapter_strips_tags_only_on_the_fallback_branch``,
    ``test_fallback_tts_adapter_healthy_primary_receives_tagged_text_verbatim_no_fallback_call``.

TK-332 AC5 (ISS-39 f1 ruling v2.195) — ``FallbackTTSAdapter.speak`` discriminates
``PartialSpeechError.played_any`` through the wrapper itself (a constructed fallback), the
reachability lesson pinned (an unwrapped-adapter test is exactly how this survived to review):
    ``test_fallback_tts_adapter_partial_speech_played_any_true_reraises_no_fallback_call``,
    ``test_fallback_tts_adapter_partial_speech_played_any_false_falls_back_with_loud_warning``,
    ``test_fallback_tts_adapter_partial_speech_played_any_false_no_fallback_reraises``.

TK-343 (DEC-79) — the origin-aware writer_factory closure (AC2), ``build_tts_adapter_with_info``'s
new optional ``turn_origin_register``/``sealed_utterance_store`` wiring (AC1's byte-identical
default), and ``FallbackTTSAdapter``'s new ``remote_attempt`` no-laptop-rescue behavior (AC7):
    ``test_remote_writer_factory_routes_to_buffered_sink_when_origin_fresh``,
    ``test_remote_writer_factory_routes_local_when_origin_empty_or_stale``,
    ``test_remote_writer_factory_reads_register_at_call_time_two_calls_route_differently``,
    ``test_build_tts_adapter_with_info_wires_remote_closure_when_both_args_given``,
    ``test_build_tts_adapter_with_info_neither_arg_wires_the_bare_class``,
    ``test_build_tts_adapter_with_info_register_only_wires_the_bare_class``,
    ``test_fallback_tts_adapter_remote_attempt_marked_reraises_on_played_any_false``,
    ``test_fallback_tts_adapter_remote_attempt_marked_reraises_on_bare_exception``,
    ``test_fallback_tts_adapter_remote_attempt_unmarked_falls_back_normally``,
    ``test_fallback_tts_adapter_remote_attempt_is_consumed_only_once``.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable, Sequence
from importlib.abc import MetaPathFinder
from importlib.machinery import ModuleSpec
from pathlib import Path
from types import ModuleType
from typing import Any
from zoneinfo import ZoneInfo

import pytest

import wombat.voice.select as select_module
from wombat.bootstrap import build_speak_sink
from wombat.config import WombatConfig
from wombat.queue import EnqueueResult, QueueItem
from wombat.sources.bootstrap import build_source_registry
from wombat.sources.registry import SourceRegistry
from wombat.voice import tts as voice_tts
from wombat.voice.remote_sinks import BufferedUtteranceSink, SealedUtteranceStore
from wombat.voice.select import (
    FallbackTranscriber,
    FallbackTTSAdapter,
    build_transcriber,
    build_tts_adapter,
)
from wombat.voice.stream_playback import StreamingAudioWriter
from wombat.voice.tts import FishAudioTTSAdapter
from wombat.voice.turn_origin import LastTurnOriginRegister


def _config(**overrides: object) -> WombatConfig:
    values: dict[str, object] = {
        "deepseek_api_key": "sk-test",
        "deepseek_base_url": "https://api.deepseek.com",
    }
    values.update(overrides)
    return WombatConfig(**values)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _no_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """TK-202/Q-103: chdir off the repo root so pydantic-settings' ``env_file=".env"``
    resolution (relative to CWD) can never pick up the operator's populated .env — every
    ``WombatConfig`` built by ``_config`` in this module is isolated from Jim's real
    ``WOMBAT_TTS_PROVIDER``/``WOMBAT_FISH_API_KEY`` (mirrors ``tests/unit/test_runtime.py``'s own
    ``_no_env_file`` fixture, made autouse here since EVERY test in this module needs it)."""
    monkeypatch.chdir(tmp_path)


# ------------------------------------------------------------------------------------------ fakes


class _FakeVoiceKeyStore:
    """The in-memory fake — unit tests never touch the real vault (Q-57(a))."""

    def __init__(self, *, initial: dict[str, str] | None = None) -> None:
        self._values = dict(initial or {})

    def get(self, provider: str) -> str | None:
        return self._values.get(provider)

    def set(self, provider: str, key: str) -> None:
        self._values[provider] = key

    def delete(self, provider: str) -> None:
        self._values.pop(provider, None)


class _UnreadableVoiceKeyStore:
    """Proves AC1's 'no key store read occurs' on the local path — any read fails the test."""

    def get(self, provider: str) -> str | None:
        raise AssertionError(
            f"the local provider path must never read the key store (got {provider!r})"
        )

    def set(self, provider: str, key: str) -> None:
        raise AssertionError("not exercised")

    def delete(self, provider: str) -> None:
        raise AssertionError("not exercised")


class _RaisingIfConstructed:
    """Stands in for a cloud provider class that fails the test the instant it is instantiated —
    proves 'NO cloud class is constructed' on the local path."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("a cloud provider class must never be constructed on the local path")


class _FakeLocalTranscriber:
    """Stands in for ``FasterWhisperTranscriber`` — proves the exact local-wiring call shape
    (``model_name=`` keyword) without a real faster-whisper model load."""

    def __init__(self, *, model_name: str) -> None:
        self.model_name = model_name
        self.calls: list[Path] = []

    def transcribe(self, path: Path) -> str:
        self.calls.append(path)
        return "local transcript"


class _FakeLocalTTS:
    """Stands in for ``Pyttsx3Adapter`` — proves the exact local-wiring call shape (no args)
    without a real OS TTS engine init."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def speak(self, text: str) -> None:
        self.calls.append(text)


class _RecordingCloudTranscriber:
    """A cloud STT stand-in that succeeds — matches every real cloud STT class's constructor
    shape (``api_key`` positional, ``model`` optional keyword; unused by ``FishAudioTranscriber``,
    which never passes it)."""

    def __init__(self, api_key: str, *, model: str | None = None) -> None:
        self.api_key = api_key
        self.model = model

    def transcribe(self, path: Path) -> str:
        return "cloud transcript"


class _RaisingCloudTranscriber:
    """A cloud STT stand-in that constructs fine but raises on every ``transcribe`` call —
    exercises the mid-call primary-failure fallback path (AC2)."""

    def __init__(self, api_key: str, *, model: str | None = None) -> None:
        self.api_key = api_key
        self.model = model

    def transcribe(self, path: Path) -> str:
        raise RuntimeError("cloud STT exploded mid-call")


class _RecordingCloudTTS:
    """A cloud TTS stand-in that succeeds — matches every real cloud TTS class's constructor
    shape (``api_key`` positional, ``voice_id``/``model`` optional keyword; TK-326 widens the
    real ``FishAudioTTSAdapter`` to a REQUIRED ``model``, but this generic fake keeps it optional
    so it still stands in for deepgram/elevenlabs, whose ctors never take one). ``writer_factory``
    (TK-332) is accepted-and-ignored, so this fake stands in for ``FishAudioTTSAdapter`` correctly
    whether ``stream_playback.streaming_available()`` resolves True or False on the machine
    running this suite."""

    def __init__(
        self,
        api_key: str,
        *,
        voice_id: str | None = None,
        model: str | None = None,
        writer_factory: object = None,
    ) -> None:
        self.api_key = api_key
        self.voice_id = voice_id
        self.model = model
        self.writer_factory = writer_factory

    def speak(self, text: str) -> None:
        pass


class _RaisingCloudTTS:
    """A cloud TTS stand-in that constructs fine but raises on every ``speak`` call — exercises
    the mid-call primary-failure fallback path (AC2). ``writer_factory`` (TK-332) is
    accepted-and-ignored, mirroring ``_RecordingCloudTTS`` above."""

    def __init__(
        self,
        api_key: str,
        *,
        voice_id: str | None = None,
        model: str | None = None,
        writer_factory: object = None,
    ) -> None:
        self.api_key = api_key
        self.voice_id = voice_id
        self.model = model
        self.writer_factory = writer_factory

    def speak(self, text: str) -> None:
        raise RuntimeError("cloud TTS exploded mid-call")


class _RaisingLocalTranscriber:
    """Stands in for ``FasterWhisperTranscriber`` failing to construct (TK-217) — raises the same
    ``ImportError`` the real class raises when the ``voice`` extra is absent."""

    def __init__(self, *, model_name: str) -> None:
        raise ImportError("faster-whisper not installed (simulated, TK-217)")


class _RaisingLocalTranscriberModelLoadFailure:
    """Stands in for ``FasterWhisperTranscriber`` failing to construct with a non-``ImportError``
    (CRF-6) — models are loaded AT CONSTRUCTION, so an uncached model + offline, a bad
    ``WOMBAT_ASR_MODEL``, or a corrupted HF cache each surface as a broad ``Exception`` (here
    ``RuntimeError``), never ``ImportError``."""

    def __init__(self, *, model_name: str) -> None:
        raise RuntimeError(f"simulated whisper model load failure for {model_name!r} (CRF-6)")


class _RaisingLocalTTS:
    """Stands in for ``Pyttsx3Adapter`` failing to construct (TK-217) — an ``ImportError`` is one
    of the ANY-exception cases ``_build_local_tts`` catches."""

    def __init__(self) -> None:
        raise ImportError("pyttsx3 not installed (simulated, TK-217)")


_CLOUD_STT_CLASS_NAMES = (
    "DeepgramTranscriber",
    "ElevenLabsScribeTranscriber",
    "FishAudioTranscriber",
)
_CLOUD_TTS_CLASS_NAMES = ("DeepgramAuraTTSAdapter", "ElevenLabsTTSAdapter", "FishAudioTTSAdapter")


def _block_all_clouds(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (*_CLOUD_STT_CLASS_NAMES, *_CLOUD_TTS_CLASS_NAMES):
        monkeypatch.setattr(select_module, name, _RaisingIfConstructed)


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
    """Simulate ``module_name`` being genuinely not installed (TK-202/Q-103) — evict any cached
    import AND install a meta-path finder ahead of the real one so any subsequent import raises
    ``ModuleNotFoundError``, robust to the module actually being present on this machine."""
    monkeypatch.delitem(sys.modules, module_name, raising=False)
    monkeypatch.setattr(sys, "meta_path", [_BlockedFinder(module_name), *sys.meta_path])


# --------------------------------------------------------------------------------------------AC1


def test_local_stt_provider_builds_faster_whisper_no_cloud_no_key_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(select_module, "FasterWhisperTranscriber", _FakeLocalTranscriber)
    _block_all_clouds(monkeypatch)
    config = _config(wombat_asr_model="tiny")

    transcriber = build_transcriber(config, key_store=_UnreadableVoiceKeyStore())

    assert isinstance(transcriber, _FakeLocalTranscriber)
    assert transcriber.model_name == "tiny"


def test_local_tts_provider_builds_pyttsx3_no_cloud_no_key_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(select_module, "Pyttsx3Adapter", _FakeLocalTTS)
    _block_all_clouds(monkeypatch)
    config = _config()

    adapter = build_tts_adapter(config, key_store=_UnreadableVoiceKeyStore())

    assert isinstance(adapter, _FakeLocalTTS)


# --------------------------------------------------------------------------------------------AC2


def test_cloud_stt_wraps_primary_and_degrades_to_local_exactly_once_on_failure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    monkeypatch.setattr(select_module, "DeepgramTranscriber", _RaisingCloudTranscriber)
    monkeypatch.setattr(select_module, "FasterWhisperTranscriber", _FakeLocalTranscriber)
    store = _FakeVoiceKeyStore(initial={"deepgram": "cloud-key"})
    config = _config(wombat_stt_provider="deepgram")

    transcriber = build_transcriber(config, key_store=store)

    assert isinstance(transcriber, FallbackTranscriber)
    audio = tmp_path / "clip.wav"
    with caplog.at_level(logging.WARNING):
        result = transcriber.transcribe(audio)

    assert result == "local transcript"
    fallback = transcriber._fallback
    assert isinstance(fallback, _FakeLocalTranscriber)
    assert fallback.calls == [audio]  # invoked exactly once
    assert any(record.levelno == logging.WARNING for record in caplog.records)


def test_cloud_tts_wraps_primary_and_degrades_to_local_exactly_once_on_failure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(select_module, "FishAudioTTSAdapter", _RaisingCloudTTS)
    monkeypatch.setattr(select_module, "Pyttsx3Adapter", _FakeLocalTTS)
    store = _FakeVoiceKeyStore(initial={"fish": "cloud-key"})
    config = _config(wombat_tts_provider="fish", wombat_tts_voice_id="voice-123")

    adapter = build_tts_adapter(config, key_store=store)

    assert isinstance(adapter, FallbackTTSAdapter)
    with caplog.at_level(logging.WARNING):
        adapter.speak("hello wombat")

    fallback = adapter._fallback
    assert isinstance(fallback, _FakeLocalTTS)
    assert fallback.calls == ["hello wombat"]  # invoked exactly once
    assert any(record.levelno == logging.WARNING for record in caplog.records)


def test_local_provider_returns_bare_local_instance_never_wrapped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Structural proof of the cloud->local-only direction: the local path NEVER returns a
    Fallback* wrapper — only a bare local instance (or None)."""
    monkeypatch.setattr(select_module, "FasterWhisperTranscriber", _FakeLocalTranscriber)
    monkeypatch.setattr(select_module, "Pyttsx3Adapter", _FakeLocalTTS)
    config = _config()

    transcriber = build_transcriber(config, key_store=_UnreadableVoiceKeyStore())
    adapter = build_tts_adapter(config, key_store=_UnreadableVoiceKeyStore())

    assert isinstance(transcriber, _FakeLocalTranscriber)
    assert not isinstance(transcriber, FallbackTranscriber)
    assert isinstance(adapter, _FakeLocalTTS)
    assert not isinstance(adapter, FallbackTTSAdapter)


@pytest.mark.parametrize("provider", ["deepgram", "elevenlabs", "fish"])
def test_stt_fallback_slot_is_always_local_type_or_none(
    monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    monkeypatch.setattr(select_module, "FasterWhisperTranscriber", _FakeLocalTranscriber)
    for name in _CLOUD_STT_CLASS_NAMES:
        monkeypatch.setattr(select_module, name, _RecordingCloudTranscriber)
    store = _FakeVoiceKeyStore(initial={provider: "cloud-key"})
    config = _config(wombat_stt_provider=provider)

    transcriber = build_transcriber(config, key_store=store)

    assert isinstance(transcriber, FallbackTranscriber)
    assert transcriber._fallback is None or isinstance(transcriber._fallback, _FakeLocalTranscriber)
    assert not isinstance(transcriber._primary, _FakeLocalTranscriber)  # never local-in-cloud-slot


@pytest.mark.parametrize("provider", ["deepgram", "elevenlabs", "fish"])
def test_tts_fallback_slot_is_always_local_type_or_none(
    monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    monkeypatch.setattr(select_module, "Pyttsx3Adapter", _FakeLocalTTS)
    for name in _CLOUD_TTS_CLASS_NAMES:
        monkeypatch.setattr(select_module, name, _RecordingCloudTTS)
    store = _FakeVoiceKeyStore(initial={provider: "cloud-key"})
    config = _config(wombat_tts_provider=provider, wombat_tts_voice_id="voice-123")

    adapter = build_tts_adapter(config, key_store=store)

    assert isinstance(adapter, FallbackTTSAdapter)
    assert adapter._fallback is None or isinstance(adapter._fallback, _FakeLocalTTS)
    assert not isinstance(adapter._primary, _FakeLocalTTS)  # never local-in-cloud-slot


# --------------------------------------------------------------------------------------------AC3


def test_cloud_stt_unresolvable_key_falls_back_to_local_with_loud_log(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(select_module, "FasterWhisperTranscriber", _FakeLocalTranscriber)
    _block_all_clouds(monkeypatch)
    store = _FakeVoiceKeyStore()  # empty — no key stored
    config = _config(wombat_stt_provider="deepgram")

    with caplog.at_level(logging.WARNING):
        transcriber = build_transcriber(config, key_store=store)

    assert isinstance(transcriber, _FakeLocalTranscriber)
    assert "WOMBAT_DEEPGRAM_API_KEY" in caplog.text
    assert "deepgram" in caplog.text.lower()


def test_cloud_tts_unresolvable_key_falls_back_to_local_with_loud_log(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(select_module, "Pyttsx3Adapter", _FakeLocalTTS)
    _block_all_clouds(monkeypatch)
    store = _FakeVoiceKeyStore()  # empty — no key stored
    config = _config(wombat_tts_provider="fish", wombat_tts_voice_id="voice-123")

    with caplog.at_level(logging.WARNING):
        adapter = build_tts_adapter(config, key_store=store)

    assert isinstance(adapter, _FakeLocalTTS)
    assert "WOMBAT_FISH_API_KEY" in caplog.text
    assert "fish" in caplog.text.lower()


def test_cloud_tts_missing_required_voice_id_falls_back_to_local_with_loud_log(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(select_module, "Pyttsx3Adapter", _FakeLocalTTS)
    _block_all_clouds(monkeypatch)
    store = _FakeVoiceKeyStore(initial={"fish": "cloud-key"})  # key resolves fine
    config = _config(wombat_tts_provider="fish")  # wombat_tts_voice_id left unset

    with caplog.at_level(logging.WARNING):
        adapter = build_tts_adapter(config, key_store=store)

    assert isinstance(adapter, _FakeLocalTTS)
    assert "WOMBAT_TTS_VOICE_ID" in caplog.text


def test_cloud_stt_absent_voice_cloud_extra_falls_back_to_local_with_loud_log(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Real, unmocked ``ImportError`` path (TK-202/Q-103 shim reused): ``httpx`` (the
    ``voice-cloud`` extra) simulated absent so the real ``DeepgramTranscriber``'s default
    ``HttpxVoiceTransport`` construction raises — never mocked."""
    monkeypatch.setattr(select_module, "FasterWhisperTranscriber", _FakeLocalTranscriber)
    _simulate_absent(monkeypatch, "httpx")
    store = _FakeVoiceKeyStore(initial={"deepgram": "cloud-key"})
    config = _config(wombat_stt_provider="deepgram")

    with caplog.at_level(logging.WARNING):
        transcriber = build_transcriber(config, key_store=store)

    assert isinstance(transcriber, _FakeLocalTranscriber)
    assert "voice-cloud" in caplog.text.lower()


def test_cloud_tts_fish_provider_threads_configured_model_into_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TK-326 (DEC-71a/DEC-72a): ``config.wombat_fish_model`` reaches the fish adapter's ``model``
    ctor param; the deepgram/elevenlabs branches never receive it — real assertion in
    ``test_cloud_tts_non_fish_providers_never_receive_a_model_kwarg`` below (TK-328 ISS-38(m3))."""
    monkeypatch.setattr(select_module, "FishAudioTTSAdapter", _RecordingCloudTTS)
    monkeypatch.setattr(select_module, "Pyttsx3Adapter", _FakeLocalTTS)
    store = _FakeVoiceKeyStore(initial={"fish": "cloud-key"})
    config = _config(
        wombat_tts_provider="fish", wombat_tts_voice_id="voice-123", wombat_fish_model="s1"
    )

    adapter = build_tts_adapter(config, key_store=store)

    assert isinstance(adapter, FallbackTTSAdapter)
    primary = adapter._primary
    assert isinstance(primary, _RecordingCloudTTS)
    assert primary.model == "s1"


@pytest.mark.parametrize("provider", ["deepgram", "elevenlabs"])
def test_cloud_tts_non_fish_providers_never_receive_a_model_kwarg(
    monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    """TK-328 ISS-38(m3): the real assertion backing the claim above — ``_construct_cloud_tts``'s
    deepgram/elevenlabs branches never pass ``model``, so the generic ``_RecordingCloudTTS``
    fake's ``model`` stays at its ``None`` default (previously only implied, never checked)."""
    monkeypatch.setattr(select_module, "Pyttsx3Adapter", _FakeLocalTTS)
    for name in _CLOUD_TTS_CLASS_NAMES:
        monkeypatch.setattr(select_module, name, _RecordingCloudTTS)
    store = _FakeVoiceKeyStore(initial={provider: "cloud-key"})
    config = _config(wombat_tts_provider=provider, wombat_tts_voice_id="voice-123")

    adapter = build_tts_adapter(config, key_store=store)

    assert isinstance(adapter, FallbackTTSAdapter)
    assert isinstance(adapter._primary, _RecordingCloudTTS)
    assert adapter._primary.model is None


class _FakeEnqueuer:
    def enqueue(self, item: QueueItem) -> EnqueueResult:
        return EnqueueResult.QUEUED


def test_boot_never_fails_for_a_cloud_voice_misconfiguration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AC3: a cloud STT/TTS misconfiguration (no resolvable key) never raises through the real
    boot seams — ``build_source_registry``/``build_speak_sink`` both complete, degraded local.

    Neither boot seam has a ``key_store`` injection point (they call ``build_transcriber(config)``/
    ``build_tts_adapter(config)`` with no override), so ``KeyringVoiceKeyStore`` itself is
    monkeypatched to the in-memory fake — this test must NEVER touch the real OS keyring
    (Q-57(a)), even on a machine that has a real wombat voice key stored."""
    monkeypatch.setattr(select_module, "FasterWhisperTranscriber", _FakeLocalTranscriber)
    monkeypatch.setattr(select_module, "Pyttsx3Adapter", _FakeLocalTTS)
    monkeypatch.setattr(select_module, "KeyringVoiceKeyStore", _FakeVoiceKeyStore)
    _block_all_clouds(monkeypatch)
    config = _config(
        wombat_stt_provider="deepgram",
        wombat_tts_provider="fish",
        wombat_voice_enabled=True,
        wombat_asr_drop_dir=str(tmp_path),
    )

    registry = build_source_registry(config, _FakeEnqueuer(), tz=ZoneInfo("UTC"))
    stage = build_speak_sink(config)

    assert isinstance(registry, SourceRegistry)
    assert stage._voice_enabled is True
    assert isinstance(stage._adapter, _FakeLocalTTS)


# ----------------------------------------------------------------------------------------- TK-217


def test_tk217_cloud_tts_healthy_primary_contextualizes_local_fallback_failure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """CR4-1: a healthy cloud TTS primary (voice-cloud extra only, no 'voice' extra installed)
    whose local fallback fails to construct must NOT claim voice output is disabled — the cloud
    primary is live and still speaks."""
    monkeypatch.setattr(select_module, "DeepgramAuraTTSAdapter", _RecordingCloudTTS)
    monkeypatch.setattr(select_module, "Pyttsx3Adapter", _RaisingLocalTTS)
    store = _FakeVoiceKeyStore(initial={"deepgram": "cloud-key"})
    config = _config(wombat_tts_provider="deepgram")

    with caplog.at_level(logging.WARNING):
        adapter = build_tts_adapter(config, key_store=store)

    assert isinstance(adapter, FallbackTTSAdapter)
    assert isinstance(adapter._primary, _RecordingCloudTTS)  # cloud primary is live
    assert adapter._fallback is None
    assert "remains active" in caplog.text
    assert "voice output disabled" not in caplog.text


def test_tk217_local_tts_primary_failure_preserves_voice_output_disabled_message(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The local-primary path's exact historical message is byte-preserved (CR4-1 must not touch
    role='primary' logging)."""
    monkeypatch.setattr(select_module, "Pyttsx3Adapter", _RaisingLocalTTS)
    _block_all_clouds(monkeypatch)
    config = _config(wombat_tts_provider="local")

    with caplog.at_level(logging.WARNING):
        adapter = build_tts_adapter(config, key_store=_UnreadableVoiceKeyStore())

    assert adapter is None
    assert "voice output disabled for this boot" in caplog.text


def test_tk217_cloud_stt_healthy_primary_contextualizes_local_fallback_failure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """CR4-1's STT axis: a healthy cloud STT primary whose local ASR fallback fails to construct
    logs a fallback-unavailable-cloud-active message, and the live cloud primary is returned."""
    monkeypatch.setattr(select_module, "DeepgramTranscriber", _RecordingCloudTranscriber)
    monkeypatch.setattr(select_module, "FasterWhisperTranscriber", _RaisingLocalTranscriber)
    store = _FakeVoiceKeyStore(initial={"deepgram": "cloud-key"})
    config = _config(wombat_stt_provider="deepgram")

    with caplog.at_level(logging.WARNING):
        transcriber = build_transcriber(config, key_store=store)

    assert isinstance(transcriber, FallbackTranscriber)
    assert isinstance(transcriber._primary, _RecordingCloudTranscriber)  # cloud primary is live
    assert transcriber._fallback is None
    assert "remains active" in caplog.text


# ------------------------------------------------------------------------------------------ CRF-6


def test_crf6_local_stt_primary_non_import_error_degrades_to_none_with_loud_log(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """AC1: a whisper model load failure (any non-``ImportError`` exception) at the local STT
    primary path degrades to ``None`` with the byte-preserved primary-role message — it must
    never propagate and crash boot (CON-3)."""
    monkeypatch.setattr(
        select_module, "FasterWhisperTranscriber", _RaisingLocalTranscriberModelLoadFailure
    )
    _block_all_clouds(monkeypatch)
    config = _config(wombat_stt_provider="local")

    with caplog.at_level(logging.WARNING):
        transcriber = build_transcriber(config, key_store=_UnreadableVoiceKeyStore())

    assert transcriber is None
    assert "local STT (faster-whisper) is not installed" in caplog.text


def test_crf6_boot_survives_local_stt_non_import_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AC1: ``_maybe_register_asr``/``build_source_registry`` complete without raising when the
    local ASR constructor raises a non-``ImportError`` (a real whisper load failure), proving boot
    survives (CON-3, CRF-6)."""
    monkeypatch.setattr(
        select_module, "FasterWhisperTranscriber", _RaisingLocalTranscriberModelLoadFailure
    )
    monkeypatch.setattr(select_module, "KeyringVoiceKeyStore", _FakeVoiceKeyStore)
    _block_all_clouds(monkeypatch)
    config = _config(
        wombat_stt_provider="local",
        wombat_voice_enabled=True,
        wombat_asr_drop_dir=str(tmp_path),
    )

    registry = build_source_registry(config, _FakeEnqueuer(), tz=ZoneInfo("UTC"))

    assert isinstance(registry, SourceRegistry)


def test_crf6_cloud_stt_healthy_primary_local_fallback_non_import_error_falls_back_to_none(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """AC2: a healthy cloud STT primary whose local fallback-slot constructor raises a
    non-``ImportError`` (CRF-6) still returns the ``FallbackTranscriber`` wrapping the live cloud
    primary with ``fallback=None`` and the TK-217 fallback-role message logged."""
    monkeypatch.setattr(select_module, "DeepgramTranscriber", _RecordingCloudTranscriber)
    monkeypatch.setattr(
        select_module, "FasterWhisperTranscriber", _RaisingLocalTranscriberModelLoadFailure
    )
    store = _FakeVoiceKeyStore(initial={"deepgram": "cloud-key"})
    config = _config(wombat_stt_provider="deepgram")

    with caplog.at_level(logging.WARNING):
        transcriber = build_transcriber(config, key_store=store)

    assert isinstance(transcriber, FallbackTranscriber)
    assert isinstance(transcriber._primary, _RecordingCloudTranscriber)  # cloud primary is live
    assert transcriber._fallback is None
    assert "remains active" in caplog.text


# ----------------------------------------------------------------------------------------- TK-328


def test_build_tts_adapter_with_info_fish_primary_true_when_actually_constructed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ruling v2.187 r1: a genuinely constructed Fish primary yields
    ``TTSBuildInfo(fish_primary=True, fish_model=<configured>)``."""
    monkeypatch.setattr(select_module, "FishAudioTTSAdapter", _RecordingCloudTTS)
    monkeypatch.setattr(select_module, "Pyttsx3Adapter", _FakeLocalTTS)
    store = _FakeVoiceKeyStore(initial={"fish": "cloud-key"})
    config = _config(
        wombat_tts_provider="fish", wombat_tts_voice_id="voice-123", wombat_fish_model="s2.1-pro"
    )

    adapter, info = select_module.build_tts_adapter_with_info(config, key_store=store)

    assert isinstance(adapter, FallbackTTSAdapter)
    assert info == select_module.TTSBuildInfo(fish_primary=True, fish_model="s2.1-pro")


@pytest.mark.parametrize(
    ("overrides", "store_initial"),
    [
        pytest.param(
            {"wombat_tts_provider": "fish", "wombat_tts_voice_id": "voice-123"},
            {},
            id="fish-no-key",
        ),
        pytest.param(
            {"wombat_tts_provider": "fish"}, {"fish": "cloud-key"}, id="fish-blank-voice-id"
        ),
        pytest.param({"wombat_tts_provider": "local"}, {}, id="local"),
        pytest.param(
            {"wombat_tts_provider": "deepgram"}, {"deepgram": "cloud-key"}, id="non-fish-cloud"
        ),
    ],
)
def test_build_tts_adapter_with_info_degrade_paths_yield_false_none(
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, object],
    store_initial: dict[str, str],
) -> None:
    """Every degrade path (no resolved key, blank required voice_id, local, a non-fish cloud
    provider) yields ``TTSBuildInfo(fish_primary=False, fish_model=None)`` -- never a placebo."""
    monkeypatch.setattr(select_module, "Pyttsx3Adapter", _FakeLocalTTS)
    monkeypatch.setattr(select_module, "DeepgramAuraTTSAdapter", _RecordingCloudTTS)
    store = _FakeVoiceKeyStore(initial=store_initial)
    config = _config(**overrides)

    _adapter, info = select_module.build_tts_adapter_with_info(config, key_store=store)

    assert info == select_module.TTSBuildInfo(fish_primary=False, fish_model=None)


def test_build_tts_adapter_with_info_fish_import_error_yields_false_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live, unmocked ``ImportError`` on the fish construction path (voice-cloud extra absent)
    degrades to local -- still ``fish_primary=False, fish_model=None``."""
    monkeypatch.setattr(select_module, "Pyttsx3Adapter", _FakeLocalTTS)
    _simulate_absent(monkeypatch, "httpx")
    store = _FakeVoiceKeyStore(initial={"fish": "cloud-key"})
    config = _config(wombat_tts_provider="fish", wombat_tts_voice_id="voice-123")

    adapter, info = select_module.build_tts_adapter_with_info(config, key_store=store)

    assert isinstance(adapter, _FakeLocalTTS)
    assert info == select_module.TTSBuildInfo(fish_primary=False, fish_model=None)


def test_build_tts_adapter_is_thin_wrapper_over_with_info(monkeypatch: pytest.MonkeyPatch) -> None:
    """``build_tts_adapter``'s two other call sites (``bootstrap.build_speak_sink``/
    ``bootstrap.make_speak_callable``) must stay byte-identical -- proven structurally: it returns
    exactly the SAME adapter shape ``build_tts_adapter_with_info`` builds."""
    monkeypatch.setattr(select_module, "FishAudioTTSAdapter", _RecordingCloudTTS)
    monkeypatch.setattr(select_module, "Pyttsx3Adapter", _FakeLocalTTS)
    store = _FakeVoiceKeyStore(initial={"fish": "cloud-key"})
    config = _config(wombat_tts_provider="fish", wombat_tts_voice_id="voice-123")

    thin = select_module.build_tts_adapter(config, key_store=store)
    with_info_adapter, _info = select_module.build_tts_adapter_with_info(config, key_store=store)

    assert isinstance(thin, FallbackTTSAdapter)
    assert isinstance(with_info_adapter, FallbackTTSAdapter)
    assert isinstance(thin._primary, _RecordingCloudTTS)
    assert isinstance(with_info_adapter._primary, _RecordingCloudTTS)


# --------------------------------------------------------------------- TK-328 fallback-tag hygiene


class _RecordingTTS:
    """Minimal ``TTSAdapter`` stand-in for direct ``FallbackTTSAdapter`` unit tests (AC2) --
    records every ``speak`` call, never raises."""

    def __init__(self) -> None:
        self.spoken: list[str] = []

    def speak(self, text: str) -> None:
        self.spoken.append(text)


class _RaisingTTS:
    """Minimal ``TTSAdapter`` stand-in whose ``speak`` always raises -- degrades to the fallback
    slot (AC2)."""

    def speak(self, text: str) -> None:
        raise RuntimeError("primary exploded (simulated, TK-328)")


class _SucceedsThenRaisesTTS:
    """Minimal ``TTSAdapter`` stand-in whose FIRST ``speak`` call succeeds and every call after
    raises -- reproduces a successful remote-routed reply followed by a failing local one on the
    SAME adapter instance (TK-343 repair regression coverage)."""

    def __init__(self) -> None:
        self.spoken: list[str] = []
        self._calls = 0

    def speak(self, text: str) -> None:
        self._calls += 1
        if self._calls == 1:
            self.spoken.append(text)
            return
        raise RuntimeError("primary exploded on a later call (simulated)")


def test_fallback_tts_adapter_strips_tags_only_on_the_fallback_branch() -> None:
    """AC2: a raising primary degrades to the fallback with every ALLOWED_TAGS marker stripped
    (whitespace collapsed) -- the local engine never speaks Fish's bracket markers aloud."""
    spy = _RecordingTTS()
    adapter = FallbackTTSAdapter(_RaisingTTS(), fallback=spy)

    adapter.speak("[calm] Lunch moved. [break] Nothing else.")

    assert spy.spoken == ["Lunch moved. Nothing else."]


def test_fallback_tts_adapter_healthy_primary_receives_tagged_text_verbatim_no_fallback_call() -> (
    None
):
    """AC2: a healthy primary receives the tagged text VERBATIM (byte-identical) and the fallback
    is never called."""
    spy = _RecordingTTS()
    primary = _RecordingTTS()
    adapter = FallbackTTSAdapter(primary, fallback=spy)

    adapter.speak("[calm] Lunch moved. [break] Nothing else.")

    assert primary.spoken == ["[calm] Lunch moved. [break] Nothing else."]
    assert spy.spoken == []


# ----------------------------------------------------------------------------------------- TK-332


def test_cloud_tts_fish_streaming_available_wires_streaming_audio_writer_factory(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """TK-332 (DEC-73a/d/f): ``stream_playback.streaming_available()`` returning True wires
    ``StreamingAudioWriter`` itself (a zero-arg callable) as the REAL ``FishAudioTTSAdapter``'s
    ``writer_factory`` -- proven without ever touching real audio hardware (the writer's own
    ``sounddevice`` import stays lazy until the factory is actually invoked). No warning is
    logged on this path."""
    monkeypatch.setattr(select_module, "Pyttsx3Adapter", _FakeLocalTTS)
    monkeypatch.setattr(select_module, "streaming_available", lambda: True)
    store = _FakeVoiceKeyStore(initial={"fish": "cloud-key"})
    config = _config(
        wombat_tts_provider="fish", wombat_tts_voice_id="voice-123", wombat_fish_model="s2.1-pro"
    )

    with caplog.at_level(logging.WARNING):
        adapter = build_tts_adapter(config, key_store=store)

    assert isinstance(adapter, FallbackTTSAdapter)
    primary = adapter._primary
    assert isinstance(primary, FishAudioTTSAdapter)
    assert primary._writer_factory is StreamingAudioWriter
    assert "streaming" not in caplog.text.lower()


def test_cloud_tts_fish_streaming_dep_absent_builds_buffered_adapter_with_loud_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """TK-332 AC4: a genuinely simulated-absent ``sounddevice`` (``stream_playback.streaming_
    available()``'s own lazy-import probe, TK-202/Q-103 shim reused) means the REAL
    ``FishAudioTTSAdapter`` is built WITHOUT a ``writer_factory`` -- the buffered wav+winsound
    path stays byte-identical to today -- alongside ONE loud WARNING naming the missing extra."""
    monkeypatch.setattr(select_module, "Pyttsx3Adapter", _FakeLocalTTS)
    _simulate_absent(monkeypatch, "sounddevice")
    store = _FakeVoiceKeyStore(initial={"fish": "cloud-key"})
    config = _config(
        wombat_tts_provider="fish", wombat_tts_voice_id="voice-123", wombat_fish_model="s2.1-pro"
    )

    with caplog.at_level(logging.WARNING):
        adapter = build_tts_adapter(config, key_store=store)

    assert isinstance(adapter, FallbackTTSAdapter)
    primary = adapter._primary
    assert isinstance(primary, FishAudioTTSAdapter)
    assert primary._writer_factory is None
    assert "voice-cloud" in caplog.text.lower()


# --------------------------------------------------- TK-332 AC5 (ISS-39 f1 ruling, v2.195)


class _PartialSpeechRaisingTTS:
    """Minimal ``TTSAdapter`` stand-in whose ``speak`` always raises a caller-chosen
    ``PartialSpeechError`` -- drives ``FallbackTTSAdapter``'s dedicated except arm directly (AC5),
    the reachability lesson: an unwrapped-adapter test is exactly how this bug survived review."""

    def __init__(self, *, played_any: bool) -> None:
        self._played_any = played_any

    def speak(self, text: str) -> None:
        raise voice_tts.PartialSpeechError(played_any=self._played_any)


def test_fallback_tts_adapter_partial_speech_played_any_true_reraises_no_fallback_call() -> None:
    """AC5: ``played_any=True`` re-raises ``PartialSpeechError`` UNCHANGED through the wrapper,
    with the constructed fallback NEVER called."""
    spy = _RecordingTTS()
    adapter = FallbackTTSAdapter(_PartialSpeechRaisingTTS(played_any=True), fallback=spy)

    with pytest.raises(voice_tts.PartialSpeechError) as excinfo:
        adapter.speak("hello")

    assert excinfo.value.played_any is True
    assert spy.spoken == []


def test_fallback_tts_adapter_partial_speech_played_any_false_falls_back_with_loud_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC5: ``played_any=False`` keeps today's exact posture through the wrapper -- ONE loud
    warning, then the constructed fallback speaks the stripped-tag text (no exception escapes)."""
    spy = _RecordingTTS()
    adapter = FallbackTTSAdapter(_PartialSpeechRaisingTTS(played_any=False), fallback=spy)

    with caplog.at_level(logging.WARNING):
        adapter.speak("[calm] Lunch moved. [break] Nothing else.")

    assert spy.spoken == ["Lunch moved. Nothing else."]
    assert "cloud tts provider failed" in caplog.text.lower()


def test_fallback_tts_adapter_partial_speech_played_any_false_no_fallback_reraises() -> None:
    """AC5: ``played_any=False`` with NO fallback constructed re-raises the ``PartialSpeechError``
    -- today's exact no-fallback posture, mirroring the bare ``Exception`` arm."""
    adapter = FallbackTTSAdapter(_PartialSpeechRaisingTTS(played_any=False), fallback=None)

    with pytest.raises(voice_tts.PartialSpeechError) as excinfo:
        adapter.speak("hello")

    assert excinfo.value.played_any is False


# ----------------------------------------------------------------------------------------- TK-343


class _FakeClock:
    def __init__(self, now: float = 0.0) -> None:
        self._now = now

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


class _FakeStreamingAudioWriter:
    """Stands in for the real ``StreamingAudioWriter`` -- never touches ``sounddevice``, so the
    factory's LOCAL branch can be exercised without any real (or even simulated-absent) audio
    hardware dependency. Typed loosely (``Any``) on purpose: this class is swapped in for
    ``select_module.StreamingAudioWriter`` at RUNTIME only (``monkeypatch``), so the functions
    under test keep their real, precise static return type (``StreamingAudioWriter``) -- callers
    below capture the result as ``Any`` to inspect the fake's own attributes instead of fighting
    the (correct) static type."""

    def __init__(self, *, stream_factory: Callable[[], object] | None = None) -> None:
        self.stream_factory = stream_factory


# ----------------------------------------------------------- AC2: the closure, in isolation


def test_remote_writer_factory_routes_to_buffered_sink_when_origin_fresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(select_module, "StreamingAudioWriter", _FakeStreamingAudioWriter)
    register = LastTurnOriginRegister(clock=_FakeClock())
    register.note_origin("watch-1", "utt-1")
    store = SealedUtteranceStore(clock=_FakeClock())
    tracker = select_module._RemoteRouteAttempted()

    factory = select_module._build_remote_aware_writer_factory(register, store, tracker)
    writer: Any = factory()

    assert isinstance(writer, _FakeStreamingAudioWriter)
    assert writer.stream_factory is not None
    sink = writer.stream_factory()
    assert isinstance(sink, BufferedUtteranceSink)
    assert sink._origin_device_id == "watch-1"
    assert sink._utterance_id == "utt-1"
    assert sink._store is store


def test_remote_writer_factory_routes_local_when_origin_empty_or_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(select_module, "StreamingAudioWriter", _FakeStreamingAudioWriter)
    register = LastTurnOriginRegister(clock=_FakeClock())  # nothing ever noted
    store = SealedUtteranceStore(clock=_FakeClock())
    tracker = select_module._RemoteRouteAttempted()

    factory = select_module._build_remote_aware_writer_factory(register, store, tracker)
    writer: Any = factory()

    assert isinstance(writer, _FakeStreamingAudioWriter)
    assert writer.stream_factory is None
    assert tracker.take() is False  # never marked -- this call was never remote


def test_remote_writer_factory_reads_register_at_call_time_two_calls_route_differently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC2: 'a remote voice turn accepted at the voice route, then a second turn from the laptop
    ... the factory is invoked once per utterance and the two speaks route DIFFERENTLY, proving
    the closure reads the register at CALL time rather than at construction time' -- the SAME
    closure object, invoked twice, must not bake in its first call's answer."""
    monkeypatch.setattr(select_module, "StreamingAudioWriter", _FakeStreamingAudioWriter)
    register = LastTurnOriginRegister(clock=_FakeClock())
    store = SealedUtteranceStore(clock=_FakeClock())
    tracker = select_module._RemoteRouteAttempted()
    factory = select_module._build_remote_aware_writer_factory(register, store, tracker)

    register.note_origin("watch-1", "utt-1")
    first_writer: Any = factory()  # the remote turn's own reply

    second_writer: Any = factory()  # a laptop turn's reply, right after -- already claimed

    assert first_writer.stream_factory is not None
    assert second_writer.stream_factory is None


# --------------------------------------------------------------- build_tts_adapter_with_info wiring


def test_build_tts_adapter_with_info_wires_remote_closure_when_both_args_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(select_module, "FishAudioTTSAdapter", _RecordingCloudTTS)
    monkeypatch.setattr(select_module, "Pyttsx3Adapter", _FakeLocalTTS)
    monkeypatch.setattr(select_module, "streaming_available", lambda: True)
    monkeypatch.setattr(select_module, "StreamingAudioWriter", _FakeStreamingAudioWriter)
    store_kv = _FakeVoiceKeyStore(initial={"fish": "cloud-key"})
    config = _config(wombat_tts_provider="fish", wombat_tts_voice_id="voice-123")
    register = LastTurnOriginRegister(clock=_FakeClock())
    sealed_store = SealedUtteranceStore(clock=_FakeClock())

    adapter, _info = select_module.build_tts_adapter_with_info(
        config,
        key_store=store_kv,
        turn_origin_register=register,
        sealed_utterance_store=sealed_store,
    )

    assert isinstance(adapter, FallbackTTSAdapter)
    primary = adapter._primary
    assert isinstance(primary, _RecordingCloudTTS)
    writer_factory: Any = primary.writer_factory
    assert writer_factory is not _FakeStreamingAudioWriter
    assert adapter._remote_attempt is not None

    # the wired closure reads the SAME register/store this call passed in.
    register.note_origin("watch-1", "utt-9")
    writer: Any = writer_factory()
    assert writer.stream_factory is not None
    sink = writer.stream_factory()
    assert isinstance(sink, BufferedUtteranceSink)
    assert sink._store is sealed_store


def test_build_tts_adapter_with_info_neither_arg_wires_the_bare_class(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC1: omitting BOTH new kwargs leaves remote routing entirely unwired -- byte-identical to
    the pre-TK-343 ``writer_factory=StreamingAudioWriter`` wiring."""
    monkeypatch.setattr(select_module, "FishAudioTTSAdapter", _RecordingCloudTTS)
    monkeypatch.setattr(select_module, "Pyttsx3Adapter", _FakeLocalTTS)
    monkeypatch.setattr(select_module, "streaming_available", lambda: True)
    store_kv = _FakeVoiceKeyStore(initial={"fish": "cloud-key"})
    config = _config(wombat_tts_provider="fish", wombat_tts_voice_id="voice-123")

    adapter, _info = select_module.build_tts_adapter_with_info(config, key_store=store_kv)

    assert isinstance(adapter, FallbackTTSAdapter)
    primary = adapter._primary
    assert isinstance(primary, _RecordingCloudTTS)
    assert primary.writer_factory is StreamingAudioWriter
    assert adapter._remote_attempt is None


def test_build_tts_adapter_with_info_register_only_wires_the_bare_class(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC1's other half: a ``turn_origin_register`` with NO ``sealed_utterance_store`` is
    ALSO not enough -- both are required together, so remote routing stays unwired."""
    monkeypatch.setattr(select_module, "FishAudioTTSAdapter", _RecordingCloudTTS)
    monkeypatch.setattr(select_module, "Pyttsx3Adapter", _FakeLocalTTS)
    monkeypatch.setattr(select_module, "streaming_available", lambda: True)
    store_kv = _FakeVoiceKeyStore(initial={"fish": "cloud-key"})
    config = _config(wombat_tts_provider="fish", wombat_tts_voice_id="voice-123")
    register = LastTurnOriginRegister(clock=_FakeClock())

    adapter, _info = select_module.build_tts_adapter_with_info(
        config, key_store=store_kv, turn_origin_register=register
    )

    assert isinstance(adapter, FallbackTTSAdapter)
    primary = adapter._primary
    assert isinstance(primary, _RecordingCloudTTS)
    assert primary.writer_factory is StreamingAudioWriter
    assert adapter._remote_attempt is None


# ----------------------------------------------------------- FallbackTTSAdapter remote_attempt


def test_fallback_tts_adapter_remote_attempt_marked_reraises_on_played_any_false() -> None:
    """AC7: a remote-origin turn's total Fish failure (played_any=False) must NEVER rescue-speak
    on the laptop -- the marked flag suppresses the fallback attempt entirely."""
    spy = _RecordingTTS()
    tracker = select_module._RemoteRouteAttempted()
    tracker.mark()
    adapter = FallbackTTSAdapter(
        _PartialSpeechRaisingTTS(played_any=False), fallback=spy, remote_attempt=tracker
    )

    with pytest.raises(voice_tts.PartialSpeechError) as excinfo:
        adapter.speak("hello")

    assert excinfo.value.played_any is False
    assert spy.spoken == []


def test_fallback_tts_adapter_remote_attempt_marked_reraises_on_bare_exception() -> None:
    """AC7's other failure shape: a non-2xx/transport error BEFORE any chunk raises a bare
    Exception (not PartialSpeechError) -- the SAME no-rescue suppression must apply."""
    spy = _RecordingTTS()
    tracker = select_module._RemoteRouteAttempted()
    tracker.mark()
    adapter = FallbackTTSAdapter(_RaisingTTS(), fallback=spy, remote_attempt=tracker)

    with pytest.raises(RuntimeError):
        adapter.speak("hello")

    assert spy.spoken == []


def test_fallback_tts_adapter_remote_attempt_unmarked_falls_back_normally() -> None:
    """A remote_attempt object is wired (streaming+fish is active) but was never marked -- this
    speak's own writer_factory call found the register empty (an ordinary local turn) -- so an
    unrelated failure still degrades to the local fallback exactly as before TK-343."""
    spy = _RecordingTTS()
    tracker = select_module._RemoteRouteAttempted()  # never marked
    adapter = FallbackTTSAdapter(_RaisingTTS(), fallback=spy, remote_attempt=tracker)

    adapter.speak("hello")

    assert spy.spoken == ["hello"]


def test_fallback_tts_adapter_remote_attempt_is_consumed_only_once() -> None:
    """The flag is one-shot per closure invocation: a SECOND speak() through the SAME adapter,
    after the first consumed the mark, falls back normally (mirrors AC2's register consumption)."""
    tracker = select_module._RemoteRouteAttempted()
    tracker.mark()
    spy = _RecordingTTS()
    adapter = FallbackTTSAdapter(_RaisingTTS(), fallback=spy, remote_attempt=tracker)

    with pytest.raises(RuntimeError):
        adapter.speak("first (remote, suppressed)")
    adapter.speak("second (local, falls back)")

    assert spy.spoken == ["second (local, falls back)"]


def test_fallback_tts_adapter_remote_attempt_success_then_local_failure_still_falls_back() -> (
    None
):
    """TK-343 repair: a SUCCESSFUL remote-routed reply must consume the one-shot mark exactly
    like a failure does -- otherwise the mark leaks into the NEXT (local, unmarked-by-this-call)
    turn and its primary failure wrongly re-raises instead of degrading to local TTS."""
    tracker = select_module._RemoteRouteAttempted()
    tracker.mark()
    spy = _RecordingTTS()
    primary = _SucceedsThenRaisesTTS()
    adapter = FallbackTTSAdapter(primary, fallback=spy, remote_attempt=tracker)

    adapter.speak("first (remote, succeeds)")
    adapter.speak("second (local, primary fails)")

    assert spy.spoken == ["second (local, primary fails)"]


# --------------------------------------------------------------------------------------------- AC8


def test_queue_item_carries_no_origin_field() -> None:
    """AC8/non-goal: 'no QueueItem schema change, no origin payload key' -- a structural pin over
    the dataclass's own field set, never a guess."""
    import dataclasses

    field_names = {f.name for f in dataclasses.fields(QueueItem)}
    assert field_names == {"idempotency_key", "payload", "item_id"}


def test_format_payload_fields_never_renders_an_origin_key() -> None:
    """AC8: 'format_payload_fields over a remote voice turn's composed payload => no origin key
    appears anywhere in the prompt' -- origin rides the register, never the payload, so a
    representative remote-voice-turn payload renders with no device/utterance identifier."""
    from wombat.compose.templates import format_payload_fields

    payload = {"transcript": "what's on my calendar today", "voice_turn": True}

    rendered = format_payload_fields(payload)

    assert rendered == "transcript: what's on my calendar today; voice_turn: True"
    for forbidden in ("device_id", "origin_device_id", "utterance_id", "origin"):
        assert forbidden not in rendered
