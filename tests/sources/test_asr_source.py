"""TK-163 acceptance criteria — ASR source smoke + lesion (Q-97 as-ruled, EP-29).

Proves voice-in works micless (AC1) and that voice ABSENCE degrades to nothing — no import
error, no blocked non-voice capability (AC2, CON-3/DEC-7/S8 lesion). NO microphone and NO
faster-whisper install anywhere in this module: ``_FakeTranscriber`` stands in for the injected
``Transcriber`` Protocol throughout (mirrors TK-162's own ``tests/sources/test_asr.py``).

AC1: a fake ``Transcriber`` + a ``tmp_path`` drop dir with one dropped file, driven through a
REAL ``SourceRegistry`` poll tick over a recording enqueuer -> exactly one ``QueueItem`` whose
``idempotency_key`` derives from ``("asr", <sha256 of the dropped bytes>)`` via the canonical
TK-12 function, carrying the transcript in its payload. The enqueue happens through the
registry's ONE generic call site — no asr-specific dispatch, proven structurally (mirroring
TK-162's own "no special-case branch" AC): ``"asr"`` never appears in ``sources/registry.py``'s
source. A second poll tick enqueues nothing further (the file was moved out of the drop dir).

AC2 (lesion): ``build_source_registry`` (``sources/bootstrap.py``) with ``WOMBAT_ASR_DROP_DIR``
unset -> ``"asr"`` is absent from ``source_ids``, exactly one loud skip log names
``WOMBAT_ASR_DROP_DIR``, and — separately — a calendar-shaped ``QueueItem`` still drains cleanly
through ``DrainQueueStage`` (the SAME pure-stage fixture shape ``tests/unit/
test_drain_queue_stage.py`` uses, no Postgres required): missing ASR blocks nothing else.

AC3 (per-file failure): a ``Transcriber`` that raises for one of two dropped files still emits
the good file's event, moves the bad file to ``failed/``, logs a warning, and ``poll()`` itself
never raises.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from cogworx.loop.result import Transition
from pydantic import SecretStr

import wombat.sources.registry as registry_module
from tests.support.stage_context_fake import StageContextFake
from wombat.config import WombatConfig
from wombat.domain.item_identity import idempotency_key as derive_key
from wombat.queue import EnqueueResult, QueueItem
from wombat.sources.asr import ASRSource
from wombat.sources.bootstrap import build_source_registry
from wombat.sources.registry import SourceRegistry
from wombat.stages.artifacts import queue_items_from_artifact_data
from wombat.stages.drain_queue import DrainQueueStage

_NOW = datetime(2026, 7, 9, 12, 0, tzinfo=UTC)
_TZ = ZoneInfo("UTC")


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


def _make_config() -> WombatConfig:
    """No client creds, no ASR drop dir — the fully voice-and-google-less boot AC2 exercises."""
    return WombatConfig(
        deepseek_api_key=SecretStr("unused-in-this-test"),
        deepseek_base_url="https://unused.example",
    )


# --------------------------------------------------------------------------------------- AC1


async def test_dropped_utterance_becomes_one_queue_item_via_the_generic_enqueue_path(
    tmp_path: Path,
) -> None:
    audio = tmp_path / "note.wav"
    audio.write_bytes(b"utterance-bytes")
    expected_event_key = hashlib.sha256(b"utterance-bytes").hexdigest()

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
        # A second poll tick has time to run too — the file was already moved, so nothing new
        # should land (proven inline here rather than as a separate registry-driven test).
        await asyncio.sleep(3 * source.poll_interval_seconds)
    finally:
        await registry.stop()

    assert len(enqueuer.items) == 1
    item = enqueuer.items[0]
    assert item.idempotency_key == derive_key("asr", expected_event_key)
    assert item.payload["transcript"] == "buy milk tomorrow"

    # No asr-specific dispatch: the registry enqueues every source through ONE generic call
    # site (registry.py's own poll loop) — "asr" never appears in that module's source at all.
    assert "asr" not in inspect.getsource(registry_module)


async def test_second_poll_after_move_yields_no_new_queue_item(tmp_path: Path) -> None:
    (tmp_path / "note.wav").write_bytes(b"second-poll-bytes")
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


# --------------------------------------------------------------------------------------- AC2


def test_asr_absent_when_drop_dir_unset_and_other_functionality_is_unaffected(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC2 (lesion), part 1: WOMBAT_ASR_DROP_DIR unset -> 'asr' never lands in source_ids and
    exactly one loud skip log names it — mirroring the Q-67 loud-skip pattern gcal/gmail/
    feedback already follow."""
    config = _make_config()

    with caplog.at_level(logging.WARNING):
        registry = build_source_registry(config, _FakeEnqueuer(), tz=_TZ, clock=_clock)

    assert "asr" not in registry.source_ids
    asr_skip_records = [r for r in caplog.records if "WOMBAT_ASR_DROP_DIR" in r.getMessage()]
    assert len(asr_skip_records) == 1


async def test_asr_absent_calendar_shaped_item_still_drains_without_error() -> None:
    """AC2 (lesion), part 2: with ASR entirely unregistered, a calendar-shaped QueueItem still
    drains cleanly through DrainQueueStage (the SAME no-Postgres pure-stage fixture shape
    tests/unit/test_drain_queue_stage.py uses) — nothing raises on missing ASR."""

    @dataclass
    class _FakeQueue:
        canned: list[QueueItem]

        def drain(self, limit: int | None = None) -> list[QueueItem]:
            return self.canned

    calendar_item = QueueItem(
        idempotency_key="cal-1",
        payload={"event_class": "calendar_conflict", "summary": "Team sync"},
        item_id=1,
    )
    queue = _FakeQueue(canned=[calendar_item])
    stage = DrainQueueStage(queue, batch_size=1, poll_interval_seconds=5.0)
    ctx = StageContextFake(now_fn=_clock)

    result = await stage.run(ctx)  # must not raise

    assert isinstance(result, Transition)
    assert result.to == "gate"
    assert queue_items_from_artifact_data(result.output.data) == [calendar_item]


# --------------------------------------------------------------------------------------- AC3


async def test_one_failing_file_moves_to_failed_the_other_still_emits_and_poll_never_raises(
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
        events = await source.poll()  # must not raise

    assert len(events) == 1
    assert events[0].payload["transcript"] == "ok transcript"
    assert (tmp_path / "processed" / "good.wav").exists()
    assert (tmp_path / "failed" / "bad.wav").exists()
    assert "bad.wav" in caplog.text
