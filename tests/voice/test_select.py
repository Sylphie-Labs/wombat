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

TK-217 (CR4-1) — the failed-local-fallback warning is contextualized when it fills the fallback
slot of an already-healthy cloud primary (never claims voice is disabled when the cloud primary
still works), while the local-primary path's exact historical message is byte-preserved:
    ``test_tk217_cloud_tts_healthy_primary_contextualizes_local_fallback_failure``,
    ``test_tk217_local_tts_primary_failure_preserves_voice_output_disabled_message``,
    ``test_tk217_cloud_stt_healthy_primary_contextualizes_local_fallback_failure``.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Sequence
from importlib.abc import MetaPathFinder
from importlib.machinery import ModuleSpec
from pathlib import Path
from types import ModuleType
from zoneinfo import ZoneInfo

import pytest

import wombat.voice.select as select_module
from wombat.bootstrap import build_speak_sink
from wombat.config import WombatConfig
from wombat.queue import EnqueueResult, QueueItem
from wombat.sources.bootstrap import build_source_registry
from wombat.sources.registry import SourceRegistry
from wombat.voice.select import (
    FallbackTranscriber,
    FallbackTTSAdapter,
    build_transcriber,
    build_tts_adapter,
)


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
    shape (``api_key`` positional, ``voice_id`` optional keyword)."""

    def __init__(self, api_key: str, *, voice_id: str | None = None) -> None:
        self.api_key = api_key
        self.voice_id = voice_id

    def speak(self, text: str) -> None:
        pass


class _RaisingCloudTTS:
    """A cloud TTS stand-in that constructs fine but raises on every ``speak`` call — exercises
    the mid-call primary-failure fallback path (AC2)."""

    def __init__(self, api_key: str, *, voice_id: str | None = None) -> None:
        self.api_key = api_key
        self.voice_id = voice_id

    def speak(self, text: str) -> None:
        raise RuntimeError("cloud TTS exploded mid-call")


class _RaisingLocalTranscriber:
    """Stands in for ``FasterWhisperTranscriber`` failing to construct (TK-217) — raises the same
    ``ImportError`` the real class raises when the ``voice`` extra is absent."""

    def __init__(self, *, model_name: str) -> None:
        raise ImportError("faster-whisper not installed (simulated, TK-217)")


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
