"""TK-23 (EP-7) — production tests for the hardened urgency()/cognitive_load() over RatingParams.

Covers the v0.32 acceptance criteria:

* AC1 — purity + Q-42 composition (clamp01(base + gain*raw), range, determinism, no clock/I/O).
* AC2 — strict monotonicity in time-to-event for gain>0 (near-term scores strictly higher).
* AC3 — payload totality: MISSING key -> silent default; present-but-INVALID -> default + WARNING;
  never raises.
* AC4 — parameter variation: different RatingParams shift the score as expected; the committed
  builder feeds the fixture in CI, the gitignored real fixture only when locally present.

The TK-22 spike tests (behavioral-equivalence port under identity params) live in
``test_scoring_spike.py``; this file adds the production-hardening coverage on top.
"""

from __future__ import annotations

import ast
import importlib.util
import inspect
import json
import logging
import math
from pathlib import Path
from typing import Any

import pytest

from wombat.gate import scoring
from wombat.gate.models import GateItem, ItemKind
from wombat.gate.scoring import (
    TIME_HORIZON_S,
    W_DENSITY,
    W_DEPTH,
    W_SENDER,
    W_TIME,
    cognitive_load,
    urgency,
)
from wombat.rating.params import EventClass, RatingParams, default_params_for

FIXTURE = Path(__file__).parent / "fixtures" / "scoring_fixture.real.yaml"

# base=0, gain=1 -> the hardened scorers return the raw signal itself (identity composition).
_IDENTITY = RatingParams(urgency_base=0.0, urgency_gain=1.0, load_base=0.0, load_gain=1.0)


def _item(**payload: Any) -> GateItem:
    return GateItem(item_id="t", item_kind=ItemKind.GENERIC, created_at=0.0, payload=payload)


# --------------------------------------------------------------------------------------------
# AC1 — purity + Q-42 composition.
# --------------------------------------------------------------------------------------------


def test_ac1_composition_matches_clamp01_base_plus_gain_times_raw() -> None:
    """urgency/load == clamp01(base + gain*raw); the raw is the identity-params score (Q-42)."""
    it = _item(
        is_timed=True,
        seconds_to_event=600.0,  # 10 min out -> time_term = 1 - 600/14400
        sender_class="vip",  # sender_term = 1.0
        meeting_density=3.0,  # density_term = 3/6 = 0.5
        thread_depth=4,  # depth_term = 4/8 = 0.5
    )
    raw_urgency = urgency(it, _IDENTITY)
    raw_load = cognitive_load(it, _IDENTITY)

    # Independently recompute the raw signals from the frozen constants.
    time_term = 1.0 - (600.0 / TIME_HORIZON_S)
    expected_raw_urgency = W_TIME * time_term + W_SENDER * 1.0
    expected_raw_load = W_DENSITY * 0.5 + W_DEPTH * 0.5
    assert math.isclose(raw_urgency, expected_raw_urgency)
    assert math.isclose(raw_load, expected_raw_load)

    params = RatingParams(urgency_base=0.2, urgency_gain=0.5, load_base=0.1, load_gain=0.7)
    assert math.isclose(urgency(it, params), 0.2 + 0.5 * raw_urgency)
    assert math.isclose(cognitive_load(it, params), 0.1 + 0.7 * raw_load)


def test_ac1_output_in_unit_interval_and_clamped() -> None:
    """base+gain*raw can exceed 1.0; the clamp keeps the output in [0,1]."""
    it = _item(is_timed=True, seconds_to_event=0.0, sender_class="vip",
               meeting_density=99.0, thread_depth=99)
    saturating = RatingParams(urgency_base=0.9, urgency_gain=1.0, load_base=0.9, load_gain=1.0)
    assert urgency(it, saturating) == 1.0
    assert cognitive_load(it, saturating) == 1.0
    # And never below 0.
    for params in (_IDENTITY, saturating, default_params_for(EventClass.CALENDAR_CONFLICT)):
        assert 0.0 <= urgency(_item(), params) <= 1.0
        assert 0.0 <= cognitive_load(_item(), params) <= 1.0


def test_ac1_deterministic_same_input_same_output() -> None:
    it = _item(is_timed=True, seconds_to_event=1200.0, sender_class="known_human",
               meeting_density=2.0, thread_depth=3)
    p = default_params_for(EventClass.DRAFT_REPLY)
    assert urgency(it, p) == urgency(it, p)
    assert cognitive_load(it, p) == cognitive_load(it, p)


def test_ac1_source_imports_no_clock_or_network_or_randomness() -> None:
    """Purity by source inspection: the module imports no clock/network/randomness source.

    Inspect the actual import statements (not prose) so the docstring may still *mention*
    ``time.time()`` while proving the code never imports a clock, network client, or RNG.
    """
    tree = ast.parse(inspect.getsource(scoring))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    for forbidden in ("time", "datetime", "random", "requests", "httpx", "os"):
        assert forbidden not in imported, f"scoring.py must not import {forbidden!r} (AC1 purity)"
    assert "open(" not in inspect.getsource(scoring).replace("# ", "")


# --------------------------------------------------------------------------------------------
# AC2 — strict monotonicity in time-to-event (gain > 0).
# --------------------------------------------------------------------------------------------


def _pair() -> tuple[GateItem, GateItem]:
    base = {"is_timed": True, "sender_class": "vip", "meeting_density": 0.0, "thread_depth": 0}
    near = GateItem("near", ItemKind.GENERIC, 0.0, {**base, "seconds_to_event": 20 * 60.0})
    far = GateItem("far", ItemKind.GENERIC, 0.0, {**base, "seconds_to_event": 5 * 3600.0})
    return near, far


def test_ac2_near_term_strictly_higher_for_all_default_classes() -> None:
    """Every documented per-class default (gain>0): <30min beats >4h strictly (no clamp mask)."""
    near, far = _pair()
    for ec in EventClass:
        params = default_params_for(ec)
        assert params.urgency_gain > 0.0  # all documented defaults have positive gain
        assert urgency(near, params) > urgency(far, params), f"monotonicity fails for {ec}"


def test_ac2_gain_zero_is_degenerate_flat() -> None:
    """gain=0 flattens the class to its base — the documented degenerate case, not monotone."""
    near, far = _pair()
    muted = RatingParams(urgency_base=0.4, urgency_gain=0.0, load_base=0.5, load_gain=0.0)
    assert urgency(near, muted) == urgency(far, muted) == 0.4


# --------------------------------------------------------------------------------------------
# AC3 — payload totality: missing -> silent default; invalid -> default + WARNING; never raises.
# --------------------------------------------------------------------------------------------


def test_ac3_missing_keys_default_silently(caplog: pytest.LogCaptureFixture) -> None:
    """A wholly-sparse item scores without raising and WITHOUT any warning (sparse is legit)."""
    with caplog.at_level(logging.WARNING, logger="wombat.gate.scoring"):
        u = urgency(_item(), _IDENTITY)
        load = cognitive_load(_item(), _IDENTITY)
    assert 0.0 <= u <= 1.0 and 0.0 <= load <= 1.0
    # is_timed defaults False -> no time term; sender defaults automated; density/depth -> 0.
    assert math.isclose(u, W_SENDER * 0.1)  # automated priority
    assert load == 0.0
    assert caplog.records == []


def test_ac3_invalid_sender_class_defaults_with_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="wombat.gate.scoring"):
        u = urgency(_item(is_timed=False, sender_class="not_a_real_class"), _IDENTITY)
    # Falls back to automated priority (0.1).
    assert math.isclose(u, W_SENDER * 0.1)
    assert any("sender_class" in r.message for r in caplog.records)


def test_ac3_invalid_numeric_values_default_with_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="wombat.gate.scoring"):
        u = urgency(_item(is_timed=True, seconds_to_event="soon", sender_class="self"), _IDENTITY)
        load = cognitive_load(
            _item(meeting_density="lots", thread_depth="deep"), _IDENTITY
        )
    # seconds_to_event defaults to TIME_HORIZON_S -> zero time pressure.
    assert math.isclose(u, W_SENDER * _sender_weight_self())
    assert load == 0.0
    messages = " ".join(r.message for r in caplog.records)
    assert "seconds_to_event" in messages
    assert "meeting_density" in messages
    assert "thread_depth" in messages


def _sender_weight_self() -> float:
    from wombat.gate.scoring import _SENDER_PRIORITY, SenderClass

    return _SENDER_PRIORITY[SenderClass.SELF]


def test_ac3_never_raises_on_garbage_payload() -> None:
    """No combination of garbage payload values propagates an exception to the pipeline."""
    garbage = _item(
        is_timed=True,
        seconds_to_event=object(),
        sender_class=12345,
        meeting_density=[1, 2],
        thread_depth=None,
    )
    assert 0.0 <= urgency(garbage, _IDENTITY) <= 1.0
    assert 0.0 <= cognitive_load(garbage, _IDENTITY) <= 1.0


# --------------------------------------------------------------------------------------------
# AC4 — parameter variation + fixture agreement (no hard dependency on the gitignored file).
# --------------------------------------------------------------------------------------------


def test_ac4_parameter_variation_shifts_scores_as_expected() -> None:
    """Different RatingParams shift the score: higher base/gain -> higher score (composition)."""
    it = _item(is_timed=True, seconds_to_event=600.0, sender_class="vip",
               meeting_density=3.0, thread_depth=4)
    low = RatingParams(urgency_base=0.0, urgency_gain=0.2, load_base=0.0, load_gain=0.2)
    high = RatingParams(urgency_base=0.3, urgency_gain=0.9, load_base=0.3, load_gain=0.9)
    assert urgency(it, high) > urgency(it, low)
    assert cognitive_load(it, high) > cognitive_load(it, low)

    # Raising base alone lifts the score by exactly the base delta (below clamp).
    p0 = RatingParams(urgency_base=0.1, urgency_gain=0.5, load_base=0.1, load_gain=0.5)
    p1 = RatingParams(urgency_base=0.3, urgency_gain=0.5, load_base=0.3, load_gain=0.5)
    assert math.isclose(urgency(it, p1) - urgency(it, p0), 0.2)
    assert math.isclose(cognitive_load(it, p1) - cognitive_load(it, p0), 0.2)


def _load_fixture_items() -> list[dict[str, Any]]:
    """Load the committed-builder fixture (regenerating if the gitignored real file is absent).

    AC4 residency: production tests MUST NOT hard-depend on the gitignored real-data file. The
    de-identified builder is committed and regenerates the fixture in CI; the real file is used
    only when locally present.
    """
    if not FIXTURE.exists():
        builder_path = FIXTURE.parent / "_build_scoring_fixture.py"
        if not builder_path.exists():
            pytest.skip("neither the real fixture nor the committed builder is present")
        spec = importlib.util.spec_from_file_location("_build_scoring_fixture", builder_path)
        assert spec is not None and spec.loader is not None
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        FIXTURE.write_text(json.dumps(builder.build(), indent=2), encoding="utf-8")
    data: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    items: list[dict[str, Any]] = data["items"]
    return items


def test_ac4_fixture_scores_stay_in_range_under_a_real_class_default() -> None:
    """Every fixture item scores in [0,1] under a documented per-class default (not identity)."""
    items = _load_fixture_items()
    assert len(items) >= 40
    params = default_params_for(EventClass.GENERIC)
    for row in items:
        gi = GateItem(row["item_id"], ItemKind.GENERIC, 0.0, dict(row["features"]))
        assert 0.0 <= urgency(gi, params) <= 1.0
        assert 0.0 <= cognitive_load(gi, params) <= 1.0
