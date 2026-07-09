"""TK-117 — extract_phrasing_hints acceptance criteria (EP-24, Q-99).

  AC1 pattern_id present in the loaded KB -> that entry's phrasing_hints (>=1 item),
      deterministic. Checked over the REAL packaged KB via load_psychology_kb().
  AC2 pattern_id absent -> [] with no exception.
  AC3 same pattern_id looked up twice -> equal lists (pure, no hidden state).

No verbatim-in-output guard and no clinical-language-in-hint-data guard here: those are
TK-114/TK-118's and TK-115's linter's ACs respectively, not this module's.
"""

from __future__ import annotations

from wombat.kb.loader import load_psychology_kb
from wombat.kb.phrasing_hints import extract_phrasing_hints
from wombat.kb.schema import GateCondition, KBEntry

_FIXTURE_ENTRY = KBEntry(
    pattern_id="fixture_pattern",
    description="A fixture pattern used only by tests.",
    gate_condition=GateCondition(metric="switch_rate", operator=">", threshold=0.6),
    phrasing_hints=("a fixture hint", "a second fixture hint"),
    autonomy_level="gentle_note",
    evidence_tag="fixture_source_2026",
    version=1,
)


# --------------------------------------------------------------------------------------- AC1


def test_ac1_known_pattern_id_returns_its_phrasing_hints() -> None:
    kb = load_psychology_kb()
    pattern_id = kb[0].pattern_id

    hints = extract_phrasing_hints(pattern_id, kb)

    assert hints == list(kb[0].phrasing_hints)
    assert len(hints) >= 1


def test_ac1_real_seed_kb_every_pattern_id_resolves_and_is_deterministic() -> None:
    kb = load_psychology_kb()

    for entry in kb:
        first_pass = extract_phrasing_hints(entry.pattern_id, kb)
        second_pass = extract_phrasing_hints(entry.pattern_id, kb)
        assert first_pass == list(entry.phrasing_hints)
        assert first_pass == second_pass
        assert len(first_pass) >= 1


# --------------------------------------------------------------------------------------- AC2


def test_ac2_unknown_pattern_id_returns_empty_list() -> None:
    assert extract_phrasing_hints("no_such_pattern", [_FIXTURE_ENTRY]) == []


def test_ac2_empty_kb_returns_empty_list() -> None:
    assert extract_phrasing_hints("fixture_pattern", []) == []


# --------------------------------------------------------------------------------------- AC3


def test_ac3_same_pattern_id_looked_up_twice_yields_equal_lists() -> None:
    first = extract_phrasing_hints("fixture_pattern", [_FIXTURE_ENTRY])
    second = extract_phrasing_hints("fixture_pattern", [_FIXTURE_ENTRY])

    assert first == second
    assert first is not second


def test_ac3_returned_list_is_independent_of_entry_tuple() -> None:
    hints = extract_phrasing_hints("fixture_pattern", [_FIXTURE_ENTRY])
    hints.append("mutated locally")

    assert extract_phrasing_hints("fixture_pattern", [_FIXTURE_ENTRY]) == list(
        _FIXTURE_ENTRY.phrasing_hints
    )


# ----------------------------------------------------------------------------- first-match rule


def test_first_matching_entry_wins_on_duplicate_pattern_ids() -> None:
    first = KBEntry(
        pattern_id="dup",
        description="first",
        gate_condition=GateCondition(metric="switch_rate", operator=">", threshold=0.6),
        phrasing_hints=("first hint",),
        autonomy_level="gentle_note",
        evidence_tag="fixture_source_2026",
        version=1,
    )
    second = KBEntry(
        pattern_id="dup",
        description="second",
        gate_condition=GateCondition(metric="window_count", operator=">", threshold=1.0),
        phrasing_hints=("second hint",),
        autonomy_level="gentle_note",
        evidence_tag="fixture_source_2026",
        version=1,
    )

    assert extract_phrasing_hints("dup", [first, second]) == ["first hint"]
