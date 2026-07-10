"""TK-213 acceptance criteria — closed-lexicon persona feedback detection + recording (EP-35,
DEC-36/DEC-37(h), Q-112 pre-ruled).

AC1: for EVERY lexicon phrase, driving a fake-transcriber ``ASRSource`` poll with a matched
utterance records exactly one event carrying axis, direction, matched phrase, and timestamp —
parametrized over the whole lexicon with a spy recorder asserting the exact recorder args, plus
one ``WOMBAT_TEST_PG_DSN``-gated test writing through the real ``BehaviorEventLog`` and reading it
back via ``events_between`` (the ruled column mapping + upsert-on-re-drop). Also: the frozen
``FeedbackToken`` admits no motive field.

AC2: an ordinary utterance produces no recorder call and a byte-identical ``SourceEvent``; a
TK-211 COMMAND utterance is consumed and records NO feedback event; the lexicon and the TK-211
grammar are normalized-disjoint (no phrase is both).

AC3: a recorder that raises yields exactly one ``logger.warning``, the poll completes, and the
``SourceEvent`` still emits.
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import fields
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest

from wombat.behavior.event_log import BehaviorEventLog, ensure_schema
from wombat.domain.item_identity import idempotency_key as derive_key
from wombat.persona.commands import GRAMMAR
from wombat.persona.commands import _normalize as normalize_transcript
from wombat.persona.feedback import (
    FEEDBACK_LEXICON,
    FeedbackToken,
    detect_feedback_token,
    token_for_phrase,
)
from wombat.persona.live import LivePersona
from wombat.persona.matrix import DEFAULT_MATRIX
from wombat.sources.asr import ASRSource
from wombat.sources.bootstrap import make_persona_command_hook, make_persona_feedback_hook

_NOW = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)


def _clock() -> datetime:
    return _NOW


class _FakeTranscriber:
    def __init__(self, text: str) -> None:
        self.text = text

    def transcribe(self, path: Path) -> str:
        return self.text


class _SpyRecorder:
    def __init__(self, *, boom: bool = False) -> None:
        self.calls: list[tuple[FeedbackToken, str, datetime]] = []
        self._boom = boom

    def __call__(self, token: FeedbackToken, event_key: str, timestamp: datetime) -> None:
        self.calls.append((token, event_key, timestamp))
        if self._boom:
            raise RuntimeError("simulated recorder failure")


# --------------------------------------------------------------------------------------- AC1


def test_feedback_token_admits_no_motive_field() -> None:
    field_names = {f.name for f in fields(FeedbackToken)}
    assert field_names == {"axis", "direction", "phrase"}


@pytest.mark.parametrize("phrase,expected_token", FEEDBACK_LEXICON)
async def test_every_lexicon_phrase_records_exactly_one_event_with_only_the_ruled_fields(
    tmp_path: Path, phrase: str, expected_token: FeedbackToken
) -> None:
    drop_dir = tmp_path / "drop"
    drop_dir.mkdir()
    audio_bytes = f"audio-for-{phrase}".encode()
    (drop_dir / "note.wav").write_bytes(audio_bytes)
    expected_event_key = hashlib.sha256(audio_bytes).hexdigest()

    recorder = _SpyRecorder()
    hook = make_persona_feedback_hook(recorder, clock=_clock)
    source = ASRSource(
        drop_dir=drop_dir,
        transcriber=_FakeTranscriber(phrase),
        poll_interval_seconds=99.0,
        clock=_clock,
        feedback_hook=hook,
    )

    events = await source.poll()

    assert len(events) == 1  # the utterance still enqueues normally (never consumed)
    assert len(recorder.calls) == 1
    token, event_key, timestamp = recorder.calls[0]
    assert token == expected_token
    assert token.axis == expected_token.axis
    assert token.direction == expected_token.direction
    assert token.phrase == phrase
    assert event_key == expected_event_key
    assert timestamp == _NOW
    # detect_feedback_token/token_for_phrase agree on the SAME token (round-trip).
    assert detect_feedback_token(phrase) == expected_token
    assert token_for_phrase(expected_token.phrase) == expected_token


_DSN = os.environ.get("WOMBAT_TEST_PG_DSN")

_requires_pg = pytest.mark.skipif(
    not _DSN,
    reason=(
        "WOMBAT_TEST_PG_DSN is not set — skipping the TK-213 real-Postgres row-mapping proof. "
        "Start one with:\n"
        "  docker run --rm -d -p 5433:5432 -e POSTGRES_PASSWORD=wombat postgres:16\n"
        "then export WOMBAT_TEST_PG_DSN=postgresql://postgres:wombat@localhost:5433/postgres"
    ),
)


@pytest.fixture
def clean_table() -> None:
    assert _DSN is not None
    with psycopg.connect(_DSN) as conn:
        ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE wombat_behavior_events")
        conn.commit()


@_requires_pg
def test_pg_gated_ruled_column_mapping_and_upsert_on_re_drop(clean_table: None) -> None:
    """The Q-112(a) row encoding, proven against a real BehaviorEventLog: event_type=
    'persona_feedback', source_id='asr', outcome_label=the matched phrase verbatim,
    duration_seconds=None, idempotency_key=idempotency_key('persona_feedback', event_key). A
    re-drop of IDENTICAL bytes (same event_key) upserts the SAME row; a distinct recording
    (different event_key) of the SAME phrase stays a distinct row."""
    assert _DSN is not None
    store = BehaviorEventLog(_DSN)
    token = detect_feedback_token("too chatty")
    assert token is not None
    event_key = hashlib.sha256(b"first-recording").hexdigest()
    key = derive_key("persona_feedback", event_key)
    try:
        store.upsert(
            idempotency_key=key,
            event_type="persona_feedback",
            source_id="asr",
            timestamp_utc=_NOW,
            outcome_label=token.phrase,
            duration_seconds=None,
        )
        # A re-drop of the IDENTICAL bytes (same event_key) at a later timestamp upserts the
        # SAME row.
        store.upsert(
            idempotency_key=key,
            event_type="persona_feedback",
            source_id="asr",
            timestamp_utc=_NOW + timedelta(hours=1),
            outcome_label=token.phrase,
            duration_seconds=None,
        )
        # A DISTINCT recording of the SAME phrase (different event_key) is a distinct row.
        other_key = derive_key(
            "persona_feedback", hashlib.sha256(b"second-recording").hexdigest()
        )
        store.upsert(
            idempotency_key=other_key,
            event_type="persona_feedback",
            source_id="asr",
            timestamp_utc=_NOW + timedelta(hours=2),
            outcome_label=token.phrase,
            duration_seconds=None,
        )

        rows = store.events_between(_NOW - timedelta(days=1), _NOW + timedelta(days=1))
    finally:
        store.close()

    assert len(rows) == 2  # upsert-on-re-drop: the first two upserts collapsed to one row
    by_key = {row.idempotency_key: row for row in rows}
    first_row = by_key[key]
    assert first_row.event_type == "persona_feedback"
    assert first_row.source_id == "asr"
    assert first_row.outcome_label == "too chatty"
    assert first_row.duration_seconds is None
    assert first_row.timestamp_utc == _NOW + timedelta(hours=1)  # the SECOND upsert won
    assert by_key[other_key].idempotency_key == other_key


# --------------------------------------------------------------------------------------- AC2


async def test_ordinary_utterance_yields_no_recorder_call_and_a_byte_identical_source_event(
    tmp_path: Path,
) -> None:
    plain_dir = tmp_path / "plain"
    plain_dir.mkdir()
    hooked_dir = tmp_path / "hooked"
    hooked_dir.mkdir()
    audio_bytes = b"ordinary-utterance-bytes"
    (plain_dir / "note.wav").write_bytes(audio_bytes)
    (hooked_dir / "note.wav").write_bytes(audio_bytes)

    plain_source = ASRSource(
        drop_dir=plain_dir,
        transcriber=_FakeTranscriber("just a note about lunch"),
        poll_interval_seconds=99.0,
        clock=_clock,
    )
    recorder = _SpyRecorder()
    hooked_source = ASRSource(
        drop_dir=hooked_dir,
        transcriber=_FakeTranscriber("just a note about lunch"),
        poll_interval_seconds=99.0,
        clock=_clock,
        feedback_hook=make_persona_feedback_hook(recorder, clock=_clock),
    )

    plain_events = await plain_source.poll()
    hooked_events = await hooked_source.poll()

    assert recorder.calls == []
    assert len(plain_events) == 1
    assert len(hooked_events) == 1
    assert hooked_events[0].payload == plain_events[0].payload


async def test_a_command_utterance_is_consumed_and_records_no_feedback_event(
    tmp_path: Path,
) -> None:
    drop_dir = tmp_path / "drop"
    drop_dir.mkdir()

    live_persona = LivePersona(
        DEFAULT_MATRIX, "Steward", settings_path=str(tmp_path / "wombat.settings.json")
    )
    command_hook = make_persona_command_hook(live_persona, speak=None)
    recorder = _SpyRecorder()
    feedback_hook = make_persona_feedback_hook(recorder, clock=_clock)
    (drop_dir / "note.wav").write_bytes(b"command-bytes")

    source = ASRSource(
        drop_dir=drop_dir,
        transcriber=_FakeTranscriber("be more brief"),  # a TK-211 GRAMMAR command
        poll_interval_seconds=99.0,
        clock=_clock,
        command_hook=command_hook,
        feedback_hook=feedback_hook,
    )

    events = await source.poll()

    assert events == []  # consumed by the command hook
    assert recorder.calls == []  # feedback_hook never even ran (AC2)


def test_lexicon_and_grammar_are_normalized_disjoint() -> None:
    """No phrase belongs to both the TK-211 command grammar and the TK-213 feedback lexicon —
    observational phrasing (feedback) and imperative phrasing (commands) never collide."""
    normalized_grammar = {normalize_transcript(utterance) for utterance, _ in GRAMMAR}
    normalized_lexicon = {normalize_transcript(phrase) for phrase, _ in FEEDBACK_LEXICON}
    assert normalized_grammar & normalized_lexicon == set()


# --------------------------------------------------------------------------------------- AC3


async def test_raising_recorder_logs_exactly_one_warning_and_the_source_event_still_emits(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    drop_dir = tmp_path / "drop"
    drop_dir.mkdir()
    (drop_dir / "note.wav").write_bytes(b"boom-bytes")

    recorder = _SpyRecorder(boom=True)
    hook = make_persona_feedback_hook(recorder, clock=_clock)
    source = ASRSource(
        drop_dir=drop_dir,
        transcriber=_FakeTranscriber("too chatty"),
        poll_interval_seconds=99.0,
        clock=_clock,
        feedback_hook=hook,
    )

    with caplog.at_level(logging.WARNING):
        events = await source.poll()  # must not raise

    assert len(events) == 1  # the poll completes and the SourceEvent still emits
    assert events[0].payload["transcript"] == "too chatty"
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "recorder raised" in warnings[0].getMessage()
