"""TK-162 acceptance criteria — ASRSource + FasterWhisperTranscriber (Q-97).

AC1: registered in a ``SourceRegistry`` over a fake enqueuer, a dropped file survives one poll
tick as exactly one ``QueueItem`` whose ``idempotency_key`` derives from ``("asr", <sha256 of
the file bytes>)`` via the canonical TK-12 function, carrying the transcript in its payload.
Re-polling after the move yields no new event; re-dropping identical bytes yields the SAME
``event_key`` (the queue is the thing that dedupes — this suite only proves the key matches).

AC3: ``SourceRegistry.source_ids`` contains ``"asr"`` alongside any other registered id, via
the SAME ``register()`` call every other source uses — no special-cased dispatch branch.

AC4 (lesion): a failing/corrupt file is moved to ``failed/``, a warning is logged, and the
poll still returns the OTHER files' events (one bad file never kills the source). A
scan-level error (the drop directory itself missing) degrades the whole poll to ``[]``.

``FasterWhisperTranscriber``'s lazy-import contract is proven directly: faster-whisper rides the
optional ``[voice]`` extra, never a core dep, but a dev/operator checkout MAY have it installed
anyway (Q-103) — so the absence the construction-failure assertion below needs is SIMULATED via
``_simulate_absent`` (TK-202) rather than assumed from the environment: importing
``wombat.sources.asr`` (and constructing any OTHER ``Transcriber``) never touches it regardless.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from importlib.abc import MetaPathFinder
from importlib.machinery import ModuleSpec
from pathlib import Path
from types import ModuleType

import pytest

from wombat.domain.item_identity import idempotency_key as derive_key
from wombat.queue import EnqueueResult, QueueItem
from wombat.sources.asr import ASRSource, FasterWhisperTranscriber
from wombat.sources.registry import SourceRegistry

_NOW = datetime(2026, 7, 9, 12, 0, tzinfo=UTC)


def _clock() -> datetime:
    return _NOW


class _FakeEnqueuer:
    def __init__(self) -> None:
        self.items: list[QueueItem] = []

    def enqueue(self, item: QueueItem) -> EnqueueResult:
        self.items.append(item)
        return EnqueueResult.QUEUED


class _FakeTranscriber:
    def __init__(self, text: str = "hello wombat") -> None:
        self.text = text
        self.calls: list[Path] = []

    def transcribe(self, path: Path) -> str:
        self.calls.append(path)
        return self.text


async def _wait_until(
    predicate: Callable[[], bool], *, timeout: float = 2.0, interval: float = 0.005
) -> None:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() >= deadline:
            raise AssertionError(f"condition not met within {timeout}s")
        await asyncio.sleep(interval)


# --------------------------------------------------------------------------------------- AC1


async def test_dropped_file_becomes_one_queue_item_with_derived_idempotency_key(
    tmp_path: Path,
) -> None:
    audio = tmp_path / "note.wav"
    audio.write_bytes(b"content-a")
    expected_event_key = hashlib.sha256(b"content-a").hexdigest()

    source = ASRSource(
        drop_dir=tmp_path,
        transcriber=_FakeTranscriber("buy milk tomorrow"),
        poll_interval_seconds=0.01,
        clock=_clock,
    )
    enqueuer = _FakeEnqueuer()
    registry = SourceRegistry(enqueuer)
    registry.register(source)

    await registry.start()
    try:
        await _wait_until(lambda: len(enqueuer.items) >= 1)
    finally:
        await registry.stop()

    assert len(enqueuer.items) == 1
    item = enqueuer.items[0]
    assert item.idempotency_key == derive_key("asr", expected_event_key)
    assert item.payload["transcript"] == "buy milk tomorrow"
    assert item.payload["captured_at"] == _NOW.isoformat()

    # The file was moved out of the drop dir on success.
    assert not audio.exists()
    assert (tmp_path / "processed" / "note.wav").exists()


async def test_repoll_after_move_yields_no_new_event(tmp_path: Path) -> None:
    (tmp_path / "note.wav").write_bytes(b"content-b")
    source = ASRSource(
        drop_dir=tmp_path,
        transcriber=_FakeTranscriber(),
        poll_interval_seconds=0.01,
        clock=_clock,
    )

    first = await source.poll()
    second = await source.poll()

    assert len(first) == 1
    assert second == []


async def test_redropping_identical_bytes_yields_the_same_event_key(tmp_path: Path) -> None:
    source = ASRSource(
        drop_dir=tmp_path,
        transcriber=_FakeTranscriber(),
        poll_interval_seconds=0.01,
        clock=_clock,
    )

    (tmp_path / "first.wav").write_bytes(b"identical-content")
    first = await source.poll()
    (tmp_path / "second.wav").write_bytes(b"identical-content")
    second = await source.poll()

    assert len(first) == 1
    assert len(second) == 1
    assert first[0].event_key == second[0].event_key


# --------------------------------------------------------------------------------------- AC3


def test_asr_registers_alongside_other_ids_via_the_one_generic_register_call(
    tmp_path: Path,
) -> None:
    registry = SourceRegistry(_FakeEnqueuer())
    registry.register(
        ASRSource(drop_dir=tmp_path, transcriber=_FakeTranscriber(), poll_interval_seconds=99.0)
    )
    assert "asr" in registry.source_ids


# --------------------------------------------------------------------------------------- AC4


async def test_failing_file_moves_to_failed_and_other_files_still_processed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    (tmp_path / "good.wav").write_bytes(b"good-bytes")
    (tmp_path / "bad.wav").write_bytes(b"bad-bytes")

    class _MixedTranscriber:
        def transcribe(self, path: Path) -> str:
            if path.name == "bad.wav":
                raise RuntimeError("corrupt audio")
            return "ok transcript"

    source = ASRSource(
        drop_dir=tmp_path,
        transcriber=_MixedTranscriber(),
        poll_interval_seconds=99.0,
        clock=_clock,
    )

    with caplog.at_level(logging.WARNING):
        events = await source.poll()

    assert len(events) == 1
    assert events[0].payload["transcript"] == "ok transcript"
    assert (tmp_path / "processed" / "good.wav").exists()
    assert (tmp_path / "failed" / "bad.wav").exists()
    assert "bad.wav" in caplog.text


async def test_scan_level_error_degrades_the_whole_poll_to_empty_list(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    missing_dir = tmp_path / "does-not-exist"
    source = ASRSource(
        drop_dir=missing_dir, transcriber=_FakeTranscriber(), poll_interval_seconds=99.0
    )

    with caplog.at_level(logging.WARNING):
        events = await source.poll()

    assert events == []
    assert "failed to scan" in caplog.text


def test_non_audio_files_are_ignored(tmp_path: Path) -> None:
    (tmp_path / "readme.txt").write_bytes(b"not audio")
    transcriber = _FakeTranscriber()
    source = ASRSource(
        drop_dir=tmp_path, transcriber=transcriber, poll_interval_seconds=99.0, clock=_clock
    )

    events = asyncio.run(source.poll())

    assert events == []
    assert transcriber.calls == []
    assert (tmp_path / "readme.txt").exists()  # left untouched, never moved


# ------------------------------------------------------------- FasterWhisperTranscriber lazy import


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
    """Simulate ``module_name`` being genuinely not installed, regardless of whether it actually
    is on this machine (TK-202/Q-103): evict any cached import AND install a meta-path finder
    ahead of the real one so any subsequent import raises ``ModuleNotFoundError``."""
    monkeypatch.delitem(sys.modules, module_name, raising=False)
    monkeypatch.setattr(sys, "meta_path", [_BlockedFinder(module_name), *sys.meta_path])


def test_faster_whisper_transcriber_construction_raises_when_not_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real, unmocked lazy-import-failure path (mirrors TK-164's Pyttsx3Adapter test):
    faster-whisper is simulated absent (TK-202) — construction raises rather than silently
    no-oping, so ``sources.bootstrap._maybe_register_asr`` can catch it and degrade loud."""
    _simulate_absent(monkeypatch, "faster_whisper")
    with pytest.raises(ImportError):
        FasterWhisperTranscriber(model_name="base")
