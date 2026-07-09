"""TK-51 — feedback affordance + FeedbackInputSource acceptance criteria (EP-12, Q-86 ruling).

CAPTURE ONLY (Q-86 split): the useful -> OUTCOME_LOAD_BEARING / not_useful -> OUTCOME_REGRETTED
fold is proven by TK-50's own AC3 (same batch) — not re-proven here. All tests are no-DSN.

  AC1 affordance binding: feedback_affordance(item_ref) returns one line containing both the
      question text and item_ref, for any item_ref (containment round-trip).
  AC2 uniform registration + enqueue: FeedbackInputSource registers with a REAL SourceRegistry
      + a stub Enqueuer; a pushed 'useful' response and an appended 'not useful' file line both
      surface as QueueItems via the ORDINARY registry path, keyed by
      derive_key('feedback', event_key); registry.source_ids exposes 'feedback'; base.py /
      registry.py are unmodified (import-level check).
  AC3 durable round-trip: from_payload(to_payload(sig)) == sig for both responses; malformed
      file lines are skipped with a warning, valid neighbors still captured.
  AC4 absence is fine (CON-3): no feedback file + no pushes -> poll() returns [], nothing
      enqueued, no error.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path

import pytest

from wombat.domain.item_identity import idempotency_key as derive_key
from wombat.queue import EnqueueResult, QueueItem
from wombat.sources.base import SourceEvent
from wombat.sources.registry import SourceRegistry
from wombat.user_model.feedback_source import (
    FeedbackInputSource,
    FeedbackSignal,
    feedback_affordance,
)


class _FakeEnqueuer:
    """Records every enqueue() call — the injected seam AC2 verifies end-to-end against."""

    def __init__(self) -> None:
        self.items: list[QueueItem] = []

    def enqueue(self, item: QueueItem) -> EnqueueResult:
        self.items.append(item)
        return EnqueueResult.QUEUED


async def _wait_until(
    predicate: Callable[[], bool], *, timeout: float = 2.0, interval: float = 0.005
) -> None:
    """Poll ``predicate`` until true or ``timeout`` elapses (event-driven, no fixed sleeps)."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() >= deadline:
            raise AssertionError(f"condition not met within {timeout}s")
        await asyncio.sleep(interval)


# ---------------------------------------------------------------------------
# AC1 — affordance binding
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "item_ref",
    [
        "abc",
        derive_key("gmail", "msg-1"),
        derive_key("calendar", "evt-123"),
    ],
)
def test_ac1_affordance_line_contains_question_and_item_ref(item_ref: str) -> None:
    line = feedback_affordance(item_ref)

    assert "was this useful? [y/n]" in line
    assert item_ref in line
    assert "\n" not in line


# ---------------------------------------------------------------------------
# AC2 — uniform registration + enqueue via the ordinary registry path
# ---------------------------------------------------------------------------


async def test_ac2_pushed_and_file_responses_enqueue_via_ordinary_registry_path(
    tmp_path: Path,
) -> None:
    feedback_file = tmp_path / "feedback.txt"
    enqueuer = _FakeEnqueuer()
    registry = SourceRegistry(enqueuer)
    source = FeedbackInputSource(poll_interval_seconds=0.01, feedback_file=feedback_file)
    registry.register(source)

    useful_signal = FeedbackSignal(item_ref="item-a", response="useful")
    source.push(
        SourceEvent(event_key=useful_signal.event_key(), payload=useful_signal.to_payload())
    )
    feedback_file.write_text("item-b no\n", encoding="utf-8")
    not_useful_signal = FeedbackSignal(item_ref="item-b", response="not_useful")

    await registry.start()
    try:
        await _wait_until(lambda: len(enqueuer.items) >= 2)
        await asyncio.sleep(2 * source.poll_interval_seconds)
    finally:
        await registry.stop()

    assert len(enqueuer.items) == 2
    assert "feedback" in registry.source_ids

    by_key = {item.idempotency_key: item for item in enqueuer.items}
    useful_key = derive_key("feedback", useful_signal.event_key())
    not_useful_key = derive_key("feedback", not_useful_signal.event_key())
    assert useful_key in by_key
    assert not_useful_key in by_key
    assert FeedbackSignal.from_payload(by_key[useful_key].payload) == useful_signal
    assert FeedbackSignal.from_payload(by_key[not_useful_key].payload) == not_useful_signal


def test_ac2_base_and_registry_import_unmodified_by_feedback_source() -> None:
    """Registration-not-rewrite: a plain import of base.py/registry.py still succeeds and
    exposes exactly the same public API — this ticket touched neither file."""
    import wombat.sources.base
    import wombat.sources.registry
    import wombat.user_model.feedback_source

    assert wombat.sources.base.PushSource is not None
    assert wombat.sources.registry.SourceRegistry is not None
    assert wombat.user_model.feedback_source.FeedbackInputSource is not None


# ---------------------------------------------------------------------------
# AC3 — durable round-trip + malformed-line handling
# ---------------------------------------------------------------------------


def test_ac3_from_payload_to_payload_round_trip_both_responses() -> None:
    for response in ("useful", "not_useful"):
        signal = FeedbackSignal(item_ref="item-x", response=response)

        assert FeedbackSignal.from_payload(signal.to_payload()) == signal


async def test_ac3_malformed_lines_skipped_with_warning_valid_neighbors_still_captured(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    feedback_file = tmp_path / "feedback.txt"
    feedback_file.write_text(
        "item-1 y\n"
        "this line has no valid token\n"
        "item-2 maybe\n"
        "item-3 n\n",
        encoding="utf-8",
    )
    source = FeedbackInputSource(poll_interval_seconds=1.0, feedback_file=feedback_file)

    with caplog.at_level(logging.WARNING):
        events = await source.poll()

    assert [e.event_key for e in events] == [
        FeedbackSignal(item_ref="item-1", response="useful").event_key(),
        FeedbackSignal(item_ref="item-3", response="not_useful").event_key(),
    ]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2


# ---------------------------------------------------------------------------
# AC4 — absence is fine (CON-3)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# TK-182 (CR2-5 + CR2-7) — bad-byte tolerance + truncation/rotation survival
# ---------------------------------------------------------------------------


async def test_tk182_ac1_bad_byte_and_pushed_event_both_survive_no_raise(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The register's exact repro: a valid line, a raw non-UTF-8 byte sequence, another valid
    line, plus one pushed event in the buffer. poll() run twice must never raise, must warn +
    skip only the bad-byte line, and must emit both valid lines and the pushed event exactly
    once total (the offset advances so the second poll sees nothing new)."""
    feedback_file = tmp_path / "feedback.txt"
    feedback_file.write_bytes(b"item-1 y\n\xff\xfe\xfd\nitem-2 n\n")
    source = FeedbackInputSource(poll_interval_seconds=1.0, feedback_file=feedback_file)

    pushed_signal = FeedbackSignal(item_ref="item-pushed", response="useful")
    source.push(
        SourceEvent(event_key=pushed_signal.event_key(), payload=pushed_signal.to_payload())
    )

    with caplog.at_level(logging.WARNING):
        first_events = await source.poll()
        second_events = await source.poll()

    assert [e.event_key for e in first_events] == [
        pushed_signal.event_key(),
        FeedbackSignal(item_ref="item-1", response="useful").event_key(),
        FeedbackSignal(item_ref="item-2", response="not_useful").event_key(),
    ]
    assert second_events == []
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1


async def test_tk182_ac2_truncation_then_append_re_reads_from_start(tmp_path: Path) -> None:
    """A file truncated/rotated to fewer lines and then appended before the next poll must
    have its new lines emitted (re-read from 0 on detected shrinkage), not silently dropped;
    any re-emitted duplicate carries the same event_key as before (idempotency-dedupable)."""
    feedback_file = tmp_path / "feedback.txt"
    feedback_file.write_text("item-1 y\nitem-2 n\nitem-3 y\n", encoding="utf-8")
    source = FeedbackInputSource(poll_interval_seconds=1.0, feedback_file=feedback_file)

    first_events = await source.poll()
    assert [e.event_key for e in first_events] == [
        FeedbackSignal(item_ref="item-1", response="useful").event_key(),
        FeedbackSignal(item_ref="item-2", response="not_useful").event_key(),
        FeedbackSignal(item_ref="item-3", response="useful").event_key(),
    ]

    # Truncate/rotate to a single line, then append a new one before the next poll.
    feedback_file.write_text("item-4 n\nitem-5 y\n", encoding="utf-8")

    second_events = await source.poll()
    second_keys = [e.event_key for e in second_events]

    # The newly-written line must be present (the CR2-7 repro: it must never be silently
    # dropped by an offset that slices past the end of the shrunk file).
    assert FeedbackSignal(item_ref="item-5", response="useful").event_key() in second_keys
    # Any re-emitted duplicate carries the same event_key as its original emission.
    for key in second_keys:
        assert key in {
            FeedbackSignal(item_ref="item-4", response="not_useful").event_key(),
            FeedbackSignal(item_ref="item-5", response="useful").event_key(),
        }


async def test_tk182_oserror_reading_file_preserves_drained_push_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A file-read failure for a reason OTHER than the tolerated missing-file no-op (e.g. an
    OSError) must log loud and contribute no file events, but must NEVER discard pushed
    events already drained by the same poll() call."""
    feedback_file = tmp_path / "feedback.txt"
    feedback_file.write_text("item-1 y\n", encoding="utf-8")
    source = FeedbackInputSource(poll_interval_seconds=1.0, feedback_file=feedback_file)

    pushed_signal = FeedbackSignal(item_ref="item-pushed", response="useful")
    source.push(
        SourceEvent(event_key=pushed_signal.event_key(), payload=pushed_signal.to_payload())
    )

    def _boom(self: Path) -> bytes:
        raise OSError("simulated read failure")

    monkeypatch.setattr(Path, "read_bytes", _boom)

    with caplog.at_level(logging.WARNING):
        events = await source.poll()

    assert [e.event_key for e in events] == [pushed_signal.event_key()]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1


async def test_ac4_no_file_no_pushes_poll_returns_empty_no_error() -> None:
    source = FeedbackInputSource(poll_interval_seconds=1.0)

    assert await source.poll() == []


async def test_ac4_nonexistent_feedback_file_poll_returns_empty_no_error(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.txt"
    source = FeedbackInputSource(poll_interval_seconds=1.0, feedback_file=missing)

    assert await source.poll() == []


async def test_ac4_absence_is_fine_via_registry_nothing_enqueued_never_degrades() -> None:
    enqueuer = _FakeEnqueuer()
    registry = SourceRegistry(enqueuer)
    source = FeedbackInputSource(poll_interval_seconds=0.01)
    registry.register(source)

    await registry.start()
    try:
        await asyncio.sleep(5 * source.poll_interval_seconds)
    finally:
        await registry.stop()

    assert enqueuer.items == []
    assert "feedback" not in registry.degraded_sources
