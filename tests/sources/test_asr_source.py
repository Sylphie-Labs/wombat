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

TK-212 (EP-34, DEC-35 + DEC-37(f)) acceptance criteria — pre-queue persona-command interception:

AC1 (matched command consumed): a fake transcriber returns "be warmer" for a dropped file; after
``poll()``, a real ``LivePersona`` carries the stepped matrix (``warmth`` moved up one level), a
recording fake ``speak`` received EXACTLY ONE ack string equal to the pinned template, ``caplog``
shows the trail line strictly BEFORE ``LivePersona.set`` is called (order-assert), NOTHING was
emitted as a ``SourceEvent``, and the file sits in ``processed/``.

AC2 (byte-identical unmatched path): a non-command transcript, even WITH a ``command_hook``
wired, yields a ``SourceEvent`` whose payload is an explicit equality pin against pre-ticket
behavior — proving the hook is a pure pass-through for anything the grammar doesn't match.

AC3 (degrade): ``LivePersona.set`` raising inside the hook logs ONE loud WARNING, still consumes
the command (no ``SourceEvent``, file in ``processed/``), and the source keeps processing
subsequent files; a raising ``speak`` degrades loud the same way, without blocking the apply.

TK-280 (DEC-60c server half, EP-32) acceptance criteria — the ``turn_hook`` seam:

AC3: ``turn_hook`` fires with ``(event_key, transcript, captured_at)`` for a non-command drop;
``turn_hook=None`` (the default) is byte-identical; a command-consumed utterance never fires it.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import shutil
import threading
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
from wombat.persona.live import LivePersona
from wombat.persona.matrix import DEFAULT_MATRIX, Warmth
from wombat.queue import EnqueueResult, QueueItem
from wombat.sources.asr import ASRSource
from wombat.sources.bootstrap import build_source_registry, make_persona_command_hook
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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """AC2 (lesion), part 1: WOMBAT_ASR_DROP_DIR unset -> 'asr' never lands in source_ids and
    exactly one loud skip log names it — mirroring the Q-67 loud-skip pattern gcal/gmail/
    feedback already follow."""
    # TK-202 (Q-103): chdir off the repo root so a populated operator .env can't leak
    # WOMBAT_ASR_DROP_DIR in from the FILE source out from under "unset" (pydantic-settings
    # resolves env_file=".env" relative to CWD — mirrors TK-186's precedent).
    monkeypatch.chdir(tmp_path)
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


# ------------------------------------------------------------------------------- TK-282: AC2/AC3


class _ThreadRecordingTranscriber:
    """Records the ident of the thread ``transcribe`` actually executes on — the LOAD-BEARING
    proof that ``asyncio.to_thread`` really moves the decode off the event loop (TK-282)."""

    def __init__(self, text: str = "hello wombat") -> None:
        self.text = text
        self.transcribe_thread_idents: list[int] = []

    def transcribe(self, path: Path) -> str:
        self.transcribe_thread_idents.append(threading.get_ident())
        return self.text


async def test_transcribe_runs_off_loop_thread_while_hooks_and_moves_stay_on_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TK-282 (DEC-60d) AC2: over two dropped files, ``transcriber.transcribe`` runs on a thread
    OTHER than the event-loop thread, while ``command_hook``/``feedback_hook``/the file move all
    run ON the loop thread — and both files keep today's success/move semantics."""
    (tmp_path / "a.wav").write_bytes(b"a-bytes")
    (tmp_path / "b.wav").write_bytes(b"b-bytes")
    loop_thread_ident = threading.get_ident()
    transcriber = _ThreadRecordingTranscriber()

    command_hook_threads: list[int] = []

    def command_hook(transcript: str) -> bool:
        command_hook_threads.append(threading.get_ident())
        return False  # never consume -- both files must still enqueue

    feedback_hook_threads: list[int] = []

    def feedback_hook(transcript: str, event_key: str) -> None:
        feedback_hook_threads.append(threading.get_ident())

    move_threads: list[int] = []
    real_move = shutil.move

    def _spy_move(src: str, dst: str) -> str:
        move_threads.append(threading.get_ident())
        return real_move(src, dst)

    monkeypatch.setattr(shutil, "move", _spy_move)

    source = ASRSource(
        drop_dir=tmp_path,
        transcriber=transcriber,
        poll_interval_seconds=99.0,
        clock=_clock,
        command_hook=command_hook,
        feedback_hook=feedback_hook,
    )

    events = await source.poll()  # must not raise

    assert len(events) == 2
    assert len(transcriber.transcribe_thread_idents) == 2
    for ident in transcriber.transcribe_thread_idents:
        assert ident != loop_thread_ident  # OFF the loop thread (asyncio.to_thread)
    assert command_hook_threads == [loop_thread_ident, loop_thread_ident]  # ON the loop thread
    assert feedback_hook_threads == [loop_thread_ident, loop_thread_ident]  # ON the loop thread
    assert move_threads == [loop_thread_ident, loop_thread_ident]  # ON the loop thread
    assert (tmp_path / "processed" / "a.wav").exists()
    assert (tmp_path / "processed" / "b.wav").exists()


async def test_empty_poll_logs_nothing_at_info(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """TK-282 (DEC-60d) AC3: an empty scan (no candidate files) logs ZERO INFO lines — the 2.0s
    poll beat must never spam."""
    source = ASRSource(
        drop_dir=tmp_path,
        transcriber=_FakeTranscriber(),
        poll_interval_seconds=99.0,
        clock=_clock,
    )

    with caplog.at_level(logging.INFO):
        events = await source.poll()

    assert events == []
    assert [r for r in caplog.records if r.levelno == logging.INFO] == []


async def test_nonempty_poll_logs_one_found_count_line_and_one_per_file_outcome_line(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """TK-282 (DEC-60d) AC3: N>0 candidates -> exactly one found-count INFO line naming the
    count, plus exactly one per-file outcome INFO line naming the event_key prefix, the
    processed/failed outcome, and the transcribe duration."""
    (tmp_path / "note.wav").write_bytes(b"note-bytes")
    expected_event_key = hashlib.sha256(b"note-bytes").hexdigest()

    source = ASRSource(
        drop_dir=tmp_path,
        transcriber=_FakeTranscriber("hi"),
        poll_interval_seconds=99.0,
        clock=_clock,
    )

    with caplog.at_level(logging.INFO):
        events = await source.poll()

    assert len(events) == 1
    info_records = [r for r in caplog.records if r.levelno == logging.INFO]
    assert len(info_records) == 2
    found_records = [r for r in info_records if "found 1 candidate" in r.getMessage()]
    assert len(found_records) == 1
    outcome_records = [
        r
        for r in info_records
        if expected_event_key[:12] in r.getMessage() and "processed" in r.getMessage()
    ]
    assert len(outcome_records) == 1


# --------------------------------------------------------------------------------- TK-212: AC1


def _live_persona() -> LivePersona:
    return LivePersona(DEFAULT_MATRIX, "Steward")  # store-less (TK-243), fully in-memory


class _RecordingSpeak:
    def __init__(self) -> None:
        self.said: list[str] = []

    def __call__(self, text: str) -> None:
        self.said.append(text)


async def test_matched_command_is_consumed_never_enqueued_stepped_matrix_one_ack_trail_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    drop_dir = tmp_path / "drop"
    drop_dir.mkdir()
    (drop_dir / "note.wav").write_bytes(b"be-warmer-bytes")

    live_persona = _live_persona()
    caplog.clear()  # drop the store-less construction warning -- irrelevant to this order-assert
    speak = _RecordingSpeak()
    hook = make_persona_command_hook(live_persona, speak)

    # Order-assert: wrap live_persona.set so we can confirm the trail log already landed BEFORE
    # the apply lands (the hook is documented to log strictly before calling set()).
    log_count_at_set: list[int] = []
    real_set = live_persona.set

    def _spy_set(matrix: object) -> None:
        log_count_at_set.append(len(caplog.records))
        real_set(matrix)  # type: ignore[arg-type]

    monkeypatch.setattr(live_persona, "set", _spy_set)
    source = ASRSource(
        drop_dir=drop_dir,
        transcriber=_FakeTranscriber("be warmer"),
        poll_interval_seconds=99.0,
        clock=_clock,
        command_hook=hook,
    )

    with caplog.at_level(logging.INFO):
        events = await source.poll()  # must not raise

    assert events == []  # NOTHING emitted as a SourceEvent
    assert live_persona.matrix.warmth == Warmth.NEUTRAL  # stepped +1 from the default RESERVED
    assert speak.said == ["Warmth is now neutral."]  # exactly one pinned ack
    assert (drop_dir / "processed" / "note.wav").exists()
    assert not (drop_dir / "failed" / "note.wav").exists()

    # TK-282 (DEC-60d): poll() ALSO logs its own found-count + per-file-outcome INFO lines now —
    # isolate the persona-command hook's own trail line from those by content, not by "the only
    # INFO record", to keep this order-assert scoped to what the hook itself is documented to do.
    info_records = [r for r in caplog.records if r.levelno == logging.INFO]
    trail_records = [r for r in info_records if "be warmer" in r.getMessage()]
    assert len(trail_records) == 1
    # The trail line was ALREADY emitted by the time set() ran; only the per-file outcome line
    # (TK-282) logs AFTER, once the command has been consumed and the file moved.
    assert log_count_at_set == [len(info_records) - 1]


async def test_matched_reset_command_uses_the_fixed_reset_ack_template(tmp_path: Path) -> None:
    drop_dir = tmp_path / "drop"
    drop_dir.mkdir()
    live_persona = _live_persona()
    speak = _RecordingSpeak()
    hook = make_persona_command_hook(live_persona, speak)
    (drop_dir / "note.wav").write_bytes(b"reset-bytes")

    source = ASRSource(
        drop_dir=drop_dir,
        transcriber=_FakeTranscriber("reset persona"),
        poll_interval_seconds=99.0,
        clock=_clock,
        command_hook=hook,
    )

    events = await source.poll()

    assert events == []
    assert live_persona.matrix == DEFAULT_MATRIX
    assert speak.said == ["Persona reset to defaults."]


# --------------------------------------------------------------------------------- TK-212: AC2


async def test_unmatched_transcript_yields_a_byte_identical_source_event_even_with_hook_wired(
    tmp_path: Path,
) -> None:
    """AC2: a command_hook is wired, but the transcript doesn't match the grammar — the
    SourceEvent payload is pinned byte-identical to pre-ticket (no command_hook) behavior aside
    from TK-278's item_kind/voice_turn stamp (DEC-60a), which is unrelated to command_hook."""
    drop_dir = tmp_path / "drop"
    drop_dir.mkdir()
    live_persona = _live_persona()
    hook = make_persona_command_hook(live_persona, speak=None)
    audio_bytes = b"unmatched-utterance-bytes"
    (drop_dir / "note.wav").write_bytes(audio_bytes)
    expected_event_key = hashlib.sha256(audio_bytes).hexdigest()

    source = ASRSource(
        drop_dir=drop_dir,
        transcriber=_FakeTranscriber("buy milk tomorrow"),
        poll_interval_seconds=99.0,
        clock=_clock,
        command_hook=hook,
    )

    events = await source.poll()

    assert len(events) == 1
    assert events[0].event_key == expected_event_key
    assert events[0].payload == {
        "item_kind": "chat",
        "voice_turn": True,
        "transcript": "buy milk tomorrow",
        "captured_at": _NOW.isoformat(),
    }  # explicit equality pin — byte-identical to pre-TK-212 behavior plus TK-278's stamp
    assert live_persona.matrix == DEFAULT_MATRIX  # untouched — never a persona-command match
    assert (drop_dir / "processed" / "note.wav").exists()


# --------------------------------------------------------------------------------- TK-212: AC3


async def test_live_persona_set_raising_still_consumes_logs_loud_and_keeps_processing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    drop_dir = tmp_path / "drop"
    drop_dir.mkdir()
    live_persona = _live_persona()
    caplog.clear()  # drop the store-less construction-time warning -- this test counts set()'s own
    speak = _RecordingSpeak()
    hook = make_persona_command_hook(live_persona, speak)

    def _boom(matrix: object) -> None:
        raise RuntimeError("simulated LivePersona.set failure")

    (drop_dir / "a-command.wav").write_bytes(b"warmer-bytes")
    (drop_dir / "b-plain.wav").write_bytes(b"plain-bytes")

    class _TwoFileTranscriber:
        def transcribe(self, path: Path) -> str:
            return "be warmer" if path.name == "a-command.wav" else "just a note"

    source = ASRSource(
        drop_dir=drop_dir,
        transcriber=_TwoFileTranscriber(),
        poll_interval_seconds=99.0,
        clock=_clock,
        command_hook=hook,
    )

    monkeypatch.setattr(live_persona, "set", _boom)
    with caplog.at_level(logging.WARNING):
        events = await source.poll()  # must not raise

    # The command file was still consumed (no SourceEvent, moved to processed/); the ack still
    # fired (the resulting/attempted level still acks); the plain file still enqueues normally —
    # one bad apply never blocks the rest of the poll.
    assert len(events) == 1
    assert events[0].payload["transcript"] == "just a note"
    assert (drop_dir / "processed" / "a-command.wav").exists()
    assert (drop_dir / "processed" / "b-plain.wav").exists()
    assert speak.said == ["Warmth is now neutral."]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "LivePersona.set" in warnings[0].getMessage()


async def test_speak_raising_degrades_loud_without_blocking_the_applied_persona_change(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    drop_dir = tmp_path / "drop"
    drop_dir.mkdir()
    live_persona = _live_persona()
    caplog.clear()  # drop the store-less construction warning -- this test counts the hook's own

    def _boom_speak(text: str) -> None:
        raise RuntimeError("simulated speak failure")

    hook = make_persona_command_hook(live_persona, _boom_speak)
    (drop_dir / "note.wav").write_bytes(b"warmer-bytes")

    source = ASRSource(
        drop_dir=drop_dir,
        transcriber=_FakeTranscriber("be warmer"),
        poll_interval_seconds=99.0,
        clock=_clock,
        command_hook=hook,
    )

    with caplog.at_level(logging.WARNING):
        events = await source.poll()  # must not raise

    assert events == []
    assert live_persona.matrix.warmth == Warmth.NEUTRAL  # the persona change still applied
    assert (drop_dir / "processed" / "note.wav").exists()
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "speak" in warnings[0].getMessage()


# ----------------------------------------------------------------------------------- TK-278: AC1


async def test_non_command_transcript_is_stamped_chat_and_feedback_hook_still_fires(
    tmp_path: Path,
) -> None:
    """TK-278 (DEC-60a): a non-command transcript's SourceEvent payload carries item_kind
    'chat' + voice_turn True alongside transcript/captured_at, AND feedback_hook still fires as
    a pure side effect (its own return value never changes what is emitted)."""
    drop_dir = tmp_path / "drop"
    drop_dir.mkdir()
    audio_bytes = b"chat-stamp-bytes"
    (drop_dir / "note.wav").write_bytes(audio_bytes)
    expected_event_key = hashlib.sha256(audio_bytes).hexdigest()

    feedback_calls: list[tuple[str, str]] = []

    def feedback_hook(transcript: str, event_key: str) -> None:
        feedback_calls.append((transcript, event_key))

    source = ASRSource(
        drop_dir=drop_dir,
        transcriber=_FakeTranscriber("what's on my calendar"),
        poll_interval_seconds=99.0,
        clock=_clock,
        feedback_hook=feedback_hook,
    )

    events = await source.poll()

    assert len(events) == 1
    assert events[0].payload == {
        "item_kind": "chat",
        "voice_turn": True,
        "transcript": "what's on my calendar",
        "captured_at": _NOW.isoformat(),
    }
    assert feedback_calls == [("what's on my calendar", expected_event_key)]


async def test_command_consumed_utterance_emits_no_event(tmp_path: Path) -> None:
    """TK-278 (DEC-60a): a command-consumed utterance still emits NO SourceEvent — the
    command_hook check runs BEFORE the (now item_kind-stamped) payload is ever built."""
    drop_dir = tmp_path / "drop"
    drop_dir.mkdir()
    (drop_dir / "note.wav").write_bytes(b"command-bytes")
    live_persona = _live_persona()
    speak = _RecordingSpeak()
    hook = make_persona_command_hook(live_persona, speak)

    source = ASRSource(
        drop_dir=drop_dir,
        transcriber=_FakeTranscriber("be warmer"),
        poll_interval_seconds=99.0,
        clock=_clock,
        command_hook=hook,
    )

    events = await source.poll()

    assert events == []
    assert (drop_dir / "processed" / "note.wav").exists()


# ----------------------------------------------------------------------------------- TK-280: AC3


async def test_turn_hook_fires_with_event_key_transcript_captured_at_for_a_non_command_drop(
    tmp_path: Path,
) -> None:
    drop_dir = tmp_path / "drop"
    drop_dir.mkdir()
    audio_bytes = b"turn-hook-bytes"
    (drop_dir / "note.wav").write_bytes(audio_bytes)
    expected_event_key = hashlib.sha256(audio_bytes).hexdigest()

    turn_calls: list[tuple[str, str, str]] = []

    def turn_hook(event_key: str, transcript: str, captured_at: str) -> None:
        turn_calls.append((event_key, transcript, captured_at))

    source = ASRSource(
        drop_dir=drop_dir,
        transcriber=_FakeTranscriber("what's the weather"),
        poll_interval_seconds=99.0,
        clock=_clock,
        turn_hook=turn_hook,
    )

    events = await source.poll()

    assert len(events) == 1
    assert turn_calls == [(expected_event_key, "what's the weather", _NOW.isoformat())]
    assert events[0].payload["captured_at"] == _NOW.isoformat()


async def test_turn_hook_none_leaves_the_source_event_byte_identical(tmp_path: Path) -> None:
    """AC3: turn_hook defaulting to None is a byte-identical boot -- the SAME payload as with a
    (no-op-recording) turn_hook wired, aside from the hook simply never firing."""
    drop_dir = tmp_path / "drop"
    drop_dir.mkdir()
    (drop_dir / "note.wav").write_bytes(b"no-hook-bytes")

    source = ASRSource(
        drop_dir=drop_dir,
        transcriber=_FakeTranscriber("what's the weather"),
        poll_interval_seconds=99.0,
        clock=_clock,
    )

    events = await source.poll()

    assert len(events) == 1
    assert events[0].payload == {
        "item_kind": "chat",
        "voice_turn": True,
        "transcript": "what's the weather",
        "captured_at": _NOW.isoformat(),
    }


async def test_command_consumed_utterance_never_fires_turn_hook(tmp_path: Path) -> None:
    """AC3: a command_hook-consumed utterance never reaches turn_hook -- the command_hook check
    runs first and returns before the turn_hook call site."""
    drop_dir = tmp_path / "drop"
    drop_dir.mkdir()
    (drop_dir / "note.wav").write_bytes(b"command-turn-bytes")
    live_persona = _live_persona()
    speak = _RecordingSpeak()
    command_hook = make_persona_command_hook(live_persona, speak)

    turn_calls: list[tuple[str, str, str]] = []

    def turn_hook(event_key: str, transcript: str, captured_at: str) -> None:
        turn_calls.append((event_key, transcript, captured_at))

    source = ASRSource(
        drop_dir=drop_dir,
        transcriber=_FakeTranscriber("be warmer"),
        poll_interval_seconds=99.0,
        clock=_clock,
        command_hook=command_hook,
        turn_hook=turn_hook,
    )

    events = await source.poll()

    assert events == []
    assert turn_calls == []
    assert (drop_dir / "processed" / "note.wav").exists()


# ----------------------------------------------------------------------------------- TK-289: AC1-5


async def test_context_hook_stamps_replying_to_alongside_the_four_built_ins(
    tmp_path: Path,
) -> None:
    """AC1: a context_hook returning {'replying_to': <text>} stamps that field onto the payload
    alongside the four built-ins, unchanged."""
    drop_dir = tmp_path / "drop"
    drop_dir.mkdir()
    (drop_dir / "note.wav").write_bytes(b"context-hook-bytes")

    source = ASRSource(
        drop_dir=drop_dir,
        transcriber=_FakeTranscriber("yes, do that"),
        poll_interval_seconds=99.0,
        clock=_clock,
        context_hook=lambda: {"replying_to": "Should I send the reply now?"},
    )

    events = await source.poll()

    assert len(events) == 1
    assert events[0].payload == {
        "item_kind": "chat",
        "voice_turn": True,
        "transcript": "yes, do that",
        "captured_at": _NOW.isoformat(),
        "replying_to": "Should I send the reply now?",
    }


async def test_context_hook_returning_empty_mapping_yields_no_replying_to_key(
    tmp_path: Path,
) -> None:
    """AC2 (the TTL-expired shape from the composition root's closure): a context_hook returning
    {} leaves the payload with NO replying_to key (absent, not empty)."""
    drop_dir = tmp_path / "drop"
    drop_dir.mkdir()
    (drop_dir / "note.wav").write_bytes(b"stale-context-bytes")

    source = ASRSource(
        drop_dir=drop_dir,
        transcriber=_FakeTranscriber("what's next"),
        poll_interval_seconds=99.0,
        clock=_clock,
        context_hook=lambda: {},
    )

    events = await source.poll()

    assert len(events) == 1
    assert "replying_to" not in events[0].payload
    assert events[0].payload == {
        "item_kind": "chat",
        "voice_turn": True,
        "transcript": "what's next",
        "captured_at": _NOW.isoformat(),
    }


async def test_context_hook_none_leaves_the_payload_byte_identical(tmp_path: Path) -> None:
    """AC3 (None case): context_hook defaulting to None builds a payload byte-identical to
    pre-TK-289 behavior."""
    drop_dir = tmp_path / "drop"
    drop_dir.mkdir()
    (drop_dir / "note.wav").write_bytes(b"no-context-hook-bytes")

    source = ASRSource(
        drop_dir=drop_dir,
        transcriber=_FakeTranscriber("what's the weather"),
        poll_interval_seconds=99.0,
        clock=_clock,
    )

    events = await source.poll()

    assert len(events) == 1
    assert events[0].payload == {
        "item_kind": "chat",
        "voice_turn": True,
        "transcript": "what's the weather",
        "captured_at": _NOW.isoformat(),
    }


async def test_command_consumed_utterance_never_invokes_context_hook(tmp_path: Path) -> None:
    """AC3 (command-consumed case): a matched persona command never invokes context_hook -- the
    command_hook early-return happens strictly before the payload-build site."""
    drop_dir = tmp_path / "drop"
    drop_dir.mkdir()
    (drop_dir / "note.wav").write_bytes(b"command-context-bytes")
    live_persona = _live_persona()
    speak = _RecordingSpeak()
    command_hook = make_persona_command_hook(live_persona, speak)

    context_calls: list[None] = []

    def context_hook() -> dict[str, str]:
        context_calls.append(None)
        return {"replying_to": "should never be reached"}

    source = ASRSource(
        drop_dir=drop_dir,
        transcriber=_FakeTranscriber("be warmer"),
        poll_interval_seconds=99.0,
        clock=_clock,
        command_hook=command_hook,
        context_hook=context_hook,
    )

    events = await source.poll()

    assert events == []
    assert context_calls == []
    assert (drop_dir / "processed" / "note.wav").exists()


async def test_raising_context_hook_logs_one_warning_moves_to_processed_unstamped(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """AC4: a raising context_hook logs ONE WARNING, the file still moves to processed/, and the
    SourceEvent carries the unstamped payload -- it must never kill the file or the poll."""
    drop_dir = tmp_path / "drop"
    drop_dir.mkdir()
    (drop_dir / "note.wav").write_bytes(b"raising-context-bytes")

    def boom() -> dict[str, str]:
        raise RuntimeError("simulated context_hook failure")

    source = ASRSource(
        drop_dir=drop_dir,
        transcriber=_FakeTranscriber("what's on my calendar"),
        poll_interval_seconds=99.0,
        clock=_clock,
        context_hook=boom,
    )

    with caplog.at_level(logging.WARNING):
        events = await source.poll()  # must not raise

    assert len(events) == 1
    assert events[0].payload == {
        "item_kind": "chat",
        "voice_turn": True,
        "transcript": "what's on my calendar",
        "captured_at": _NOW.isoformat(),
    }
    assert (drop_dir / "processed" / "note.wav").exists()
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "context_hook" in warnings[0].getMessage()


async def test_hostile_context_hook_reserved_key_overrides_are_ignored(tmp_path: Path) -> None:
    """AC5: a hostile context_hook returning overrides for the four reserved keys never wins --
    all four keep their built-in values."""
    drop_dir = tmp_path / "drop"
    drop_dir.mkdir()
    (drop_dir / "note.wav").write_bytes(b"hostile-context-bytes")

    def hostile() -> dict[str, str]:
        return {
            "item_kind": "draft",
            "voice_turn": "false",
            "transcript": "hijacked",
            "captured_at": "1970-01-01T00:00:00+00:00",
            "replying_to": "still allowed through",
        }

    source = ASRSource(
        drop_dir=drop_dir,
        transcriber=_FakeTranscriber("what's on my calendar"),
        poll_interval_seconds=99.0,
        clock=_clock,
        context_hook=hostile,
    )

    events = await source.poll()

    assert len(events) == 1
    assert events[0].payload == {
        "item_kind": "chat",
        "voice_turn": True,
        "transcript": "what's on my calendar",
        "captured_at": _NOW.isoformat(),
        "replying_to": "still allowed through",
    }
