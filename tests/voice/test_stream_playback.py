"""TK-331 acceptance criteria — StreamingAudioWriter over sounddevice (EP-31, DEC-73c).

AC1 (frame discipline, torn boundaries): ``test_write_carries_torn_frame_remainder_and_
preserves_all_audible_bytes`` — every buffer submitted to the fake stream is a whole-frame
multiple, the odd-byte remainder is carried into the next ``write()`` call, and total audible
bytes equal total bytes sent.

AC2 (blocking-until-drained / abort / write failure): ``test_finish_drains_before_closing_in_
call_order``, ``test_abort_stops_without_draining``, ``test_write_failure_raises_and_is_never_
swallowed``.

AC3 (clean-checkout import bar / lazy sounddevice import): ``test_module_imports_without_
sounddevice_installed``, ``test_construction_without_factory_raises_importerror_without_
sounddevice``, ``test_streaming_available_returns_false_without_sounddevice``.

AC4 (module separation, structural grep): ``test_stream_playback_source_has_no_capture_or_
input_api_token`` (the existing DEC-68a ``observe_mic.py`` pin lives in a different module,
untouched by this ticket).

Every test rides a fake ``AudioOutputStream`` (or blocks the real ``sounddevice`` import
entirely) — ZERO real audio hardware touched (DEF-7).
"""

from __future__ import annotations

import importlib
import re
import sys
from collections.abc import Sequence
from importlib.abc import MetaPathFinder
from importlib.machinery import ModuleSpec
from pathlib import Path
from types import ModuleType

import pytest

from wombat.voice import stream_playback
from wombat.voice.stream_playback import FRAME_BYTES, StreamingAudioWriter, streaming_available


class _FakeStream:
    """A fake ``AudioOutputStream`` recording every call, in order, so tests can assert both the
    submitted bytes AND the ``write``/``stop``/``abort``/``close`` call order (AC1/AC2) — never
    touches real audio hardware."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, bytes | None]] = []

    def write(self, data: bytes) -> None:
        self.calls.append(("write", data))

    def stop(self) -> None:
        self.calls.append(("stop", None))

    def abort(self) -> None:
        self.calls.append(("abort", None))

    def close(self) -> None:
        self.calls.append(("close", None))

    @property
    def written(self) -> list[bytes]:
        return [data for kind, data in self.calls if kind == "write" and data is not None]


class _RaisingWriteStream:
    """A fake ``AudioOutputStream`` whose ``write`` always raises (AC2 — CON-3)."""

    def write(self, data: bytes) -> None:
        raise RuntimeError("simulated PortAudio write failure")

    def stop(self) -> None:  # pragma: no cover - never reached in the failure test
        pass

    def abort(self) -> None:  # pragma: no cover - never reached in the failure test
        pass

    def close(self) -> None:  # pragma: no cover - never reached in the failure test
        pass


# --- AC1: frame discipline, torn-frame carry ------------------------------------------------


def test_write_carries_torn_frame_remainder_and_preserves_all_audible_bytes() -> None:
    fake = _FakeStream()
    writer = StreamingAudioWriter(stream_factory=lambda: fake)

    # Chunk lengths 2, 1, 2, 1 — every odd chunk tears a frame boundary; total is 6 bytes, an
    # exact whole-frame multiple (3 frames at FRAME_BYTES == 2).
    chunks = [b"AB", b"C", b"DE", b"F"]
    for chunk in chunks:
        writer.write(chunk)

    for submitted in fake.written:
        assert len(submitted) % FRAME_BYTES == 0, f"torn frame submitted: {submitted!r}"

    total_sent = sum(len(chunk) for chunk in chunks)
    total_audible = sum(len(submitted) for submitted in fake.written)
    assert total_audible == total_sent
    assert b"".join(fake.written) == b"".join(chunks)


def test_write_before_a_whole_frame_is_available_opens_no_stream() -> None:
    """The very first odd byte alone can never complete a frame — nothing is submitted, and the
    stream (opened lazily, on first submitted write) is never even constructed."""
    open_count = 0

    def _factory() -> _FakeStream:
        nonlocal open_count
        open_count += 1
        return _FakeStream()

    writer = StreamingAudioWriter(stream_factory=_factory)
    writer.write(b"\x01")

    assert open_count == 0


# --- AC2: blocking-until-drained finish / non-draining abort / write failure raises ----------


def test_finish_drains_before_closing_in_call_order() -> None:
    fake = _FakeStream()
    writer = StreamingAudioWriter(stream_factory=lambda: fake)
    writer.write(b"AB")
    writer.write(b"CD")

    writer.finish()

    # Event-ordering proof (never a wall-clock sleep): every write precedes stop, which
    # precedes close — finish() returns only after the fake stream reports the drain (stop).
    kinds = [kind for kind, _ in fake.calls]
    assert kinds == ["write", "write", "stop", "close"]


def test_finish_with_nothing_written_is_a_harmless_no_op() -> None:
    fake = _FakeStream()
    writer = StreamingAudioWriter(stream_factory=lambda: fake)

    writer.finish()  # must not raise, must not touch the stream

    assert fake.calls == []


def test_abort_stops_without_draining() -> None:
    fake = _FakeStream()
    writer = StreamingAudioWriter(stream_factory=lambda: fake)
    writer.write(b"AB")

    writer.abort()

    kinds = [kind for kind, _ in fake.calls]
    assert kinds == ["write", "abort", "close"]
    assert "stop" not in kinds


def test_write_failure_raises_and_is_never_swallowed() -> None:
    writer = StreamingAudioWriter(stream_factory=lambda: _RaisingWriteStream())

    with pytest.raises(RuntimeError, match="simulated PortAudio write failure"):
        writer.write(b"AB")


# --- AC3: clean-checkout import bar --------------------------------------------------------


class _BlockedFinder(MetaPathFinder):
    """A meta-path finder that fails the import of one named module (and its submodules) — the
    same idiom ``tests/voice/test_tts_fish.py`` uses for its httpx-absent proof."""

    def __init__(self, blocked: str) -> None:
        self._blocked = blocked

    def find_spec(
        self, fullname: str, path: Sequence[str] | None, target: ModuleType | None = None
    ) -> ModuleSpec | None:
        if fullname == self._blocked or fullname.startswith(f"{self._blocked}."):
            raise ModuleNotFoundError(f"No module named {fullname!r} (simulated absence, TK-331)")
        return None


def _simulate_absent(monkeypatch: pytest.MonkeyPatch, module_name: str) -> None:
    """Simulate ``module_name`` being genuinely not installed, robust to the module actually
    being present in this environment."""
    monkeypatch.delitem(sys.modules, module_name, raising=False)
    monkeypatch.setattr(sys, "meta_path", [_BlockedFinder(module_name), *sys.meta_path])


def test_module_imports_without_sounddevice_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC3: importing ``wombat.voice.stream_playback`` never touches ``sounddevice`` — only
    constructing the default ``StreamingAudioWriter`` (no injected factory) does."""
    _simulate_absent(monkeypatch, "sounddevice")
    assert "sounddevice" not in sys.modules
    importlib.reload(importlib.import_module("wombat.voice.stream_playback"))
    assert "sounddevice" not in sys.modules


def test_construction_without_factory_raises_importerror_without_sounddevice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC3: constructing a ``StreamingAudioWriter`` WITHOUT an injected ``stream_factory`` (the
    default arg, which lazily imports ``sounddevice``) raises ``ImportError`` when the
    ``voice-cloud`` extra is absent — the real, unmocked lazy-import-failure path."""
    _simulate_absent(monkeypatch, "sounddevice")
    with pytest.raises(ImportError):
        StreamingAudioWriter()


def test_streaming_available_returns_false_without_sounddevice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC3: ``streaming_available()`` probes the lazy import ONLY and returns ``False`` when
    ``sounddevice`` cannot be imported — never opening a stream."""
    _simulate_absent(monkeypatch, "sounddevice")
    assert streaming_available() is False


# --- AC4: module separation, structural grep -------------------------------------------------

_FORBIDDEN_SUBSTRINGS = ("InputStream", "sounddevice.rec", "pyaudio")
_FORBIDDEN_WORD_PATTERN = re.compile(r"\brec\b")


def test_stream_playback_source_has_no_capture_or_input_api_token() -> None:
    """DEC-73c/EP-31 module-separation structural pin: this writer is OUTPUT-ONLY. No
    capture/input-API token (``InputStream``/``sounddevice.rec``/``pyaudio``, or a bare ``rec``
    token) appears anywhere in the module source."""
    source = Path(stream_playback.__file__).read_text(encoding="utf-8")
    for token in _FORBIDDEN_SUBSTRINGS:
        assert token not in source, f"forbidden capture/input API token found: {token!r}"
    assert not _FORBIDDEN_WORD_PATTERN.search(source), "forbidden bare 'rec' token found"
