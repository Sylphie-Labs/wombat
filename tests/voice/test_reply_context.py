"""TK-288 acceptance criteria — LastSpokenRegister (DEC-64 gap A, half 1).

AC4: a fake-clock register — below LAST_SPOKEN_TTL_SECONDS returns the (600-char-truncated)
spoken text; above it returns None; a second ``note_spoken`` replaces the first (newest wins,
single slot). All PURE: no Postgres, no real network, no real clock (a fake float clock only).
"""

from __future__ import annotations

from wombat.voice.reply_context import (
    _MAX_SPOKEN_CHARS,
    LAST_SPOKEN_TTL_SECONDS,
    LastSpokenRegister,
)


class _FakeClock:
    """A settable epoch-seconds clock double."""

    def __init__(self, now: float = 0.0) -> None:
        self._now = now

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


# --- nothing spoken yet -------------------------------------------------------------------------


def test_current_is_none_before_any_note_spoken() -> None:
    register = LastSpokenRegister(clock=_FakeClock())
    assert register.current() is None


# --- AC4: within TTL returns the text; truncated to _MAX_SPOKEN_CHARS ---------------------------


def test_current_returns_text_within_ttl() -> None:
    clock = _FakeClock(now=1000.0)
    register = LastSpokenRegister(clock=clock)

    register.note_spoken("i-1", "Good morning.")
    clock.advance(LAST_SPOKEN_TTL_SECONDS - 1.0)

    assert register.current() == "Good morning."


def test_current_at_exactly_the_ttl_boundary_still_returns_the_text() -> None:
    """``age <= TTL`` per the briefing — the boundary itself is still valid."""
    clock = _FakeClock(now=0.0)
    register = LastSpokenRegister(clock=clock)

    register.note_spoken("i-1", "Good morning.")
    clock.advance(LAST_SPOKEN_TTL_SECONDS)

    assert register.current() == "Good morning."


def test_text_longer_than_max_spoken_chars_is_truncated() -> None:
    clock = _FakeClock()
    register = LastSpokenRegister(clock=clock)
    long_text = "x" * (_MAX_SPOKEN_CHARS + 50)

    register.note_spoken("i-1", long_text)

    result = register.current()
    assert result is not None
    assert len(result) == _MAX_SPOKEN_CHARS
    assert result == long_text[:_MAX_SPOKEN_CHARS]


# --- AC4: above TTL returns None -----------------------------------------------------------------


def test_current_returns_none_once_past_the_ttl() -> None:
    clock = _FakeClock(now=0.0)
    register = LastSpokenRegister(clock=clock)

    register.note_spoken("i-1", "Good morning.")
    clock.advance(LAST_SPOKEN_TTL_SECONDS + 0.001)

    assert register.current() is None


# --- AC4: newest wins, single slot ---------------------------------------------------------------


def test_second_note_spoken_replaces_the_first() -> None:
    clock = _FakeClock(now=0.0)
    register = LastSpokenRegister(clock=clock)

    register.note_spoken("i-1", "First reply.")
    clock.advance(1.0)
    register.note_spoken("i-2", "Second reply.")

    assert register.current() == "Second reply."


def test_second_note_spoken_resets_the_ttl_window() -> None:
    """The second note's own age is what matters — it is not stamped with the first note's
    (possibly much older) spoken_at."""
    clock = _FakeClock(now=0.0)
    register = LastSpokenRegister(clock=clock)

    register.note_spoken("i-1", "First reply.")
    clock.advance(LAST_SPOKEN_TTL_SECONDS - 1.0)
    register.note_spoken("i-2", "Second reply.")
    clock.advance(LAST_SPOKEN_TTL_SECONDS - 1.0)

    assert register.current() == "Second reply."


# --- TK-303 (DEC-67e): ttl_seconds is unpinned via a keyword-only ctor param ---------------------


def test_default_construction_stays_byte_identical_to_the_120s_constant() -> None:
    """No ttl_seconds passed -- the ctor default must still be LAST_SPOKEN_TTL_SECONDS (120s),
    proving every existing call site (and every test above) is behavior-preserving."""
    clock = _FakeClock(now=0.0)
    register = LastSpokenRegister(clock=clock)

    register.note_spoken("i-1", "Good morning.")
    clock.advance(LAST_SPOKEN_TTL_SECONDS)
    assert register.current() == "Good morning."

    clock.advance(0.001)
    assert register.current() is None


def test_ac1_custom_ttl_seconds_is_fresh_at_250s_and_stale_at_301s() -> None:
    """AC1: a register constructed with ttl_seconds=300 -- fresh at 250s, stale at 301s."""
    clock = _FakeClock(now=0.0)
    register = LastSpokenRegister(clock=clock, ttl_seconds=300.0)

    register.note_spoken("i-1", "Good morning.")

    clock.advance(250.0)
    assert register.current() == "Good morning."

    clock.advance(51.0)  # now at 301s
    assert register.current() is None
