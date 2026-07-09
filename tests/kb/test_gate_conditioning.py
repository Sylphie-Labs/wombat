"""TK-116 — pattern_warrants_nudge acceptance criteria (EP-23, Q-99b/h).

  AC1 a metrics dict + a KB entry whose gate_condition matches -> True.
  AC2 metrics satisfying NO entry's condition -> False, including the missing-metric-key case.
  AC3 kb == [] -> False (CON-3 safe default).
  AC4 10 diverse metric dicts against the REAL seed KB (load_psychology_kb()): >=3 True, >=3
      False, and calling twice gives identical results (determinism).

Also covers every operator in the closed set ({'>', '>=', '<', '<=', '=='}) once, parametrized.
"""

from __future__ import annotations

import pytest

from wombat.kb.gate_conditioning import pattern_warrants_nudge
from wombat.kb.loader import load_psychology_kb
from wombat.kb.schema import GateCondition, KBEntry

_FIXTURE_ENTRY = KBEntry(
    pattern_id="fixture_pattern",
    description="A fixture pattern used only by tests.",
    gate_condition=GateCondition(metric="switch_rate", operator=">", threshold=0.6),
    phrasing_hints=("a fixture hint",),
    autonomy_level="gentle_note",
    evidence_tag="fixture_source_2026",
    version=1,
)


def _entry_with_condition(metric: str, op: str, threshold: float) -> KBEntry:
    return KBEntry(
        pattern_id="fixture_pattern",
        description="A fixture pattern used only by tests.",
        gate_condition=GateCondition(metric=metric, operator=op, threshold=threshold),
        phrasing_hints=("a fixture hint",),
        autonomy_level="gentle_note",
        evidence_tag="fixture_source_2026",
        version=1,
    )


# --------------------------------------------------------------------------------------- AC1


def test_ac1_matching_entry_returns_true() -> None:
    assert pattern_warrants_nudge({"switch_rate": 0.8}, [_FIXTURE_ENTRY]) is True


# --------------------------------------------------------------------------------------- AC2


def test_ac2_no_matching_entry_returns_false() -> None:
    assert pattern_warrants_nudge({"switch_rate": 0.3}, [_FIXTURE_ENTRY]) is False


def test_ac2_missing_metric_key_returns_false() -> None:
    assert pattern_warrants_nudge({"window_count": 10.0}, [_FIXTURE_ENTRY]) is False


def test_ac2_empty_metrics_returns_false() -> None:
    assert pattern_warrants_nudge({}, [_FIXTURE_ENTRY]) is False


# --------------------------------------------------------------------------------------- AC3


def test_ac3_empty_kb_returns_false() -> None:
    assert pattern_warrants_nudge({"switch_rate": 0.9}, []) is False


# --------------------------------------------------------------------------------------- AC4

_REAL_KB_FIXTURES: tuple[dict[str, float], ...] = (
    {"switch_rate": 0.8, "window_count": 3.0, "event_count": 10.0},  # switch_rate>0.6 -> True
    {"switch_rate": 0.3, "window_count": 8.0, "event_count": 10.0},  # window_count>=6 -> True
    {"switch_rate": 0.3, "window_count": 4.0, "event_count": 40.0},  # event_count>35 -> True
    {"switch_rate": 0.3, "window_count": 1.0, "event_count": 10.0},  # window_count<=1 -> True
    {"switch_rate": 0.1, "window_count": 4.0, "event_count": 10.0},  # switch_rate<0.15 -> True
    {"switch_rate": 0.3, "window_count": 4.0, "event_count": 2.0},  # event_count<5 -> True
    {"switch_rate": 0.3, "window_count": 4.0, "event_count": 10.0},  # nothing matches -> False
    {"switch_rate": 0.5, "window_count": 4.0, "event_count": 20.0},  # nothing matches -> False
    {},  # every metric missing -> False
    {"switch_rate": 0.6, "window_count": 5.0, "event_count": 35.0},  # all at/near boundary -> False
)


def test_ac4_real_seed_kb_diverse_metrics_and_determinism() -> None:
    kb = load_psychology_kb()
    assert len(_REAL_KB_FIXTURES) == 10

    first_pass = [pattern_warrants_nudge(metrics, kb) for metrics in _REAL_KB_FIXTURES]
    second_pass = [pattern_warrants_nudge(metrics, kb) for metrics in _REAL_KB_FIXTURES]

    assert first_pass == second_pass, "pattern_warrants_nudge must be deterministic"
    assert first_pass.count(True) >= 3
    assert first_pass.count(False) >= 3


# --------------------------------------------------------------------------------- operators


@pytest.mark.parametrize(
    ("op", "value", "threshold", "expected"),
    [
        (">", 0.7, 0.6, True),
        (">=", 0.6, 0.6, True),
        ("<", 0.1, 0.15, True),
        ("<=", 1.0, 1.0, True),
        ("==", 3.0, 3.0, True),
    ],
)
def test_every_closed_operator_is_applied(
    op: str, value: float, threshold: float, expected: bool
) -> None:
    entry = _entry_with_condition("switch_rate", op, threshold)
    assert pattern_warrants_nudge({"switch_rate": value}, [entry]) is expected
