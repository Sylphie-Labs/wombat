"""tests/capabilities/test_capability_charter.py — TK-298 (DEC-65(h), RULING r4 v2.168) and
TK-312 (DEC-68(f)): the CAPABILITY_CHARTER structural diff oracle.

wombat's charter (TK-284, DEC-62) told the model what it CANNOT do but never that it CAN recall
personal details the user shared earlier — an omission, not a capability grant: the model already
sees prior-conversation facts in its prompt (DEC-65f's ``known_user_context``), so silently never
telling it that ability existed only made "I don't remember" a plausible but WRONG guess. RULING
r4 inserts exactly ONE new sentence, conditionally phrased ("when they appear in what you are
given") so it stays TRUE regardless of whether the store actually holds anything for this turn —
DEC-62's accuracy invariant amended for ACCURACY, never weakened.

TK-312 (DEC-68(f)) repeats the same move for screen observation (TK-309..314's ambient-
observability arc): the charter never told the model it CAN see the active application/window, so
it inserts ONE more sentence, again conditionally phrased ("when they have turned on screen
observation and it appears in what you are given") so it stays TRUE whether the toggle is on or
off.

This test diffs the CURRENT (imported, live) ``CAPABILITY_CHARTER`` against two hand-pinned
baselines at SENTENCE granularity — the PRE-TK-298 text, and the POST-TK-298/PRE-TK-312 text (the
byte-identical string this ticket found in the repo before editing it) — proving in each stage
that exactly one sentence was inserted and every other sentence, including every "cannot"/"never"
clause, is byte-identical and untouched (a structural assert, not a human eyeball diff).
"""

from __future__ import annotations

import difflib
import re

from wombat.persona.capabilities import CAPABILITY_CHARTER

# The charter exactly as it stood before TK-298 (verified against the repo pre-change, lines
# 15-22 of src/wombat/persona/capabilities.py) — the diff oracle below measures the CURRENT
# (live, imported) charter against this fixed baseline. Never edited by this ticket; it is the
# "before" snapshot the diff is taken against.
_PREVIOUS_CHARTER = (
    "Your abilities are fixed and known. You can converse and answer from what you are given, "
    "deliver the morning brief from read-only Calendar and Gmail, draft Gmail replies that the "
    "user must approve, and read web pages when asked. You cannot set alarms, timers, or "
    "reminders, cannot send email or modify the calendar, and cannot perform any other action on "
    "any device or service. If the user asks for something outside these abilities, say plainly "
    "that you can't do that - never say an action was done, is being done, or is scheduled."
)

# RULING r4 (v2.168): the ONE sentence TK-298 inserts, conditionally phrased so it is TRUE
# regardless of store contents.
_INSERTED_SENTENCE = (
    "You remember personal details the user has shared in earlier conversations when they "
    "appear in what you are given."
)

# The charter exactly as it stood after TK-298 and before TK-312 (verified against the repo
# pre-TK-312-change, lines 15-25 of src/wombat/persona/capabilities.py) — the second-stage diff
# oracle below measures the CURRENT (live, imported) charter against this fixed baseline. Never
# edited by this ticket; it is the "before" snapshot TK-312's insert is taken against.
_POST_TK298_CHARTER = (
    "Your abilities are fixed and known. You can converse and answer from what you are given, "
    "deliver the morning brief from read-only Calendar and Gmail, draft Gmail replies that the "
    "user must approve, and read web pages when asked. "
    "You remember personal details the user has shared in earlier conversations when they appear "
    "in what you are given. "
    "You cannot set alarms, timers, or "
    "reminders, cannot send email or modify the calendar, and cannot perform any other action on "
    "any device or service. If the user asks for something outside these abilities, say plainly "
    "that you can't do that - never say an action was done, is being done, or is scheduled."
)

# DEC-68(f): the ONE sentence TK-312 inserts, conditionally phrased so it is TRUE whether screen
# observation is toggled on or off.
_INSERTED_SENTENCE_SCREEN = (
    "You can see which application and window the user is currently working in when they have "
    "turned on screen observation and it appears in what you are given."
)

_SENTENCE_BOUNDARY = re.compile(r"(?<=\. )")


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_BOUNDARY.split(text) if s.strip()]


def test_previous_charter_splits_into_the_expected_four_sentences() -> None:
    # Sanity-checks the sentence splitter against the known baseline before trusting it as a diff
    # oracle below.
    sentences = _sentences(_PREVIOUS_CHARTER)
    assert len(sentences) == 4
    assert sentences[0] == "Your abilities are fixed and known."
    assert sentences[1].startswith("You can converse")
    assert sentences[2].startswith("You cannot set alarms")
    assert sentences[3].startswith("If the user asks")


def _assert_single_contiguous_insert(
    old_sentences: list[str], new_sentences: list[str], expected_sentence: str
) -> None:
    matcher = difflib.SequenceMatcher(None, old_sentences, new_sentences, autojunk=False)
    opcodes = matcher.get_opcodes()

    inserts = [op for op in opcodes if op[0] == "insert"]
    non_equal = [op for op in opcodes if op[0] != "equal"]

    # Exactly one contiguous insertion, and every other opcode is 'equal' — no replace/delete
    # anywhere, i.e. every pre-existing sentence is byte-identical, in the same order.
    assert non_equal == inserts
    assert len(inserts) == 1

    _tag, i1, i2, j1, j2 = inserts[0]
    assert i1 == i2  # nothing from the old text was consumed/replaced by this opcode
    assert new_sentences[j1:j2] == [expected_sentence]


def test_charter_diff_inserts_exactly_one_sentence_and_touches_nothing_else() -> None:
    # Stage 1 (TK-298): PRE-TK-298 baseline -> POST-TK-298/PRE-TK-312 baseline.
    old_sentences = _sentences(_PREVIOUS_CHARTER)
    post_tk298_sentences = _sentences(_POST_TK298_CHARTER)
    _assert_single_contiguous_insert(old_sentences, post_tk298_sentences, _INSERTED_SENTENCE)

    # The insertion lands after "...read web pages when asked." and before "You cannot set
    # alarms..." (RULING r4's ruled position) — i.e. right after old_sentences[1].
    matcher = difflib.SequenceMatcher(None, old_sentences, post_tk298_sentences, autojunk=False)
    i1 = next(op[1] for op in matcher.get_opcodes() if op[0] == "insert")
    assert i1 == 2


def test_charter_diff_inserts_the_screen_observation_sentence_and_touches_nothing_else() -> None:
    # Stage 2 (TK-312, DEC-68(f)): POST-TK-298 baseline -> CURRENT (live) charter. The oracle
    # enforces shape (one contiguous insert, zero deletes/replaces), not position.
    post_tk298_sentences = _sentences(_POST_TK298_CHARTER)
    live_sentences = _sentences(CAPABILITY_CHARTER)
    _assert_single_contiguous_insert(
        post_tk298_sentences, live_sentences, _INSERTED_SENTENCE_SCREEN
    )


def test_no_cannot_or_never_clause_was_touched() -> None:
    # Spans the full arc: every guard clause present in the PRE-TK-298 baseline must still be
    # present, byte-identical, in the CURRENT (live, post-TK-312) charter.
    old_sentences = _sentences(_PREVIOUS_CHARTER)
    new_sentences = _sentences(CAPABILITY_CHARTER)

    old_guard_clauses = [
        s for s in old_sentences if "cannot" in s.lower() or "never" in s.lower()
    ]
    assert old_guard_clauses  # sanity: the baseline really does contain guard clauses
    for clause in old_guard_clauses:
        assert clause in new_sentences


def test_inserted_sentence_is_conditionally_phrased_true_regardless_of_store_contents() -> None:
    # RULING r4: phrased "when they appear in what you are given" — never an unconditional claim
    # that memory always contains something, so it stays TRUE even on an empty
    # known_user_context.
    assert "when they appear in what you are given" in _INSERTED_SENTENCE
    assert "cannot" not in _INSERTED_SENTENCE.lower()
    assert "never" not in _INSERTED_SENTENCE.lower()
    assert _INSERTED_SENTENCE in CAPABILITY_CHARTER


def test_screen_observation_sentence_is_conditionally_phrased_true_in_both_toggle_worlds() -> (
    None
):
    # DEC-68(f): phrased "when they have turned on screen observation and it appears in what you
    # are given" — never an unconditional claim that the toggle is on, so it stays TRUE whether
    # screen observation is enabled or disabled.
    assert _INSERTED_SENTENCE_SCREEN == (
        "You can see which application and window the user is currently working in when they "
        "have turned on screen observation and it appears in what you are given."
    )
    assert "when they have turned on screen observation" in _INSERTED_SENTENCE_SCREEN
    assert "and it appears in what you are given" in _INSERTED_SENTENCE_SCREEN
    assert "cannot" not in _INSERTED_SENTENCE_SCREEN.lower()
    assert "never" not in _INSERTED_SENTENCE_SCREEN.lower()
    assert _INSERTED_SENTENCE_SCREEN in CAPABILITY_CHARTER
