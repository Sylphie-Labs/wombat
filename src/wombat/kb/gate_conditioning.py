"""pattern_warrants_nudge — pure, deterministic, evidence-based nudge gating (TK-116, EP-23).

NO model anywhere (NG-4/CON-1): this module is a plain comparison over KB-declared thresholds.
No I/O, no clock, no logging side-channel decisions — same inputs always yield the same output.

TK-113 calls ``pattern_warrants_nudge(metrics, kb)`` before ever enqueueing a nudge, and finds
WHICH pattern matched by iterating KB entries in file order and calling this function with a
single-entry ``kb`` per entry (Q-99h) — the bool signature below is the ruled seam and must not
change to return a ``pattern_id``.
"""

from __future__ import annotations

import operator as operator_module
from collections.abc import Callable, Mapping, Sequence

from wombat.kb.schema import KBEntry

# Closed v1 operator vocabulary (Q-99b), validated at KB load time (TK-115) — every KBEntry this
# module ever sees is assumed to already carry an operator from this set.
_OPERATORS: dict[str, Callable[[float, float], bool]] = {
    ">": operator_module.gt,
    ">=": operator_module.ge,
    "<": operator_module.lt,
    "<=": operator_module.le,
    "==": operator_module.eq,
}


def pattern_warrants_nudge(metrics: Mapping[str, float], kb: Sequence[KBEntry]) -> bool:
    """True iff ANY ``kb`` entry's ``gate_condition`` matches ``metrics`` (Q-99b/h).

    A metric key absent from ``metrics`` means that entry's condition is NOT satisfied (CON-3
    quiet default — never raise). An empty ``kb`` (e.g. a load that returned no entries) always
    returns False: absence of KB means no nudges.
    """
    for entry in kb:
        condition = entry.gate_condition
        if condition.metric not in metrics:
            continue
        compare = _OPERATORS[condition.operator]
        if compare(metrics[condition.metric], condition.threshold):
            return True
    return False
