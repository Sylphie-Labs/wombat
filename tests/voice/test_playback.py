"""TK-191/TK-262/TK-264 acceptance criteria (playback half) — ``AudioPlayer`` protocol +
``WinsoundPlayer`` (EP-31, CST-1, DEC-53a, ISS-17).

TK-264 (ISS-17): sentinel-length WAV normalization runs BEFORE the TK-262/DEC-53a validation —
``test_play_normalizes_sentinel_sizes_then_plays`` (incl. the exact live Fish shape),
``test_normalize_sentinel_sizes_does_not_patch_non_sentinel_overrun``,
``test_normalize_sentinel_sizes_is_noop_on_well_formed_wav``,
``test_normalize_sentinel_sizes_returns_unchanged_on_unparseable_bytes``.

AC3 (clean-checkout import bar / lazy winsound; construction + play on win32):
``test_playback_module_imports_without_winsound_installed``,
``test_winsound_player_constructs_on_win32``,
``test_winsound_player_play_calls_winsound_playsound_with_snd_memory``.

TK-262 (DEC-53a, ISS-15): ``play()`` validates ``wav_bytes`` BEFORE ever calling
``winsound.PlaySound`` — malformed/truncated audio raises ``ValueError`` instead of taking a
native access violation. ``test_play_rejects_*`` (empty / non-RIFF / truncated) and
``test_play_accepts_well_formed_wav_and_forwards_byte_identical``. The SpeakSink regression
(voice-enabled, adapter raising the new ``ValueError``, still degrades cleanly) is
``test_speak_sink_degrades_cleanly_when_adapter_raises_wav_validation_error``.

No audible playback in tests — ``winsound.PlaySound`` itself is monkeypatched in the play tests.
"""

from __future__ import annotations

import importlib
import io
import sys
import wave
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from importlib.abc import MetaPathFinder
from importlib.machinery import ModuleSpec
from types import ModuleType

import pytest
from cogworx.claims.provenance import Artifact, Provenance
from cogworx.loop.result import Degraded

from tests.support.stage_context_fake import StageContextFake
from wombat.gate.models import ItemKind
from wombat.sinks.speak import SpeakSink
from wombat.stages.artifacts import (
    COMPOSED_OUTPUT,
    composed_output_to_artifact_data,
    spoken_output_from_artifact_data,
)
from wombat.voice.playback import AudioPlayer, WinsoundPlayer, _normalize_sentinel_sizes


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


def test_playback_module_imports_without_winsound_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC3: importing ``wombat.voice.playback`` never touches ``winsound`` — only constructing
    ``WinsoundPlayer`` does."""
    _simulate_absent(monkeypatch, "winsound")
    assert "winsound" not in sys.modules
    importlib.reload(importlib.import_module("wombat.voice.playback"))
    assert "winsound" not in sys.modules


@pytest.mark.skipif(sys.platform != "win32", reason="winsound is Windows-only (CST-1)")
def test_winsound_player_constructs_on_win32() -> None:
    """AC3: on Windows, ``winsound`` is stdlib-satisfied, so construction succeeds."""
    player: AudioPlayer = WinsoundPlayer()
    assert isinstance(player, WinsoundPlayer)


def _make_wav_bytes(*, nframes: int = 8, nchannels: int = 1, sampwidth: int = 2) -> bytes:
    """A small, well-formed WAV buffer, built via the stdlib ``wave`` module (never hand-rolled)."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(nchannels)
        wav.setsampwidth(sampwidth)
        wav.setframerate(8000)
        wav.writeframes(b"\x00" * (nframes * nchannels * sampwidth))
    return buf.getvalue()


@pytest.mark.skipif(sys.platform != "win32", reason="winsound is Windows-only (CST-1)")
def test_winsound_player_play_calls_winsound_playsound_with_snd_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC3/AC2 (TK-262): a well-formed WAV reaches ``winsound.PlaySound`` with the ``SND_MEMORY``
    flag and the ORIGINAL bytes, byte-identical — monkeypatched, so no audible playback happens in
    tests."""
    import winsound

    calls: list[tuple[bytes, int]] = []
    monkeypatch.setattr(
        winsound, "PlaySound", lambda sound, flags: calls.append((sound, flags))
    )

    wav_bytes = _make_wav_bytes()
    player = WinsoundPlayer()
    player.play(wav_bytes)

    assert calls == [(wav_bytes, winsound.SND_MEMORY)]


# --- TK-262 (DEC-53a, ISS-15): malformed WAV bytes raise before winsound.PlaySound -------------


@pytest.mark.skipif(sys.platform != "win32", reason="winsound is Windows-only (CST-1)")
@pytest.mark.parametrize(
    "wav_bytes",
    [
        pytest.param(b"", id="empty"),
        pytest.param(b"not a wav file at all, just random junk bytes" * 4, id="non-riff"),
        pytest.param(_make_wav_bytes(nframes=100)[:60], id="truncated-overrunning-declaration"),
    ],
)
def test_play_rejects_malformed_wav_bytes_without_ever_calling_playsound(
    monkeypatch: pytest.MonkeyPatch, wav_bytes: bytes
) -> None:
    """AC1: empty bytes, non-RIFF bytes, and a WAV whose declared data overruns the actual buffer
    each raise ``ValueError`` naming the defect class and the byte length — and the spy proves
    ``winsound.PlaySound`` was NEVER invoked."""
    import winsound

    calls: list[tuple[bytes, int]] = []
    monkeypatch.setattr(
        winsound, "PlaySound", lambda sound, flags: calls.append((sound, flags))
    )

    player = WinsoundPlayer()
    with pytest.raises(ValueError, match=str(len(wav_bytes))):
        player.play(wav_bytes)

    assert calls == []


# --- TK-264 (ISS-17): sentinel-length WAV normalization, BEFORE the TK-262/DEC-53a validation ---


def _poison_data_size(wav_bytes: bytes, sentinel: int) -> bytes:
    """Overwrite a well-formed WAV's ``data`` chunk size field with ``sentinel``, leaving the
    rest of the (self-consistent) buffer untouched — the exact shape Fish.audio's streaming
    encoder produces (ISS-17)."""
    idx = wav_bytes.find(b"data")
    assert idx != -1, "fixture must already contain a data sub-chunk"
    poisoned = bytearray(wav_bytes)
    poisoned[idx + 4 : idx + 8] = sentinel.to_bytes(4, "little")
    return bytes(poisoned)


def _poison_riff_size(wav_bytes: bytes, sentinel: int) -> bytes:
    """Overwrite a well-formed WAV's RIFF size field (bytes 4-7) with ``sentinel``."""
    poisoned = bytearray(wav_bytes)
    poisoned[4:8] = sentinel.to_bytes(4, "little")
    return bytes(poisoned)


@pytest.mark.skipif(sys.platform != "win32", reason="winsound is Windows-only (CST-1)")
@pytest.mark.parametrize(
    "poison",
    [
        pytest.param(
            lambda good: _poison_data_size(good, 0xFFFFFF00),
            id="data-sentinel-live-fish-shape",
        ),
        pytest.param(
            lambda good: _poison_data_size(good, 0xFFFFFFFF),
            id="data-sentinel-all-ones",
        ),
        pytest.param(
            lambda good: _poison_riff_size(good, 0xFFFFFFFF),
            id="riff-sentinel",
        ),
        pytest.param(
            lambda good: _poison_riff_size(_poison_data_size(good, 0xFFFFFF00), 0xFFFFFFFF),
            id="both-riff-and-data-sentinel",
        ),
    ],
)
def test_play_normalizes_sentinel_sizes_then_plays(
    monkeypatch: pytest.MonkeyPatch, poison: Callable[[bytes], bytes]
) -> None:
    """AC1: a self-consistent WAV whose RIFF and/or ``data`` size field carries a known
    unknown-length sentinel (including the exact live Fish shape, declared data size
    4294967040 over a much smaller actual body) is patched to the buffer's actual lengths
    BEFORE validation, and ``winsound.PlaySound`` is invoked exactly once with the patched
    (here: reconstructed-original) bytes."""
    import winsound

    calls: list[tuple[bytes, int]] = []
    monkeypatch.setattr(winsound, "PlaySound", lambda sound, flags: calls.append((sound, flags)))

    good = _make_wav_bytes(nframes=100)
    poisoned = poison(good)
    assert poisoned != good  # sanity: the fixture is actually poisoned

    player = WinsoundPlayer()
    player.play(poisoned)

    assert calls == [(good, winsound.SND_MEMORY)]


def test_normalize_sentinel_sizes_does_not_patch_non_sentinel_overrun() -> None:
    """AC2: a genuinely truncated buffer's (non-sentinel) declared size is never rewritten —
    normalization is a strict no-op on it, so validation's existing overrun check still fires."""
    truncated = _make_wav_bytes(nframes=100)[:60]
    assert _normalize_sentinel_sizes(truncated) is truncated


def test_normalize_sentinel_sizes_is_noop_on_well_formed_wav() -> None:
    """AC3: a well-formed, already-consistent WAV passes through byte-identical (same object,
    no gratuitous rewrite)."""
    good = _make_wav_bytes(nframes=100)
    assert _normalize_sentinel_sizes(good) is good


def test_normalize_sentinel_sizes_returns_unchanged_on_unparseable_bytes() -> None:
    """Bytes that are not parseable as RIFF/WAVE at all are returned UNCHANGED, letting
    validation raise its own existing errors."""
    junk = b"not a wav file at all, just random junk bytes" * 4
    assert _normalize_sentinel_sizes(junk) is junk


# --- AC3 (TK-262): SpeakSink already contains any adapter exception (regression, no new code) --


_FIXED_NOW = datetime(2026, 7, 19, 12, 0, 0, tzinfo=UTC)
_ITEM_ID = "gate-item-1"
_ITEM_KIND = ItemKind.GENERIC
_TEXT = "You have a new alert."


class _WavValidationRaisingAdapter:
    """A ``TTSAdapter`` double whose ``speak()`` raises the same ``ValueError`` shape
    ``WinsoundPlayer.play()`` now raises on poisoned WAV bytes (TK-262)."""

    def __init__(self) -> None:
        self.call_count = 0

    def speak(self, text: str) -> None:
        self.call_count += 1
        raise ValueError("truncated WAV audio (20 bytes): declared frame data overruns buffer")


async def test_speak_sink_degrades_cleanly_when_adapter_raises_wav_validation_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC3 (regression, no new sink code): SpeakSink wired voice-enabled with a fake adapter whose
    ``speak()`` raises the validation ``ValueError`` returns the existing terminal ``Degraded``
    artifact (spoken=False, degraded=True); nothing raises, and a warning is logged."""
    adapter = _WavValidationRaisingAdapter()
    stage = SpeakSink(voice_enabled=True, adapter=adapter)
    compose_artifact = Artifact(
        kind=COMPOSED_OUTPUT,
        produced_by="compose",
        provenance=Provenance(source="system", confidence=1.0, recorded_at=_FIXED_NOW),
        data=composed_output_to_artifact_data(_TEXT, _ITEM_ID, _ITEM_KIND, False),
    )
    ctx = StageContextFake(
        now_fn=lambda: _FIXED_NOW,
        last_output_map={"compose": compose_artifact},
    )

    with caplog.at_level("WARNING"):
        result = await stage.run(ctx)  # must not raise

    assert adapter.call_count == 1
    assert isinstance(result, Degraded)
    assert result.to is None
    _item_id, _item_kind, spoken, degraded = spoken_output_from_artifact_data(result.output.data)
    assert spoken is False
    assert degraded is True
    assert any(
        record.levelname == "WARNING" and "TTS adapter failed" in record.message
        for record in caplog.records
    )
