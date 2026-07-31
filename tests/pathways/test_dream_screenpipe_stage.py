"""TK-324 — DreamScreenpipeStage acceptance criteria (EP-37, DEC-70h).

In-memory/monkeypatched substrate, ZERO real network beyond the deliberate degraded-client case:
mirrors ``tests/behavior/test_dream_facts.py``'s own idiom — ``user_facts`` is a REAL
``UserFactsStore`` instance over an unreachable DSN (lazy — never actually connects) with its
public methods monkeypatched to recording/canned/raising doubles; ``model`` is TK-8's
``FakeModel``; ``client`` is a scripted fake (mirrors ``tests/sources/test_screenpipe_source.py``'s
own windowed-fake-client convention) or, for the "degraded client" case, a REAL ``ScreenpipeClient``
pointed at a port nothing is listening on (mirrors that same test module's own AC(d) precedent —
its own documented never-raise degrade makes this fast and safe, no real screenpipe required).

  AC1 (RULING R-C): a fake client backing a rich 21-day timeline -> exactly ONE ``client.search``
      call per local day (21 total), exactly ONE model call whose user message is the capped
      projection (line count / total chars / per-line length all asserted, and the fold's own
      residency/title/daypart lines are exactly as expected), accepted proposals land as
      ``source='behavior'`` facts through the TK-294 seams.
  AC2 (CUSTODY VERBATIM from dream_facts): a model proposal with 7 valid facts, an over-long
      line, a forbidden-token line, and a duplicate -> at most 5 new facts land, all <=200 chars,
      the motive line is screened, the duplicate deduped, one INFO journal line per acceptance.
  AC3 (degrade, three separate runs): ``client=None`` -> zero client/model contact, inert
      transition; a genuinely degraded client (unreachable port) -> at least one WARNING (the
      client's own, DEC-70i), zero model calls, still transitions; zero timeline data (a healthy
      fake returning nothing) -> zero model calls, NO warning, still transitions. Every case:
      ``new_facts == 0`` and the stage still ``Transition``s to ``dream_behavior_log``.
  AC(fold): ``_build_projection``'s own caps (line count / total chars / per-line length) hold
      even when the raw fold would overflow them — a stable, deterministic prefix truncation,
      logged loud when it bites.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from cogworx.loop.result import Transition
from cogworx.model.base import ModelResponse

from tests.support.stage_context_fake import FakeModel, StageContextFake
from wombat.behavior.stages.dream_screenpipe import (
    _MAX_PROJECTION_CHARS,
    _MAX_PROJECTION_LINE_CHARS,
    _MAX_PROJECTION_LINES,
    DreamScreenpipeStage,
    _build_projection,
    _fact_key,
)
from wombat.integrations.screenpipe.client import ScreenpipeClient, ScreenpipeItem
from wombat.user_facts import UserFactsStore

_NOW = datetime(2026, 7, 30, 3, 0, 0, tzinfo=UTC)  # today_local (UTC) == 2026-07-30
_TZ = ZoneInfo("UTC")
_UNREACHABLE_DSN = "postgresql://nonexistent-host-should-never-be-dialed:1/db"
_RAW_OCR_MARKER = "SECRET raw OCR body that must never reach the model"


class _FakeClient:
    """A scripted fake ``ScreenpipeClient``-shaped double (mirrors ``tests/sources/
    test_screenpipe_source.py``'s own ``_WindowedFakeClient`` convention): ``search`` filters a
    fixed master list of ``ScreenpipeItem`` by ``[start, end)`` on ``captured_at``, and records
    every call's window so a test can assert the ONE-call-per-local-day shape."""

    def __init__(self, items: list[ScreenpipeItem]) -> None:
        self._items = items
        self.calls: list[tuple[datetime, datetime]] = []

    def search(
        self,
        start: datetime,
        end: datetime,
        *,
        app_name: str | None = None,
        limit: int | None = None,
    ) -> list[ScreenpipeItem]:
        self.calls.append((start, end))
        return [item for item in self._items if start <= item.captured_at < end]


def _item(app: str, title: str, day: date, hour: int) -> ScreenpipeItem:
    return ScreenpipeItem(
        app=app,
        title=title,
        text_snippet=_RAW_OCR_MARKER,
        captured_at=datetime(day.year, day.month, day.day, hour, 0, tzinfo=UTC),
        ref_id="ref",
    )


def _fake_user_facts(
    monkeypatch: pytest.MonkeyPatch,
    *,
    existing: dict[str, str] | None = None,
    raises_upsert_on_call: int | None = None,
) -> tuple[UserFactsStore, list[tuple[str, str, str]]]:
    """A stateful in-memory double — mirrors ``test_dream_facts.py``'s own fake exactly."""
    rows: dict[str, str] = dict(existing or {})
    calls: list[tuple[str, str, str]] = []
    call_index = {"n": 0}

    def _count(self: UserFactsStore) -> int:
        return len(rows)

    def _list_facts(self: UserFactsStore, limit: int) -> list[dict[str, Any]]:
        return [{"fact_key": key, "fact": text} for key, text in list(rows.items())[:limit]]

    def _upsert_fact(self: UserFactsStore, fact_key: str, fact: str, source: str) -> None:
        call_index["n"] += 1
        calls.append((fact_key, fact, source))
        if raises_upsert_on_call is not None and call_index["n"] == raises_upsert_on_call:
            raise RuntimeError(f"simulated upsert_fact failure on call {call_index['n']} — AC")
        rows[fact_key] = fact

    monkeypatch.setattr(UserFactsStore, "count", _count)
    monkeypatch.setattr(UserFactsStore, "list_facts", _list_facts)
    monkeypatch.setattr(UserFactsStore, "upsert_fact", _upsert_fact)
    return UserFactsStore(_UNREACHABLE_DSN), calls


def _rich_timeline_client() -> _FakeClient:
    """A 21-day timeline: Slack dominates mornings, VSCode dominates afternoons (both with a
    recurring title), Chrome shows up every third evening with a fresh title each time (never
    recurring) — exercises residency, recurring-title, AND daypart-regularity folding all at
    once, deterministically."""
    items: list[ScreenpipeItem] = []
    today = _NOW.astimezone(_TZ).date()
    for day_offset in range(21):
        day = today - timedelta(days=day_offset)
        items.append(_item("Slack", "general channel", day, 6))
        items.append(_item("VSCode", "main.py - myproject", day, 14))
        if day_offset % 3 == 0:
            items.append(_item("Chrome", f"Tab {day_offset}", day, 19))
    return _FakeClient(items)


# ================================================================================================
# AC1: rich timeline -> exactly one search call per local day, exactly one capped-projection model
# call, accepted proposals land as source='behavior' facts.
# ================================================================================================


async def test_ac1_rich_timeline_makes_one_search_per_day_and_one_capped_model_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _rich_timeline_client()
    user_facts, upsert_calls = _fake_user_facts(monkeypatch)

    raw_text = (
        "The user usually has Slack open in the morning.\n"
        "The user works in VSCode most afternoons.\n"
    )
    model = FakeModel(response=ModelResponse(text=raw_text, model_id="fake", finish_reason="stop"))

    stage = DreamScreenpipeStage(client=client, model=model, user_facts=user_facts, tz=_TZ)
    result = await stage.run(StageContextFake(now_fn=lambda: _NOW))

    # ONE client.search call per local day, 21 total (RULING R-C).
    assert len(client.calls) == 21
    for start, end in client.calls:
        assert end - start == timedelta(days=1)

    # Exactly ONE model call.
    assert len(model.calls) == 1
    messages = model.calls[0]
    assert messages[0].role == "system"
    assert messages[1].role == "user"
    projection_text = messages[1].content
    lines = projection_text.split("\n")

    # The pinned caps, asserted directly (AC1).
    assert len(lines) <= _MAX_PROJECTION_LINES
    assert len(projection_text) <= _MAX_PROJECTION_CHARS
    assert all(len(line) <= _MAX_PROJECTION_LINE_CHARS for line in lines)

    # The raw OCR body never reaches the model (DEC-70f).
    assert _RAW_OCR_MARKER not in projection_text

    # The fold's own expected content — residency (tie-broken alphabetically), recurring titles,
    # and daypart regularities.
    assert "Top app: Slack (21 captures)" in lines
    assert "Top app: VSCode (21 captures)" in lines
    assert "Top app: Chrome (7 captures)" in lines
    assert "Slack recurring title: general channel (21x)" in lines
    assert "VSCode recurring title: main.py - myproject (21x)" in lines
    assert "Mornings mostly Slack" in lines
    assert "Afternoons mostly VSCode" in lines
    assert "Evenings mostly Chrome" in lines

    assert isinstance(result, Transition)
    assert result.to == "dream_behavior_log"
    assert result.output.data == {"new_facts": 2}
    assert all(source == "behavior" for _key, _fact, source in upsert_calls)
    landed_facts = [fact for _key, fact, _source in upsert_calls]
    assert landed_facts == [
        "The user usually has Slack open in the morning.",
        "The user works in VSCode most afternoons.",
    ]


# ================================================================================================
# AC2 (CUSTODY VERBATIM from dream_facts): mixed proposal caps at 5 NEW facts; over-long/
# forbidden/duplicate each dropped loudly.
# ================================================================================================


async def test_ac2_mixed_proposal_lands_at_most_five_new_facts_dropping_the_rest_loudly(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # A minimal non-empty projection (one item) is enough to trigger the ONE model call — this
    # test's focus is the downstream custody filter, mirrored exactly from dream_facts's own AC1.
    client = _FakeClient([_item("Slack", "general", _NOW.date(), 6)])

    duplicate_text = "The user's dog is named Biscuit."
    seeded_key = _fact_key(duplicate_text)
    user_facts, upsert_calls = _fake_user_facts(monkeypatch, existing={seeded_key: duplicate_text})

    valid_lines = [
        "The user prefers tea over coffee.",
        "The user's sister is named Ana.",
        "The user works from a home office on Fridays.",
        "The user is training for a 10k in October.",
        "The user's favorite band is playing next month.",
        "The user just adopted a cat named Waffles.",
        "The user always jokes about Mondays being cursed.",
    ]
    over_long_line = "x" * 250
    forbidden_line = "This is a clinical observation about the user's disorder."
    duplicate_line = "the user's dog is named   biscuit."  # casefold/whitespace variant of seeded

    raw_text = "\n".join([duplicate_line, *valid_lines, over_long_line, forbidden_line])
    model = FakeModel(response=ModelResponse(text=raw_text, model_id="fake", finish_reason="stop"))

    stage = DreamScreenpipeStage(client=client, model=model, user_facts=user_facts, tz=_TZ)

    with caplog.at_level(logging.INFO, logger="wombat.behavior.stages.dream_screenpipe"):
        result = await stage.run(StageContextFake(now_fn=lambda: _NOW))

    assert isinstance(result, Transition)
    assert result.to == "dream_behavior_log"
    assert result.output.data == {"new_facts": 5}

    landed_facts = [fact for _key, fact, _source in upsert_calls]
    assert landed_facts == valid_lines[:5]
    assert all(source == "behavior" for _key, _fact, source in upsert_calls)
    assert all(len(fact) <= 200 for _key, fact, _source in upsert_calls)
    assert all(key == _fact_key(fact) for key, fact, _source in upsert_calls)

    warning_messages = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("over-long" in m for m in warning_messages)
    assert any("forbidden-token" in m for m in warning_messages)

    info_messages = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert any("duplicate" in m for m in info_messages)
    accepted_lines = [m for m in info_messages if "accepted new fact" in m]
    assert len(accepted_lines) == 5


# ================================================================================================
# AC3: three separate degrade shapes.
# ================================================================================================


async def test_ac3_client_none_is_structurally_inert(monkeypatch: pytest.MonkeyPatch) -> None:
    user_facts, upsert_calls = _fake_user_facts(monkeypatch)
    model = FakeModel(raises=AssertionError("a None client must never reach the mouth"))
    stage = DreamScreenpipeStage(client=None, model=model, user_facts=user_facts, tz=_TZ)

    result = await stage.run(StageContextFake(now_fn=lambda: _NOW))

    assert isinstance(result, Transition)
    assert result.to == "dream_behavior_log"
    assert result.output.data == {"new_facts": 0}
    assert model.calls == []
    assert upsert_calls == []


async def test_ac3_degraded_client_logs_one_warning_and_makes_zero_model_calls(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # A real ScreenpipeClient pointed at a port nothing listens on — its OWN documented degrade
    # contract (DEC-70i) makes search() return [] rather than raise, logging at most ONE WARNING
    # across the whole failure streak (mirrors test_screenpipe_source.py's own AC(d) precedent).
    degraded_client = ScreenpipeClient("http://127.0.0.1:59999")
    user_facts, upsert_calls = _fake_user_facts(monkeypatch)
    model = FakeModel(
        raises=AssertionError("a degraded client's empty projection must never reach the mouth")
    )
    stage = DreamScreenpipeStage(client=degraded_client, model=model, user_facts=user_facts, tz=_TZ)

    with caplog.at_level(logging.WARNING):
        result = await stage.run(StageContextFake(now_fn=lambda: _NOW))

    assert isinstance(result, Transition)
    assert result.to == "dream_behavior_log"
    assert result.output.data == {"new_facts": 0}
    assert model.calls == []
    assert upsert_calls == []
    assert any(r.levelno == logging.WARNING for r in caplog.records)


async def test_ac3_zero_timeline_data_makes_zero_model_calls_no_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    client = _FakeClient(items=[])  # a healthy client, simply nothing in the window
    user_facts, upsert_calls = _fake_user_facts(monkeypatch)
    model = FakeModel(raises=AssertionError("zero timeline data must never reach the mouth"))
    stage = DreamScreenpipeStage(client=client, model=model, user_facts=user_facts, tz=_TZ)

    with caplog.at_level(logging.WARNING, logger="wombat.behavior.stages.dream_screenpipe"):
        result = await stage.run(StageContextFake(now_fn=lambda: _NOW))

    assert isinstance(result, Transition)
    assert result.to == "dream_behavior_log"
    assert result.output.data == {"new_facts": 0}
    assert model.calls == []
    assert upsert_calls == []
    assert len(client.calls) == 21  # it DID attempt every day's read, unlike the None-client case
    assert not any(r.levelno == logging.WARNING for r in caplog.records)


# ================================================================================================
# AC(fold): _build_projection's own line-count/total-char/per-line caps hold under overflow.
# ================================================================================================


def test_build_projection_truncates_to_the_pinned_caps_deterministically(
    caplog: pytest.LogCaptureFixture,
) -> None:
    day = _NOW.astimezone(_TZ).date()
    items: list[ScreenpipeItem] = [
        # An app name alone longer than the per-line cap — proves the per-line clamp engages.
        ScreenpipeItem(
            app="A" * 200,
            title="",
            text_snippet="",
            captured_at=datetime(day.year, day.month, day.day, 6, 0, tzinfo=UTC),
            ref_id="r-long",
        ),
    ]
    # 40 distinct apps, one capture each — far more residency lines than _MAX_PROJECTION_LINES.
    for i in range(40):
        items.append(
            ScreenpipeItem(
                app=f"App{i:03d}",
                title="",
                text_snippet="",
                captured_at=datetime(day.year, day.month, day.day, 6, 0, tzinfo=UTC),
                ref_id=f"r{i}",
            )
        )

    with caplog.at_level(logging.WARNING, logger="wombat.behavior.stages.dream_screenpipe"):
        lines = _build_projection(items, _TZ)

    assert len(lines) <= _MAX_PROJECTION_LINES
    assert len(lines) == _MAX_PROJECTION_LINES  # confirms truncation actually engaged
    assert len("\n".join(lines)) <= _MAX_PROJECTION_CHARS
    assert all(len(line) <= _MAX_PROJECTION_LINE_CHARS for line in lines)
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_build_projection_is_empty_for_an_empty_timeline() -> None:
    assert _build_projection([], _TZ) == []


# ================================================================================================
# F4 (post-batch-review repair): the line-cap assertion must run on the ACTUAL RENDERED model
# user message, not just the returned line list — a hostile title carrying a newline/semicolon
# must not forge an extra line/field once the projection is actually joined and handed to the
# model.
# ================================================================================================


async def test_f4_rendered_prompt_never_gains_a_line_from_a_hostile_titles_embedded_newline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A title carrying an embedded newline and a semicolon must never forge an extra physical
    line (or a fake ``key: value``-looking field) in the RENDERED prompt text handed to the model
    — asserting only on the returned line LIST (as the prior fold-only test did) missed this
    defect class entirely, since sanitization must survive the ``\\n``.join(...) done in run()."""
    hostile_title = "line one\nline two; forged: field"
    client = _FakeClient(
        [
            _item("Slack", hostile_title, _NOW.date(), 6),
            _item("Slack", hostile_title, _NOW.date(), 7),
        ]
    )
    user_facts, _upsert_calls = _fake_user_facts(monkeypatch)
    model = FakeModel(response=ModelResponse(text="", model_id="fake", finish_reason="stop"))

    stage = DreamScreenpipeStage(client=client, model=model, user_facts=user_facts, tz=_TZ)
    await stage.run(StageContextFake(now_fn=lambda: _NOW))

    assert len(model.calls) == 1
    rendered_prompt = model.calls[0][1].content
    rendered_lines = rendered_prompt.splitlines()

    # Exactly the two conceptual projection lines this timeline supports (residency + recurring
    # title) — a hostile embedded newline must NOT add a third.
    assert len(rendered_lines) == 2
    assert rendered_lines[0] == "Top app: Slack (2 captures)"
    assert rendered_lines[1] == "Slack recurring title: line one line two, forged: field (2x)"

    # No interior newline/semicolon survived into the rendered text at all.
    assert ";" not in rendered_prompt
    assert len(rendered_prompt.splitlines()) <= _MAX_PROJECTION_LINES
