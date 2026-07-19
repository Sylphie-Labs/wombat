"""TK-195 acceptance criteria — voice egress lesion tests (EP-31, the DEC-28 structural proof).

Proves the DEC-28 posture structurally: the default configuration is offline-but-for-DeepSeek
(zero voice-surface cloud egress), and a mid-run cloud outage on either voice axis (STT/TTS)
degrades to local without a crash or a lost item. TESTS ONLY — no ``src`` edits (the TK-163/
TK-165 lesion pattern); this module reuses TK-193's as-built ``voice.select`` seams exactly as
they are.

  AC1 (default zero-egress, Q-105(f) binding proof shape): ``test_ac1_...`` — a FRESH SUBPROCESS
      (``sys.executable -c``, ``cwd`` chdir'd to a tmp dir so the operator's real ``.env`` can
      never leak, the TK-202 hermeticity rule) that, under a default ``WombatConfig`` (every
      voice field defaulted), calls the voice-surface builders (``sources.bootstrap.
      build_source_registry`` over a fake queue + in-memory token stores, ``bootstrap.
      build_speak_sink``, ``bootstrap.make_speak_callable``, ``voice.select.build_transcriber``/
      ``build_tts_adapter``) with every ``wombat.voice`` cloud class substituted for a
      raises-on-``__init__`` sentinel, then asserts NONE of them fired and that ``'httpx'`` never
      entered ``sys.modules``. Q-105(f) scopes this assertion to the VOICE surface (not the whole
      process): the sanctioned DeepSeek mouth's ``openai`` client may itself import ``httpx`` —
      this proof never calls ``build_engine``/``assemble_runtime``, only the five voice-surface
      builders named above, so that egress is out of scope by construction. This is the first
      subprocess-shaped proof in this test suite — a fresh interpreter is required because, in a
      shared pytest process, an EARLIER test module may have already imported ``httpx`` for an
      unrelated (cloud-provider) reason, which would make an in-process ``'httpx' not in sys.
      modules`` assertion meaningless.

  AC2 (STT outage, both fallback slots): ``test_ac2_...`` — a cloud STT provider is selected via
      ``build_transcriber`` (a fake resolving ``VoiceKeyStore``, NEVER the real keyring, Q-57(a))
      with the primary transcriber raising on every ``transcribe()`` call, then driven through the
      REAL ``ASRSource.poll()`` over a real drop directory (``tmp_path``): with a local fallback
      present, the transcript is produced via that fallback and the file lands in ``processed/``;
      with local absent (the fallback slot itself ``None``, mirroring ``_build_local_transcriber``'s
      own ``ImportError``-degrades-to-``None`` contract), a two-file poll never crashes — each
      file independently lands in ``failed/``.

  AC3 (TTS outage, both fallback slots): ``test_ac3_...`` — a cloud TTS provider is selected via
      ``build_tts_adapter`` with the primary adapter raising on every ``speak()`` call, then driven
      through the REAL ``SpeakSink(voice_enabled=True, adapter=...)`` over a gate-surfaced composed
      output (TK-165's own fixture shape): with a local fallback present, the sink returns ``Done``
      and the fallback recorded exactly one call; with local absent, the sink returns
      ``Degraded(to=None)`` and the composed-output artifact is byte-unaffected (TK-165 parity —
      deep-copied before, compared after).

  Structural direction proof (DEC-28): ``test_structural_...`` — extends TK-193's own
      fallback-slot-is-always-local-or-none assertion (``tests/voice/test_select.py``) from the
      LESION side: after an actual primary failure has been driven through both ``ASRSource`` and
      ``SpeakSink``, the wrapper's ``_fallback`` slot is still never a cloud instance and its
      ``_primary`` slot is still never a local instance — no lesion path can invert DEC-28's
      cloud-to-local-only direction.

CI tests use fakes ONLY — an in-memory ``VoiceKeyStore`` fake, monkeypatched provider classes,
and (for AC1) a raises-on-construction sentinel. NEVER the real keyring (Q-57(a)) and NEVER a
live network call (DEF-7).
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cogworx.claims.provenance import Artifact, Provenance
from cogworx.loop.result import Degraded, Done

import wombat.voice.select as select_module
from tests.support.stage_context_fake import StageContextFake
from wombat.config import WombatConfig
from wombat.gate.models import ItemKind
from wombat.sinks.speak import SpeakSink
from wombat.sources.asr import ASRSource
from wombat.stages.artifacts import (
    COMPOSED_OUTPUT,
    composed_output_to_artifact_data,
    speech_output_to_artifact_data,
    spoken_output_from_artifact_data,
)
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
    resolution (relative to CWD) can never pick up the operator's populated .env (mirrors
    ``tests/voice/test_select.py``'s own autouse fixture)."""
    monkeypatch.chdir(tmp_path)


# ------------------------------------------------------------------------------------------ fakes


class _FakeVoiceKeyStore:
    """The in-memory ``VoiceKeyStore`` fake — unit tests never touch the real vault (Q-57(a))."""

    def __init__(self, *, initial: dict[str, str] | None = None) -> None:
        self._values = dict(initial or {})

    def get(self, provider: str) -> str | None:
        return self._values.get(provider)

    def set(self, provider: str, key: str) -> None:
        self._values[provider] = key

    def delete(self, provider: str) -> None:
        self._values.pop(provider, None)


class _FakeLocalTranscriber:
    """Stands in for ``FasterWhisperTranscriber`` — records every ``transcribe()`` call, never
    loads a real model."""

    def __init__(self, *, model_name: str) -> None:
        self.model_name = model_name
        self.calls: list[Path] = []

    def transcribe(self, path: Path) -> str:
        self.calls.append(path)
        return "local transcript"


class _AbsentLocalTranscriber:
    """Stands in for ``FasterWhisperTranscriber`` on a checkout without the ``[voice]`` extra —
    raises ``ImportError`` at construction, the exact exception ``_build_local_transcriber``
    catches and degrades to ``None`` for."""

    def __init__(self, *, model_name: str) -> None:
        raise ImportError("simulated absent faster-whisper (TK-195)")


class _RaisingCloudTranscriber:
    """A cloud STT stand-in that constructs fine but raises on every ``transcribe()`` call — the
    mid-call cloud-outage path (AC2)."""

    def __init__(self, api_key: str, *, model: str | None = None) -> None:
        self.api_key = api_key
        self.model = model

    def transcribe(self, path: Path) -> str:
        raise RuntimeError("cloud STT exploded mid-call")


class _FakeLocalTTS:
    """Stands in for ``Pyttsx3Adapter`` — records every ``speak()`` call, never inits a real OS
    TTS engine."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def speak(self, text: str) -> None:
        self.calls.append(text)


class _AbsentLocalTTS:
    """Stands in for ``Pyttsx3Adapter`` on a checkout without the ``[voice]`` extra (or a broken
    OS TTS engine) — raises at construction, the exact broad-``Exception`` shape
    ``_build_local_tts`` catches and degrades to ``None`` for."""

    def __init__(self) -> None:
        raise RuntimeError("simulated absent/broken local TTS engine (TK-195)")


class _RaisingCloudTTS:
    """A cloud TTS stand-in that constructs fine but raises on every ``speak()`` call — the
    mid-call cloud-outage path (AC3)."""

    def __init__(self, api_key: str, *, voice_id: str | None = None) -> None:
        self.api_key = api_key
        self.voice_id = voice_id

    def speak(self, text: str) -> None:
        raise RuntimeError("cloud TTS exploded mid-call")


# ---------------------------------------------------------------------------------------------AC1
#
# Q-105(f) binding proof shape: a FRESH subprocess. In-process, an earlier test module may already
# have imported httpx for an unrelated (cloud-provider) reason, which would make an in-process
# 'httpx' not in sys.modules assertion meaningless — only a fresh interpreter proves the claim.

_AC1_SCRIPT = textwrap.dedent(
    '''
    import sys
    from zoneinfo import ZoneInfo

    import wombat.voice.select as select_module
    from wombat.bootstrap import build_speak_sink, make_speak_callable
    from wombat.config import WombatConfig
    from wombat.queue import EnqueueResult
    from wombat.sources.bootstrap import build_source_registry
    from wombat.voice.select import build_transcriber, build_tts_adapter


    class _RaisingIfConstructed:
        """Stands in for EVERY wombat.voice cloud class — fails the moment it is
        instantiated, proving none of them fired on the default-config path."""

        def __init__(self, *args, **kwargs):
            raise AssertionError(
                "a wombat.voice cloud class was constructed on the default-config path"
            )


    class _FakeLocalTranscriber:
        def __init__(self, *, model_name):
            self.model_name = model_name

        def transcribe(self, path):
            raise AssertionError("not exercised in this proof")


    class _FakeLocalTTS:
        def speak(self, text):
            raise AssertionError("not exercised in this proof")


    for _name in (
        "DeepgramTranscriber",
        "ElevenLabsScribeTranscriber",
        "FishAudioTranscriber",
        "DeepgramAuraTTSAdapter",
        "ElevenLabsTTSAdapter",
        "FishAudioTTSAdapter",
    ):
        setattr(select_module, _name, _RaisingIfConstructed)
    # The local classes ALSO never actually construct for real here (no model download / OS TTS
    # engine bring-up in a lesion test) -- substituted with lightweight fakes, mirroring
    # tests/voice/test_select.py's own AC1 tests.
    select_module.FasterWhisperTranscriber = _FakeLocalTranscriber
    select_module.Pyttsx3Adapter = _FakeLocalTTS


    class _FakeKeyStore:
        """Proves the default local path never reads a key store at all."""

        def get(self, provider):
            raise AssertionError("the default local path must never read the key store")

        def set(self, provider, key):
            raise AssertionError("not exercised")

        def delete(self, provider):
            raise AssertionError("not exercised")


    class _FakeEnqueuer:
        def enqueue(self, item):
            return EnqueueResult.QUEUED


    class _FakeTokenStore:
        """An in-memory gcal/gmail TokenStore fake -- no Google/keyring touch (Q-61/Q-67)."""

        def load(self):
            return None

        def save(self, token):
            raise AssertionError("not exercised")

        def clear(self):
            raise AssertionError("not exercised")


    config = WombatConfig(
        deepseek_api_key="sk-test", deepseek_base_url="https://api.deepseek.com"
    )
    assert config.wombat_voice_enabled is False
    assert config.wombat_stt_provider == "local"
    assert config.wombat_tts_provider == "local"
    assert config.wombat_asr_drop_dir is None

    registry = build_source_registry(
        config,
        _FakeEnqueuer(),
        tz=ZoneInfo("UTC"),
        gcal_token_store=_FakeTokenStore(),
        gmail_token_store=_FakeTokenStore(),
    )
    speak_sink = build_speak_sink(config)
    speak_callable = make_speak_callable(config)
    transcriber = build_transcriber(config, key_store=_FakeKeyStore())
    adapter = build_tts_adapter(config, key_store=_FakeKeyStore())

    # voice_enabled=False -> build_speak_sink/make_speak_callable never even attempt
    # construction (bootstrap.py's own contract) -- no cloud, no local.
    assert speak_sink._adapter is None
    assert speak_callable is None
    # provider="local" -> build_transcriber/build_tts_adapter return the bare local fake,
    # never a cloud class, never a Fallback* wrapper.
    assert isinstance(transcriber, _FakeLocalTranscriber)
    assert isinstance(adapter, _FakeLocalTTS)
    assert "httpx" not in sys.modules
    print("AC1-OK")
    '''
)


def test_ac1_default_config_constructs_zero_cloud_voice_and_never_imports_httpx(
    tmp_path: Path,
) -> None:
    result = subprocess.run(
        [sys.executable, "-c", _AC1_SCRIPT],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, (
        f"AC1 subprocess proof failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "AC1-OK" in result.stdout


# ---------------------------------------------------------------------------------------------AC2


async def test_ac2_stt_outage_with_local_present_falls_back_and_processes_the_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(select_module, "DeepgramTranscriber", _RaisingCloudTranscriber)
    monkeypatch.setattr(select_module, "FasterWhisperTranscriber", _FakeLocalTranscriber)
    store = _FakeVoiceKeyStore(initial={"deepgram": "cloud-key"})
    config = _config(wombat_stt_provider="deepgram")

    transcriber = build_transcriber(config, key_store=store)
    assert isinstance(transcriber, FallbackTranscriber)

    audio = tmp_path / "clip.wav"
    audio.write_bytes(b"RIFF....WAVEfmt ")
    source = ASRSource(drop_dir=tmp_path, transcriber=transcriber, poll_interval_seconds=300.0)

    events = await source.poll()

    assert len(events) == 1
    assert events[0].payload["transcript"] == "local transcript"
    assert (tmp_path / "processed" / "clip.wav").exists()
    assert not (tmp_path / "failed" / "clip.wav").exists()

    fallback = transcriber._fallback
    assert isinstance(fallback, _FakeLocalTranscriber)
    assert fallback.calls == [audio]  # invoked exactly once


async def test_ac2_stt_outage_with_local_absent_never_crashes_two_files_land_in_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(select_module, "DeepgramTranscriber", _RaisingCloudTranscriber)
    monkeypatch.setattr(select_module, "FasterWhisperTranscriber", _AbsentLocalTranscriber)
    store = _FakeVoiceKeyStore(initial={"deepgram": "cloud-key"})
    config = _config(wombat_stt_provider="deepgram")

    transcriber = build_transcriber(config, key_store=store)
    assert isinstance(transcriber, FallbackTranscriber)
    assert transcriber._fallback is None  # local absent -> the fallback slot itself is None

    first = tmp_path / "a.wav"
    second = tmp_path / "b.wav"
    first.write_bytes(b"RIFF....WAVEfmt one")
    second.write_bytes(b"RIFF....WAVEfmt two")
    source = ASRSource(drop_dir=tmp_path, transcriber=transcriber, poll_interval_seconds=300.0)

    events = await source.poll()  # must not raise

    assert events == []
    assert (tmp_path / "failed" / "a.wav").exists()
    assert (tmp_path / "failed" / "b.wav").exists()
    processed_dir = tmp_path / "processed"
    assert not processed_dir.exists() or list(processed_dir.iterdir()) == []


# ---------------------------------------------------------------------------------------------AC3

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


def _speech_output_artifact() -> Artifact:
    """The ``speech_shape`` hop's output — the TEXT ``SpeakSink`` actually speaks (TK-267)."""
    return Artifact(
        kind="wombat.speech_output",
        produced_by="speech_shape",
        provenance=Provenance(source="system", confidence=1.0, recorded_at=_FIXED_NOW),
        data=speech_output_to_artifact_data(_ITEM_ID, _ITEM_KIND, _TEXT, False),
    )


def _ctx(compose_artifact: Artifact) -> StageContextFake:
    return StageContextFake(
        now_fn=lambda: _FIXED_NOW,
        last_output_map={
            "compose": compose_artifact,
            "speech_shape": _speech_output_artifact(),
        },
    )


async def test_ac3_tts_outage_with_local_present_speaks_via_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(select_module, "FishAudioTTSAdapter", _RaisingCloudTTS)
    monkeypatch.setattr(select_module, "Pyttsx3Adapter", _FakeLocalTTS)
    store = _FakeVoiceKeyStore(initial={"fish": "cloud-key"})
    config = _config(wombat_tts_provider="fish", wombat_tts_voice_id="voice-123")

    adapter = build_tts_adapter(config, key_store=store)
    assert isinstance(adapter, FallbackTTSAdapter)

    stage = SpeakSink(voice_enabled=True, adapter=adapter)
    compose_artifact = _composed_output_artifact()
    snapshot = compose_artifact.model_copy(deep=True)
    ctx = _ctx(compose_artifact)

    result = await stage.run(ctx)

    assert isinstance(result, Done)
    _item_id, _item_kind, spoken, degraded = spoken_output_from_artifact_data(result.output.data)
    assert spoken is True
    assert degraded is False
    fallback = adapter._fallback
    assert isinstance(fallback, _FakeLocalTTS)
    assert fallback.calls == [_TEXT]  # invoked exactly once
    assert compose_artifact == snapshot  # composed_output artifact byte-unaffected


async def test_ac3_tts_outage_with_local_absent_degrades_to_none_text_unaffected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(select_module, "FishAudioTTSAdapter", _RaisingCloudTTS)
    monkeypatch.setattr(select_module, "Pyttsx3Adapter", _AbsentLocalTTS)
    store = _FakeVoiceKeyStore(initial={"fish": "cloud-key"})
    config = _config(wombat_tts_provider="fish", wombat_tts_voice_id="voice-123")

    adapter = build_tts_adapter(config, key_store=store)
    assert isinstance(adapter, FallbackTTSAdapter)
    assert adapter._fallback is None  # local absent -> the fallback slot itself is None

    stage = SpeakSink(voice_enabled=True, adapter=adapter)
    compose_artifact = _composed_output_artifact()
    snapshot = compose_artifact.model_copy(deep=True)
    ctx = _ctx(compose_artifact)

    result = await stage.run(ctx)  # must not raise

    assert isinstance(result, Degraded)
    assert result.to is None
    assert result.reason
    _item_id, _item_kind, spoken, degraded = spoken_output_from_artifact_data(result.output.data)
    assert spoken is False
    assert degraded is True
    assert compose_artifact == snapshot  # composed_output artifact byte-unaffected


# --------------------------------------------------------------------------- structural direction


async def test_structural_fallback_slot_never_holds_a_cloud_instance_after_a_lesion(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """DEC-28 structural proof, extended from the LESION side (TK-193's own ``test_select.py``
    proves it at SELECTION time; this proves it AFTER an actual primary failure has been driven
    through the real ``ASRSource``/``SpeakSink`` consumers) — the fallback slot is always the
    local type or ``None``, the primary slot is never the local type, on either axis."""
    monkeypatch.setattr(select_module, "DeepgramTranscriber", _RaisingCloudTranscriber)
    monkeypatch.setattr(select_module, "FasterWhisperTranscriber", _FakeLocalTranscriber)
    monkeypatch.setattr(select_module, "FishAudioTTSAdapter", _RaisingCloudTTS)
    monkeypatch.setattr(select_module, "Pyttsx3Adapter", _FakeLocalTTS)

    stt_store = _FakeVoiceKeyStore(initial={"deepgram": "cloud-key"})
    stt_config = _config(wombat_stt_provider="deepgram")
    transcriber = build_transcriber(stt_config, key_store=stt_store)
    assert isinstance(transcriber, FallbackTranscriber)

    audio = tmp_path / "clip.wav"
    audio.write_bytes(b"RIFF....WAVEfmt ")
    source = ASRSource(drop_dir=tmp_path, transcriber=transcriber, poll_interval_seconds=300.0)
    await source.poll()

    assert transcriber._fallback is None or isinstance(
        transcriber._fallback, _FakeLocalTranscriber
    )
    assert not isinstance(transcriber._fallback, _RaisingCloudTranscriber)
    assert not isinstance(transcriber._primary, _FakeLocalTranscriber)

    tts_store = _FakeVoiceKeyStore(initial={"fish": "cloud-key"})
    tts_config = _config(wombat_tts_provider="fish", wombat_tts_voice_id="voice-123")
    adapter = build_tts_adapter(tts_config, key_store=tts_store)
    assert isinstance(adapter, FallbackTTSAdapter)

    stage = SpeakSink(voice_enabled=True, adapter=adapter)
    ctx = _ctx(_composed_output_artifact())
    await stage.run(ctx)

    assert adapter._fallback is None or isinstance(adapter._fallback, _FakeLocalTTS)
    assert not isinstance(adapter._fallback, _RaisingCloudTTS)
    assert not isinstance(adapter._primary, _FakeLocalTTS)
