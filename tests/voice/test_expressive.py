"""TK-327 — voice.expressive acceptance criteria (DEC-71b/c/d/e as revised by DEC-72b/c/h/i).

AC3 (strip_allowed_tags): ``test_ac3_strip_allowed_tags_removes_tags_and_collapses_whitespace``,
``test_ac3_strip_allowed_tags_is_idempotent``.
AC4 (module-structural): ``test_ac4_tag_definitions_has_exactly_the_eight_pinned_keys``,
``test_ac4_allowed_tags_equals_tag_definitions_key_set``, ``test_ac4_every_key_is_square_bracket_
lowercase``, ``test_ac4_no_parenthesized_s1_tag_form_in_module_source``, ``test_ac4_expressive_
fish_models_is_the_enumerated_three_model_set``.

All pure — no IO, no model, no config.
"""

from __future__ import annotations

import inspect
import re

from wombat.voice import expressive
from wombat.voice.expressive import (
    ALLOWED_TAGS,
    EXPRESSIVE_FISH_MODELS,
    TAG_DEFINITIONS,
    find_disallowed_token,
    render_expressive_instruction,
    strip_allowed_tags,
)

_PINNED_TAGS = frozenset(
    {
        "[calm]",
        "[curious]",
        "[sympathetic]",
        "[soft tone]",
        "[chuckling]",
        "[sighing]",
        "[break]",
        "[long-break]",
    }
)


# --- AC4: module-structural ------------------------------------------------------------------


def test_ac4_tag_definitions_has_exactly_the_eight_pinned_keys() -> None:
    assert set(TAG_DEFINITIONS) == _PINNED_TAGS
    assert len(TAG_DEFINITIONS) == 8


def test_ac4_allowed_tags_equals_tag_definitions_key_set() -> None:
    assert frozenset(TAG_DEFINITIONS) == ALLOWED_TAGS


def test_ac4_every_key_is_square_bracket_lowercase() -> None:
    for tag in TAG_DEFINITIONS:
        assert tag.startswith("[")
        assert tag.endswith("]")
        assert tag == tag.lower()


def test_ac4_no_parenthesized_s1_tag_form_anywhere_in_module_source() -> None:
    # DEC-72c: no S1 parenthesized vocabulary lives here, not even as a leftover comment example
    # (other than this test's own literal, which inspect.getsource never sees since it reads
    # THIS module, not itself).
    source = inspect.getsource(expressive)
    for tag in TAG_DEFINITIONS:
        paren_form = "(" + tag[1:-1] + ")"
        assert paren_form not in source


def test_ac4_expressive_fish_models_is_the_enumerated_three_model_set() -> None:
    assert frozenset({"s2-pro", "s2.1-pro", "s2.1-pro-free"}) == EXPRESSIVE_FISH_MODELS


# --- render_expressive_instruction: parity with TAG_DEFINITIONS, nothing beyond the 8 ----------


def test_instruction_carries_every_tag_definition_and_nothing_beyond_the_eight() -> None:
    instruction = render_expressive_instruction()
    for tag, guidance in TAG_DEFINITIONS.items():
        assert tag in instruction
        assert guidance in instruction
    # nothing beyond the 8 -- every bracketed token the instruction itself contains is allowed.
    for token in re.findall(r"\[[^\]]+\]", instruction):
        assert token in ALLOWED_TAGS


def test_instruction_forbids_a_marker_directly_before_an_opening_parenthesis() -> None:
    instruction = render_expressive_instruction()
    assert "directly before an opening parenthesis" in instruction


def test_instruction_is_deterministic() -> None:
    assert render_expressive_instruction() == render_expressive_instruction()


# --- find_disallowed_token ----------------------------------------------------------------------


def test_find_disallowed_token_none_when_every_bracket_is_allowed() -> None:
    text = "[calm] Your meeting moved. [break] Nothing else needs you."
    assert find_disallowed_token(text, ALLOWED_TAGS) is None


def test_find_disallowed_token_returns_the_first_out_of_set_fixed_tag() -> None:
    assert find_disallowed_token("[screaming] Look out!", ALLOWED_TAGS) == "[screaming]"


def test_find_disallowed_token_returns_the_first_out_of_set_free_form_description() -> None:
    text = "[warm, slightly amused] Sure."
    assert find_disallowed_token(text, ALLOWED_TAGS) == "[warm, slightly amused]"


def test_find_disallowed_token_rejects_sic_the_pinned_accepted_false_positive() -> None:
    assert find_disallowed_token("[sic]", ALLOWED_TAGS) == "[sic]"


def test_find_disallowed_token_ignores_prose_parentheses() -> None:
    assert find_disallowed_token("(around noon)", ALLOWED_TAGS) is None


def test_find_disallowed_token_against_the_empty_allowed_set_rejects_any_bracket() -> None:
    assert find_disallowed_token("[calm] hello", frozenset()) == "[calm]"


# --- AC3: strip_allowed_tags ---------------------------------------------------------------------


def test_ac3_strip_allowed_tags_removes_tags_and_collapses_whitespace() -> None:
    text = "[chuckling] Done. [break] See you (around noon)."
    assert strip_allowed_tags(text) == "Done. See you (around noon)."


def test_ac3_strip_allowed_tags_is_idempotent() -> None:
    text = "[chuckling] Done. [break] See you (around noon)."
    once = strip_allowed_tags(text)
    twice = strip_allowed_tags(once)
    assert once == twice


def test_ac3_strip_allowed_tags_leaves_out_of_set_brackets_and_prose_untouched() -> None:
    text = "[screaming] [sic] plain text (around noon)"
    assert strip_allowed_tags(text) == text
