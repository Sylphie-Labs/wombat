"""TK-343 acceptance criteria — SealedUtteranceStore, BufferedUtteranceSink, and
UtteranceFetchHandler (DEC-79, wire-contract.md §5).

AC3: BufferedUtteranceSink driven through a REAL StreamingAudioWriter with a chunked, deliberately
torn-odd-byte fake stream — the sealed buffer equals the concatenated source PCM byte-for-byte and
no torn frame is ever submitted (frame discipline is INHERITED from StreamingAudioWriter, never
re-implemented here).

AC4: the single-slot TTL store — first fetch returns the full payload, an immediate repeat is
gone, and a separate one left to age past the TTL is gone too.

All PURE: no Postgres, no real network, no real audio hardware, no real clock (a fake float clock
only) — StreamingAudioWriter's own ``stream_factory`` injection point is what keeps this off real
sounddevice, exactly like ``tests/voice/test_stream_playback.py``.
"""

from __future__ import annotations

from wombat.voice.remote_sinks import (
    BufferedUtteranceSink,
    SealedUtterance,
    SealedUtteranceStore,
    UtteranceFetchHandler,
)
from wombat.voice.stream_playback import STREAM_SAMPLE_RATE, StreamingAudioWriter


class _FakeClock:
    def __init__(self, now: float = 0.0) -> None:
        self._now = now

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


class _RecordingSink:
    """Wraps a real ``BufferedUtteranceSink`` and records every raw ``write`` call it receives —
    a test-only spy over the SAME four-method AudioOutputStream shape, so ``StreamingAudioWriter``
    treats it identically to the sink alone."""

    def __init__(self, inner: BufferedUtteranceSink) -> None:
        self._inner = inner
        self.write_calls: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.write_calls.append(data)
        self._inner.write(data)

    def stop(self) -> None:
        self._inner.stop()

    def abort(self) -> None:
        self._inner.abort()

    def close(self) -> None:
        self._inner.close()


# --------------------------------------------------------------------------- SealedUtteranceStore


def test_take_is_none_before_any_publish() -> None:
    store = SealedUtteranceStore(clock=_FakeClock())
    assert store.take() is None


def test_publish_then_take_returns_the_utterance() -> None:
    clock = _FakeClock(now=100.0)
    store = SealedUtteranceStore(clock=clock)
    utterance = SealedUtterance(utterance_id="u-1", origin_device_id="watch-1", pcm=b"\x01\x02")

    store.publish(utterance)

    assert store.take() == utterance


def test_take_is_single_fetch_then_discard() -> None:
    """AC4: an immediate repeat finds nothing."""
    clock = _FakeClock(now=0.0)
    store = SealedUtteranceStore(clock=clock)
    utterance = SealedUtterance(utterance_id="u-1", origin_device_id="watch-1", pcm=b"\x01\x02")
    store.publish(utterance)

    first = store.take()
    second = store.take()

    assert first == utterance
    assert second is None


def test_unfetched_utterance_expires_at_the_ttl() -> None:
    """AC4: a separate utterance left to age past the pinned TTL is gone."""
    clock = _FakeClock(now=0.0)
    store = SealedUtteranceStore(clock=clock, ttl_seconds=120.0)
    store.publish(SealedUtterance(utterance_id="u-1", origin_device_id="watch-1", pcm=b"\x01\x02"))

    clock.advance(120.001)

    assert store.take() is None


def test_default_ttl_matches_the_devices_surface_pinned_constant() -> None:
    """DEC-83 §4/§5: 'devices read these windows off GET /v1/health rather than holding their own
    copy' — this store's DEFAULT ttl must be the SAME pinned constant, never a second literal."""
    from wombat.devices.surface import UTTERANCE_TTL_SECONDS

    clock = _FakeClock(now=0.0)
    store = SealedUtteranceStore(clock=clock)
    store.publish(SealedUtterance(utterance_id="u-1", origin_device_id="watch-1", pcm=b"\x01\x02"))

    clock.advance(UTTERANCE_TTL_SECONDS)
    assert store.take() is not None

    store.publish(SealedUtterance(utterance_id="u-2", origin_device_id="watch-1", pcm=b"\x01\x02"))
    clock.advance(UTTERANCE_TTL_SECONDS + 0.001)
    assert store.take() is None


def test_second_publish_replaces_an_unfetched_first() -> None:
    clock = _FakeClock(now=0.0)
    store = SealedUtteranceStore(clock=clock)
    store.publish(SealedUtterance(utterance_id="u-1", origin_device_id="watch-1", pcm=b"a"))
    store.publish(SealedUtterance(utterance_id="u-2", origin_device_id="watch-1", pcm=b"b"))

    assert store.take() == SealedUtterance(utterance_id="u-2", origin_device_id="watch-1", pcm=b"b")


# -------------------------------------------------------------------------- BufferedUtteranceSink


def test_write_then_stop_seals_and_publishes_the_accumulated_bytes() -> None:
    clock = _FakeClock(now=0.0)
    store = SealedUtteranceStore(clock=clock)
    sink = BufferedUtteranceSink(origin_device_id="watch-1", utterance_id="u-1", store=store)

    sink.write(b"ab")
    sink.write(b"cd")
    sink.stop()

    sealed = store.take()
    assert sealed == SealedUtterance(utterance_id="u-1", origin_device_id="watch-1", pcm=b"abcd")


def test_abort_discards_and_never_publishes() -> None:
    clock = _FakeClock(now=0.0)
    store = SealedUtteranceStore(clock=clock)
    sink = BufferedUtteranceSink(origin_device_id="watch-1", utterance_id="u-1", store=store)

    sink.write(b"partial-chunk")
    sink.abort()

    assert store.take() is None


def test_close_is_a_no_op_and_does_not_publish() -> None:
    clock = _FakeClock(now=0.0)
    store = SealedUtteranceStore(clock=clock)
    sink = BufferedUtteranceSink(origin_device_id="watch-1", utterance_id="u-1", store=store)

    sink.write(b"never-stopped")
    sink.close()

    assert store.take() is None


# ----------------------------------------------------------------- AC3: real StreamingAudioWriter


def test_ac3_real_writer_with_torn_chunks_seals_byte_for_byte_with_no_torn_write() -> None:
    """A chunked fake Fish stream whose individual chunks are deliberately torn (odd byte counts)
    drives a REAL StreamingAudioWriter over a BufferedUtteranceSink. StreamingAudioWriter's own
    carry-forward frame discipline must mean every single write() this sink ever receives is a
    whole number of frames (even byte count) — never re-derived here, only observed — and the
    final sealed buffer must equal the full 10-byte source PCM byte-for-byte."""
    source_pcm = b"ABCDEFGHIJ"  # 10 bytes -- even total, nothing trails unflushed
    chunks = [b"ABC", b"DE", b"FGHIJ"]  # 3 + 2 + 5 -- each individually odd-length ("torn")
    assert b"".join(chunks) == source_pcm

    clock = _FakeClock(now=0.0)
    store = SealedUtteranceStore(clock=clock)
    sink = BufferedUtteranceSink(origin_device_id="watch-1", utterance_id="u-1", store=store)
    recorder = _RecordingSink(sink)
    writer = StreamingAudioWriter(stream_factory=lambda: recorder)

    for chunk in chunks:
        writer.write(chunk)
    writer.finish()

    for submitted in recorder.write_calls:
        assert len(submitted) % 2 == 0, f"a torn (odd-byte) frame was submitted: {submitted!r}"

    sealed = store.take()
    assert sealed is not None
    assert sealed.pcm == source_pcm
    assert sealed.utterance_id == "u-1"
    assert sealed.origin_device_id == "watch-1"


def test_ac3_abort_mid_stream_discards_the_partial_buffer_via_a_real_writer() -> None:
    """The abort() half of the SAME real-writer wiring: a failure after some (but not all) chunks
    have been written must leave NOTHING sealed."""
    clock = _FakeClock(now=0.0)
    store = SealedUtteranceStore(clock=clock)
    sink = BufferedUtteranceSink(origin_device_id="watch-1", utterance_id="u-1", store=store)
    writer = StreamingAudioWriter(stream_factory=lambda: sink)

    writer.write(b"AB")
    writer.write(b"CD")
    writer.abort()

    assert store.take() is None


# -------------------------------------------------------------------------- UtteranceFetchHandler


async def test_handler_answers_204_when_the_store_is_empty() -> None:
    store = SealedUtteranceStore(clock=_FakeClock())
    handler = UtteranceFetchHandler(store=store)

    status, headers, body = await handler.handle()

    assert status == 204
    assert headers == {}
    assert body == b""


async def test_handler_answers_200_with_the_full_wire_header_set() -> None:
    clock = _FakeClock(now=0.0)
    store = SealedUtteranceStore(clock=clock)
    store.publish(
        SealedUtterance(utterance_id="u-1", origin_device_id="watch-1", pcm=b"\x01\x02\x03\x04")
    )
    handler = UtteranceFetchHandler(store=store)

    status, headers, body = await handler.handle()

    assert status == 200
    assert body == b"\x01\x02\x03\x04"
    assert headers["X-Wombat-Utterance-Id"] == "u-1"
    assert headers["X-Wombat-Origin-Device-Id"] == "watch-1"
    assert headers["X-Wombat-Sample-Rate-Hz"] == str(STREAM_SAMPLE_RATE)
    assert headers["X-Wombat-Audio-Format"] == "pcm_s16le"
    assert headers["X-Wombat-Channels"] == "1"


async def test_handler_is_single_fetch_then_discard() -> None:
    clock = _FakeClock(now=0.0)
    store = SealedUtteranceStore(clock=clock)
    store.publish(SealedUtterance(utterance_id="u-1", origin_device_id="watch-1", pcm=b"\x01"))
    handler = UtteranceFetchHandler(store=store)

    first_status, _first_headers, _first_body = await handler.handle()
    second_status, second_headers, second_body = await handler.handle()

    assert first_status == 200
    assert second_status == 204
    assert second_headers == {}
    assert second_body == b""


async def test_handler_origin_device_id_names_the_originating_device_not_the_fetcher() -> None:
    """AC9: a sealed utterance originating from the PHONE, fetched under the watch's routing
    fall-through — X-Wombat-Origin-Device-Id names the PHONE regardless of which device's
    authenticated token the GET actually rode in on (this handler is deliberately WHO-fetched-it
    agnostic; DeviceSurface authenticates the fetcher, this handler only ever reports who the
    reply was FOR)."""
    clock = _FakeClock(now=0.0)
    store = SealedUtteranceStore(clock=clock)
    store.publish(
        SealedUtterance(utterance_id="turn-utt-id", origin_device_id="phone-1", pcm=b"\x00\x01")
    )
    handler = UtteranceFetchHandler(store=store)

    status, headers, _body = await handler.handle()

    assert status == 200
    assert headers["X-Wombat-Origin-Device-Id"] == "phone-1"
    assert headers["X-Wombat-Utterance-Id"] == "turn-utt-id"
