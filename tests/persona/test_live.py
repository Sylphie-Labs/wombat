"""TK-209 — LivePersona acceptance criteria (EP-33, DEC-34 Jim authority + DEC-37(g)); storage
tier ported to Postgres by TK-243 (DEC-43).

  AC1 identity-through-reroute (the mouth-level byte-identity itself is TK-207's own test): a
      default-config LivePersona's instruction() matches the live oracles for all four mouths.
  AC2 hot-apply: set() swaps the in-memory matrix immediately — instruction() reflects it on the
      very next call, no restart — AND best-effort persists the five persona keys plus pins via
      SettingsStore.put (a key-level upsert — no read-modify-write, every unrelated row untouched).
  AC3 degrade: a persistence failure leaves the in-memory matrix applied, logs exactly ONE loud
      WARNING, and set() never raises; a store-less instance is fully in-memory (one loud warning
      at construction, never a crash).
  AC4 beat pickup: poll_settings() is fully lazy at construction (BINDING v2.61 ruling 2 — zero
      store I/O until the first beat); its FIRST call hydrates axes+pins from whatever the store
      holds; every later call is a value-diff no-op unless the six tracked keys changed, in which
      case it's an app edit — matrix swap + pin stamp + best-effort persist. A read/apply failure
      never raises, warning ONCE PER FAILURE STREAK (a success resets the guard).

TK-214 (DEC-36/DEC-37(h), Q-112(f)) pin mechanics:
  AC-pins-1: ``set()`` (default ``explicit=True``) stamps a pin for exactly the axes whose level
      CHANGED vs the pre-swap matrix; an axis whose value didn't change is never pinned.
  AC-pins-2: ``set(..., explicit=False)`` (the dream nudge path) stamps NOTHING, and — critically
      — a SUBSEQUENT poll never mistakes that nudge's own persisted write for an external app edit
      (the value-diff cursor advances past an own successful write, same precedent the old mtime
      cursor served).
  AC-pins-3: ``pinned_axes(now)`` returns axes stamped within the last ``PERSONA_PIN_DAYS`` days;
      an older stamp is excluded.
  AC-pins-4: a poll-detected value diff on a persona key is itself stamped as a pin and
      best-effort persisted.
  AC-pins-5: pins hydrate best-effort on the first poll — absent/malformed never raises, yields no
      pins.

AC1 (restart survival) and the app-edit-hot-apply-over-real-pg test are gated on
``WOMBAT_TEST_PG_DSN`` (the ``tests/unit/test_settings_store.py`` convention) — absent it, they
are skipped loudly. Every other test here uses in-memory ``SettingsStore`` subclass doubles (never
touching a real connection), or a store-less LivePersona.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest

from wombat.behavior.stages.reflection_compose import _SYSTEM_INSTRUCTION as REFLECTION_LIVE
from wombat.compose.brief_template import brief_system_instruction as brief_live
from wombat.integrations.gmail.draft_composer import _system_instruction as draft_live
from wombat.persona.builder import Mouth
from wombat.persona.live import PERSONA_PIN_DAYS, LivePersona
from wombat.persona.matrix import DEFAULT_MATRIX, Directness, Humor, PersonaMatrix, Warmth
from wombat.settings_store import SettingsStore, ensure_schema
from wombat.stages.compose import _system_instruction as compose_live

_DSN = os.environ.get("WOMBAT_TEST_PG_DSN")

_requires_pg = pytest.mark.skipif(
    not _DSN,
    reason=(
        "WOMBAT_TEST_PG_DSN is not set — skipping LivePersona DB tests that require a real "
        "throwaway Postgres. Start one with:\n"
        "  docker run --rm -d -p 5433:5432 -e POSTGRES_PASSWORD=wombat postgres:16\n"
        "then export WOMBAT_TEST_PG_DSN=postgresql://postgres:wombat@localhost:5433/postgres"
    ),
)

_DRY_MATRIX = PersonaMatrix(
    brevity=DEFAULT_MATRIX.brevity,
    warmth=DEFAULT_MATRIX.warmth,
    directness=DEFAULT_MATRIX.directness,
    humor=Humor.DRY,
    proactivity=DEFAULT_MATRIX.proactivity,
)


class _FakeStore(SettingsStore):
    """In-memory ``SettingsStore`` double (never opens a real connection — both public methods
    are fully overridden) — the ``tests/settings_app/test_api.py`` fake pattern, subclassed so
    LivePersona's ``store: SettingsStore | None`` typing stays strict-mypy-clean."""

    def __init__(self, *, initial: dict[str, Any] | None = None) -> None:
        super().__init__(dsn="postgresql://unused/fake")
        self._rows: dict[str, Any] = dict(initial or {})
        self.put_calls: list[dict[str, Any]] = []

    def get_all(self) -> dict[str, Any]:
        return dict(self._rows)

    def put(self, mapping: dict[str, Any]) -> None:
        self.put_calls.append(dict(mapping))
        self._rows.update(mapping)


class _RaisingReadStore(SettingsStore):
    """``get_all`` raises every time — proves the AC4 failure-streak warning-once posture."""

    def __init__(self) -> None:
        super().__init__(dsn="postgresql://unused/fake")

    def get_all(self) -> dict[str, Any]:
        raise RuntimeError("simulated store read failure")

    def put(self, mapping: dict[str, Any]) -> None:
        raise AssertionError("not exercised")


class _RaisingWriteStore(SettingsStore):
    """``put`` raises every time, ``get_all`` succeeds — proves AC3's degrade posture."""

    def __init__(self, *, initial: dict[str, Any] | None = None) -> None:
        super().__init__(dsn="postgresql://unused/fake")
        self._rows: dict[str, Any] = dict(initial or {})

    def get_all(self) -> dict[str, Any]:
        return dict(self._rows)

    def put(self, mapping: dict[str, Any]) -> None:
        raise RuntimeError("simulated store write failure")


class _NoConnectStore(SettingsStore):
    """``_connection`` raises if ever touched — structural proof that LivePersona performs ZERO
    store I/O at construction (BINDING v2.61 ruling 2)."""

    def __init__(self) -> None:
        super().__init__(dsn="postgresql://unused/fake")

    def _connection(self) -> Any:
        raise AssertionError("LivePersona touched the store at construction")


def _live_persona(name: str = "Steward") -> LivePersona:
    """A store-less instance — fully in-memory (AC3)."""
    return LivePersona(DEFAULT_MATRIX, name)


# --------------------------------------------------------------------------------------- AC1


def test_default_matrix_renders_byte_identical_to_live_oracles_for_all_four_mouths() -> None:
    live_persona = _live_persona()

    assert live_persona.instruction(Mouth.COMPOSE) == compose_live("Steward")
    assert live_persona.instruction(Mouth.BRIEF) == brief_live("Steward")
    assert live_persona.instruction(Mouth.DRAFT) == draft_live("Steward")
    assert live_persona.instruction(Mouth.REFLECTION) == REFLECTION_LIVE


def test_construction_never_touches_the_store(caplog: pytest.LogCaptureFixture) -> None:
    """BINDING v2.61 ruling 2 — fully lazy, even when a store IS given."""
    with caplog.at_level(logging.WARNING, logger="wombat.persona.live"):
        LivePersona(DEFAULT_MATRIX, "Steward", store=_NoConnectStore())  # must not raise

    assert caplog.records == []  # a real store was given -- no store-less warning either


def test_store_less_construction_logs_exactly_one_loud_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="wombat.persona.live"):
        LivePersona(DEFAULT_MATRIX, "Steward")

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1


# --------------------------------------------------------------------------------------- AC2


def test_set_applies_the_new_matrix_immediately_no_restart() -> None:
    live_persona = _live_persona()

    before = live_persona.instruction(Mouth.COMPOSE)
    live_persona.set(_DRY_MATRIX)
    after = live_persona.instruction(Mouth.COMPOSE)

    assert before != after
    assert live_persona.matrix == _DRY_MATRIX


def test_set_persists_the_five_persona_keys_via_put_never_touching_other_rows() -> None:
    store = _FakeStore(initial={"wombat_assistant_name": "Marvin", "wombat_tts_provider": "fish"})
    live_persona = LivePersona(DEFAULT_MATRIX, "Steward", store=store)
    matrix = PersonaMatrix(
        brevity=DEFAULT_MATRIX.brevity,
        warmth=Warmth.WARM,
        directness=DEFAULT_MATRIX.directness,
        humor=Humor.DRY,
        proactivity=DEFAULT_MATRIX.proactivity,
    )

    live_persona.set(matrix)

    assert len(store.put_calls) == 1
    written = store.put_calls[0]
    assert set(written) == {
        "wombat_persona_brevity",
        "wombat_persona_warmth",
        "wombat_persona_directness",
        "wombat_persona_humor",
        "wombat_persona_proactivity",
        "wombat_persona_pins",
    }
    assert written["wombat_persona_warmth"] == "warm"
    assert written["wombat_persona_humor"] == "dry"
    # the pre-existing, unrelated rows are untouched (a key-level upsert, never a read-modify-write)
    assert store.get_all()["wombat_assistant_name"] == "Marvin"
    assert store.get_all()["wombat_tts_provider"] == "fish"


# --------------------------------------------------------------------------------------- AC3


def test_set_write_failure_still_applies_in_memory_one_warning_never_raises(
    caplog: pytest.LogCaptureFixture,
) -> None:
    live_persona = LivePersona(DEFAULT_MATRIX, "Steward", store=_RaisingWriteStore())

    with caplog.at_level(logging.WARNING, logger="wombat.persona.live"):
        live_persona.set(_DRY_MATRIX)  # must not raise

    assert live_persona.matrix == _DRY_MATRIX  # in-memory still applied
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1


def test_store_less_set_applies_in_memory_and_adds_no_further_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The ONE loud log for a store-less instance fires at construction; set() itself degrades
    silently (persistence was already honestly declared absent)."""
    live_persona = _live_persona()
    caplog.clear()  # drop the ONE construction-time warning — this test is about set()'s own log

    with caplog.at_level(logging.WARNING, logger="wombat.persona.live"):
        live_persona.set(_DRY_MATRIX)  # must not raise

    assert live_persona.matrix == _DRY_MATRIX
    assert caplog.records == []


# --------------------------------------------------------------------------------------- AC4


def test_poll_settings_store_less_is_a_safe_noop() -> None:
    live_persona = _live_persona()

    live_persona.poll_settings()  # must not raise

    assert live_persona.matrix == DEFAULT_MATRIX


def test_poll_settings_first_beat_hydrates_matrix_and_pins_from_the_store() -> None:
    store = _FakeStore(
        initial={
            "wombat_persona_brevity": "terse",
            "wombat_persona_warmth": "reserved",
            "wombat_persona_directness": "gentle",
            "wombat_persona_humor": "dry",
            "wombat_persona_proactivity": "balanced",
            "wombat_persona_pins": {"humor": "2026-07-01T00:00:00+00:00"},
        }
    )
    live_persona = LivePersona(DEFAULT_MATRIX, "Steward", store=store)

    live_persona.poll_settings()

    assert live_persona.matrix.humor is Humor.DRY
    assert live_persona.matrix.directness is Directness.GENTLE
    assert live_persona.instruction(Mouth.COMPOSE) != compose_live("Steward")
    assert "humor" in live_persona.pinned_axes(datetime(2026, 7, 2, tzinfo=UTC))


def test_poll_settings_first_beat_empty_table_leaves_defaults_standing() -> None:
    live_persona = LivePersona(DEFAULT_MATRIX, "Steward", store=_FakeStore())

    live_persona.poll_settings()

    assert live_persona.matrix == DEFAULT_MATRIX
    assert live_persona.pinned_axes(datetime.now(UTC)) == frozenset()


def test_poll_settings_unchanged_table_is_a_noop_after_the_first_beat() -> None:
    store = _FakeStore(initial={"wombat_persona_humor": "dry"})
    live_persona = LivePersona(DEFAULT_MATRIX, "Steward", store=store)
    live_persona.poll_settings()  # first beat -- hydrates

    live_persona.poll_settings()  # second beat -- table unchanged

    assert live_persona.matrix.humor is Humor.DRY
    assert store.put_calls == []  # never wrote anything -- a pure no-op poll


def test_poll_settings_app_edit_swaps_stamps_pin_and_persists() -> None:
    store = _FakeStore()
    live_persona = LivePersona(DEFAULT_MATRIX, "Steward", store=store)
    live_persona.poll_settings()  # first beat -- empty table, defaults stand

    # An external writer (the settings-app path, TK-197/TK-200) updates one persona key row.
    store.put({"wombat_persona_humor": "dry"})

    live_persona.poll_settings()

    assert live_persona.matrix.humor is Humor.DRY
    assert "humor" in live_persona.pinned_axes(datetime.now(UTC))
    assert len(store.put_calls) == 2  # the external write, then LivePersona's own pin persist
    assert "humor" in store.put_calls[-1]["wombat_persona_pins"]


def test_poll_settings_app_edit_partial_keys_keep_the_current_value_for_the_rest() -> None:
    store = _FakeStore()
    live_persona = LivePersona(DEFAULT_MATRIX, "Steward", store=store)
    live_persona.poll_settings()

    store.put({"wombat_persona_humor": "dry"})
    live_persona.poll_settings()

    assert live_persona.matrix.humor is Humor.DRY
    assert live_persona.matrix.warmth is DEFAULT_MATRIX.warmth  # untouched, stays default


def test_poll_settings_read_failure_never_raises_warns_once_per_streak(
    caplog: pytest.LogCaptureFixture,
) -> None:
    live_persona = LivePersona(DEFAULT_MATRIX, "Steward", store=_RaisingReadStore())

    with caplog.at_level(logging.WARNING, logger="wombat.persona.live"):
        for _ in range(5):  # many Sweeper beats over the SAME failure streak
            live_persona.poll_settings()  # must not raise

    assert live_persona.matrix == DEFAULT_MATRIX
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1


def test_poll_settings_transient_failure_then_success_recovers_and_resets_the_guard(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class _FlakyOnceStore(SettingsStore):
        def __init__(self) -> None:
            super().__init__(dsn="postgresql://unused/fake")
            self._rows: dict[str, Any] = {"wombat_persona_humor": "dry"}
            self._calls = 0

        def get_all(self) -> dict[str, Any]:
            self._calls += 1
            if self._calls == 1:
                raise RuntimeError("transient store read failure (simulated)")
            return dict(self._rows)

        def put(self, mapping: dict[str, Any]) -> None:
            self._rows.update(mapping)

    live_persona = LivePersona(DEFAULT_MATRIX, "Steward", store=_FlakyOnceStore())

    with caplog.at_level(logging.WARNING, logger="wombat.persona.live"):
        live_persona.poll_settings()  # the first read raises

    assert live_persona.matrix == DEFAULT_MATRIX  # not yet applied
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1

    live_persona.poll_settings()  # retried -- now succeeds (first-beat hydrate)

    assert live_persona.matrix.humor is Humor.DRY


# ---------------------------------------------------------------------------- TK-214 pins (AC-pins)


def test_set_explicit_default_stamps_a_pin_for_exactly_the_changed_axis() -> None:
    live_persona = _live_persona()

    live_persona.set(_DRY_MATRIX)  # explicit=True by default — only humor changed

    pinned = live_persona.pinned_axes(datetime.now(UTC))
    assert pinned == frozenset({"humor"})


def test_set_explicit_persists_pins_alongside_the_five_persona_keys() -> None:
    store = _FakeStore()
    live_persona = LivePersona(DEFAULT_MATRIX, "Steward", store=store)

    live_persona.set(_DRY_MATRIX)

    saved_pins = store.get_all()["wombat_persona_pins"]
    assert "humor" in saved_pins
    stamped_at = datetime.fromisoformat(saved_pins["humor"])
    assert stamped_at.tzinfo is not None  # aware-UTC, per the ruled wire shape


def test_set_explicit_an_unchanged_matrix_pins_nothing() -> None:
    live_persona = _live_persona()

    live_persona.set(DEFAULT_MATRIX)  # identical matrix -> no axis changed

    assert live_persona.pinned_axes(datetime.now(UTC)) == frozenset()


def test_set_explicit_false_stamps_no_pin() -> None:
    live_persona = _live_persona()

    live_persona.set(_DRY_MATRIX, explicit=False)  # the dream-nudge path (TK-214)

    assert live_persona.pinned_axes(datetime.now(UTC)) == frozenset()


def test_set_explicit_false_nudge_write_is_not_mistaken_for_an_app_edit_by_the_next_poll() -> None:
    """AC-pins-2's critical half: a dream nudge persists its own write; the NEXT poll must not
    re-observe that write as an external app edit and wrongly stamp a pin."""
    store = _FakeStore()
    live_persona = LivePersona(DEFAULT_MATRIX, "Steward", store=store)
    live_persona.poll_settings()  # first beat -- establishes the cursor over an empty table

    live_persona.set(_DRY_MATRIX, explicit=False)  # the nudge persists humor=dry, stamps nothing
    live_persona.poll_settings()  # must NOT treat the nudge's own write as an app edit

    assert live_persona.matrix.humor is Humor.DRY
    assert live_persona.pinned_axes(datetime.now(UTC)) == frozenset()


def test_pinned_axes_excludes_stamps_older_than_pin_days() -> None:
    now = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)
    recent = (now - timedelta(days=3)).isoformat()
    stale = (now - timedelta(days=PERSONA_PIN_DAYS + 1)).isoformat()
    store = _FakeStore(initial={"wombat_persona_pins": {"brevity": recent, "warmth": stale}})
    live_persona = LivePersona(DEFAULT_MATRIX, "Steward", store=store)
    live_persona.poll_settings()  # pins hydrate on the first beat

    assert live_persona.pinned_axes(now) == frozenset({"brevity"})


def test_hydrate_absent_table_never_raises_and_yields_no_pins() -> None:
    live_persona = LivePersona(DEFAULT_MATRIX, "Steward", store=_FakeStore())

    live_persona.poll_settings()

    assert live_persona.pinned_axes(datetime.now(UTC)) == frozenset()


def test_hydrate_malformed_pins_value_never_raises() -> None:
    store = _FakeStore(initial={"wombat_persona_pins": "not-a-dict"})
    live_persona = LivePersona(DEFAULT_MATRIX, "Steward", store=store)

    live_persona.poll_settings()  # must not raise

    assert live_persona.pinned_axes(datetime.now(UTC)) == frozenset()


# --------------------------------------------------------------------------------- pg-gated (AC1)


@_requires_pg
def test_ac1_restart_survival_first_beat_hydrates_from_a_real_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC1: ``set()`` persists to a real ``wombat_settings`` row; a FRESH LivePersona over the
    SAME store performs ZERO store I/O at construction, and its FIRST ``poll_settings()`` hydrates
    axes+pins from the table — the restart-survival story end to end."""
    assert _DSN is not None
    with psycopg.connect(_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS wombat_settings CASCADE")
        conn.commit()
        ensure_schema(conn)

    store_a = SettingsStore(_DSN)
    live_a = LivePersona(DEFAULT_MATRIX, "Steward", store=store_a)
    live_a.set(_DRY_MATRIX)
    store_a.close()

    store_b = SettingsStore(_DSN)
    live_b = LivePersona(DEFAULT_MATRIX, "Steward", store=store_b)  # zero I/O so far

    live_b.poll_settings()  # the first-beat hydrate

    assert live_b.matrix == _DRY_MATRIX
    assert live_b.instruction(Mouth.COMPOSE) != compose_live("Steward")
    assert "humor" in live_b.pinned_axes(datetime.now(UTC))
    store_b.close()
