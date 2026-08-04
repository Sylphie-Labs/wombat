"""TK-343 acceptance criteria — LastTurnOriginRegister (DEC-79).

Mirrors ``tests/voice/test_reply_context.py``'s own shape (a settable fake epoch-seconds clock,
pure in-memory assertions, no Postgres, no real network). The ONE deliberate divergence from
``LastSpokenRegister``'s test suite: ``take()`` is a CONSUMING read, so every "still fresh" case
here is proven via a SECOND, separate ``note_origin`` (or checked before any earlier ``take()``
call), never via a repeated ``current()``-style peek — this register has no such peek.
"""

from __future__ import annotations

from wombat.voice.turn_origin import TURN_ORIGIN_TTL_SECONDS, LastTurnOriginRegister, TurnOrigin


class _FakeClock:
    """A settable epoch-seconds clock double (mirrors test_reply_context.py's own)."""

    def __init__(self, now: float = 0.0) -> None:
        self._now = now

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


# --- nothing noted yet ----------------------------------------------------------------------


def test_take_is_none_before_any_note_origin() -> None:
    register = LastTurnOriginRegister(clock=_FakeClock())
    assert register.take() is None


# --- within TTL: take() returns the origin ---------------------------------------------------


def test_take_returns_origin_within_ttl() -> None:
    clock = _FakeClock(now=1000.0)
    register = LastTurnOriginRegister(clock=clock)

    register.note_origin("watch-1", "utt-1")
    clock.advance(TURN_ORIGIN_TTL_SECONDS - 1.0)

    assert register.take() == TurnOrigin(device_id="watch-1", utterance_id="utt-1")


def test_take_at_exactly_the_ttl_boundary_still_returns_the_origin() -> None:
    """``age <= TTL`` (mirrors LastSpokenRegister's own boundary rule)."""
    clock = _FakeClock(now=0.0)
    register = LastTurnOriginRegister(clock=clock)

    register.note_origin("watch-1", "utt-1")
    clock.advance(TURN_ORIGIN_TTL_SECONDS)

    assert register.take() == TurnOrigin(device_id="watch-1", utterance_id="utt-1")


# --- above TTL: take() returns None ------------------------------------------------------------


def test_take_returns_none_once_past_the_ttl() -> None:
    clock = _FakeClock(now=0.0)
    register = LastTurnOriginRegister(clock=clock)

    register.note_origin("watch-1", "utt-1")
    clock.advance(TURN_ORIGIN_TTL_SECONDS + 0.001)

    assert register.take() is None


# --- AC2: single-fetch consumption — a claimed origin is gone for the NEXT speak --------------


def test_second_take_in_a_row_returns_none_even_though_still_within_ttl() -> None:
    """TK-343 AC2: 'a remote voice turn accepted at the voice route, then a second turn from the
    laptop ... the factory is invoked once per utterance and the two speaks route DIFFERENTLY' —
    the SAME register, read twice back to back, must not hand out the SAME origin twice."""
    clock = _FakeClock(now=0.0)
    register = LastTurnOriginRegister(clock=clock)

    register.note_origin("watch-1", "utt-1")

    first = register.take()
    second = register.take()  # no time has even passed — still well within the TTL

    assert first == TurnOrigin(device_id="watch-1", utterance_id="utt-1")
    assert second is None


def test_take_after_a_fresh_note_origin_following_an_earlier_claim_returns_the_new_origin() -> None:
    """A NEW note_origin after an earlier claim re-arms the slot — newest wins, single slot."""
    clock = _FakeClock(now=0.0)
    register = LastTurnOriginRegister(clock=clock)

    register.note_origin("watch-1", "utt-1")
    assert register.take() == TurnOrigin(device_id="watch-1", utterance_id="utt-1")

    register.note_origin("phone-1", "utt-2")
    assert register.take() == TurnOrigin(device_id="phone-1", utterance_id="utt-2")


# --- newest wins, single slot (an un-taken origin is replaced, not queued) ---------------------


def test_second_note_origin_replaces_an_unclaimed_first() -> None:
    clock = _FakeClock(now=0.0)
    register = LastTurnOriginRegister(clock=clock)

    register.note_origin("watch-1", "utt-1")
    clock.advance(1.0)
    register.note_origin("watch-2", "utt-2")

    assert register.take() == TurnOrigin(device_id="watch-2", utterance_id="utt-2")


def test_second_note_origin_resets_the_ttl_window() -> None:
    clock = _FakeClock(now=0.0)
    register = LastTurnOriginRegister(clock=clock)

    register.note_origin("watch-1", "utt-1")
    clock.advance(TURN_ORIGIN_TTL_SECONDS - 1.0)
    register.note_origin("watch-2", "utt-2")
    clock.advance(TURN_ORIGIN_TTL_SECONDS - 1.0)

    assert register.take() == TurnOrigin(device_id="watch-2", utterance_id="utt-2")


# --- default construction / custom ttl_seconds --------------------------------------------------


def test_default_construction_stays_byte_identical_to_the_120s_constant() -> None:
    clock = _FakeClock(now=0.0)
    register = LastTurnOriginRegister(clock=clock)

    register.note_origin("watch-1", "utt-1")
    clock.advance(TURN_ORIGIN_TTL_SECONDS)
    assert register.take() is not None

    register.note_origin("watch-1", "utt-2")
    clock.advance(TURN_ORIGIN_TTL_SECONDS + 0.001)
    assert register.take() is None


# --- TK-343 critical repair: claims_suppressed() ----------------------------------------------


def test_take_inside_claims_suppressed_returns_none_and_leaves_a_fresh_origin_untouched() -> None:
    clock = _FakeClock(now=0.0)
    register = LastTurnOriginRegister(clock=clock)
    register.note_origin("watch-1", "utt-1")

    with register.claims_suppressed():
        assert register.take() is None

    # the suppressed take() never consumed the slot -- it's still claimable afterward.
    assert register.take() == TurnOrigin(device_id="watch-1", utterance_id="utt-1")


def test_take_before_claims_suppressed_is_unaffected() -> None:
    clock = _FakeClock(now=0.0)
    register = LastTurnOriginRegister(clock=clock)
    register.note_origin("watch-1", "utt-1")

    assert register.take() == TurnOrigin(device_id="watch-1", utterance_id="utt-1")

    with register.claims_suppressed():
        assert register.take() is None


def test_claims_suppressed_restores_permission_even_if_the_block_raises() -> None:
    clock = _FakeClock(now=0.0)
    register = LastTurnOriginRegister(clock=clock)
    register.note_origin("watch-1", "utt-1")

    class _Boom(Exception):
        pass

    try:
        with register.claims_suppressed():
            raise _Boom
    except _Boom:
        pass

    assert register.take() == TurnOrigin(device_id="watch-1", utterance_id="utt-1")


def test_nested_claims_suppressed_restores_the_prior_state_not_unconditionally_true() -> None:
    clock = _FakeClock(now=0.0)
    register = LastTurnOriginRegister(clock=clock)
    register.note_origin("watch-1", "utt-1")

    with register.claims_suppressed():
        with register.claims_suppressed():
            assert register.take() is None
        # still inside the outer suppression -- must remain suppressed, not reset to permitted.
        assert register.take() is None

    assert register.take() == TurnOrigin(device_id="watch-1", utterance_id="utt-1")


def test_custom_ttl_seconds_is_fresh_at_250s_and_stale_at_301s() -> None:
    clock = _FakeClock(now=0.0)
    register = LastTurnOriginRegister(clock=clock, ttl_seconds=300.0)

    register.note_origin("watch-1", "utt-1")
    clock.advance(250.0)
    assert register.take() == TurnOrigin(device_id="watch-1", utterance_id="utt-1")

    register.note_origin("watch-1", "utt-2")
    clock.advance(301.0)
    assert register.take() is None
