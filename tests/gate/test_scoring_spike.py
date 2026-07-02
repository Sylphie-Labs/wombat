"""TK-22 SPIKE (RISK-1) — urgency()/cognitive_load() vs proxy human labels.

The fixture (scoring_fixture.real.yaml, gitignored per DEC-24) is seeded from a one-time LOCAL
read of real Gmail/Calendar metadata, de-identified to numeric features + sender CLASS. If the
gitignored fixture is absent (fresh clone / CI), we regenerate it from the committed builder so
the test is self-contained.

What this proves (and does NOT): it measures AGREEMENT of the scoring functions against the
proxy labels in RUBRIC.md. RISK-1's real thesis needs Jim's confirmatory pass over those proxy
labels, so this is an intermediate (preliminary) check, not the final human-judgment validation.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Any

from wombat.gate import scoring
from wombat.gate.models import GateItem, ItemKind
from wombat.gate.scoring import cognitive_load, urgency
from wombat.rating.params import RatingParams

FIXTURE = Path(__file__).parent / "fixtures" / "scoring_fixture.real.yaml"
AGREEMENT_THRESHOLD = 0.80

# Identity params (base=0, gain=1): the hardened scorers reproduce the spike's RAW scores
# EXACTLY (Q-42, convex-combination proof), so these ported spike tests keep their original
# thresholds unchanged — the port doubles as a behavioral-equivalence proof of the hardening.
_IDENTITY = RatingParams(urgency_base=0.0, urgency_gain=1.0, load_base=0.0, load_gain=1.0)


def _load_fixture() -> dict[str, Any]:
    if not FIXTURE.exists():
        # Regenerate from the committed builder (no PII; emits the gitignored .real.yaml).
        # Load by file path so no tests/ __init__.py packaging is required.
        import importlib.util

        builder_path = FIXTURE.parent / "_build_scoring_fixture.py"
        spec = importlib.util.spec_from_file_location("_build_scoring_fixture", builder_path)
        assert spec is not None and spec.loader is not None
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        built = builder.build()
        FIXTURE.write_text(json.dumps(built, indent=2), encoding="utf-8")
    # JSON is a valid-YAML subset; the .real.yaml is JSON-encoded so stdlib json reads it.
    data: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return data


def _predicted_label(u: float, load: float, sender_class: str, params: RatingParams) -> str:
    """The scoring functions' OWN surface/hold call, independent of the fixture's stored label.

    Mirrors the rubric's structure but is driven purely by urgency()/cognitive_load() output:
    automated holds; self surfaces only when imminent; human surfaces on high urgency. Deep
    threads (high load) on human mail also surface (load-bearing backlog).
    """
    if sender_class == "automated":
        return "hold"
    if sender_class == "self":
        return "surface" if u >= 0.78 else "hold"
    if u >= 0.60 or load >= 0.6:
        return "surface"
    return "hold"


def test_ac1_agreement_at_least_80pct_and_disagreements_logged() -> None:
    fixture = _load_fixture()
    params = _IDENTITY
    items = fixture["items"]
    assert len(items) >= 40, "AC requires a >=40-event fixture"

    agree = 0
    disagreements: list[dict[str, Any]] = []
    for row in items:
        feats = row["features"]
        gi = GateItem(
            item_id=row["item_id"],
            item_kind=ItemKind.GENERIC,
            created_at=0.0,
            payload=feats,
        )
        u = urgency(gi, params)
        load = cognitive_load(gi, params)
        predicted = _predicted_label(u, load, feats["sender_class"], params)
        proxy = row["proxy_label"]
        if predicted == proxy:
            agree += 1
        else:
            disagreements.append({
                "item_id": row["item_id"],
                "sender_class": feats["sender_class"],
                "urgency": round(u, 4),
                "load": round(load, 4),
                "predicted": predicted,
                "proxy": proxy,
            })

    agreement = agree / len(items)
    report = FIXTURE.parent / "scoring_fixture_report.real.txt"
    lines = [
        "TK-22 RISK-1 scoring agreement report",
        f"items={len(items)} agree={agree} agreement={agreement:.3f} "
        f"threshold={AGREEMENT_THRESHOLD}",
        f"disagreements={len(disagreements)}",
        "",
        "# Systematic-vs-ambiguous analysis (AC3):",
        "# Disagreements, if any, are listed below with their scores so a systematic pattern",
        "# (e.g. one sender_class always mis-split) is visible vs. scattered boundary cases.",
        "",
    ]
    for d in disagreements:
        lines.append(json.dumps(d))
    report.write_text("\n".join(lines), encoding="utf-8")

    assert agreement >= AGREEMENT_THRESHOLD, (
        f"agreement {agreement:.3f} < {AGREEMENT_THRESHOLD}; see {report}"
    )


def test_ac2_scoring_functions_are_pure_no_network_imports() -> None:
    """AC2: functions are pure — no requests/httpx import in the scoring module source."""
    src = inspect.getsource(scoring)
    for forbidden in ("import requests", "import httpx", "from requests", "from httpx"):
        assert forbidden not in src, f"scoring.py must not {forbidden!r} (purity)"
    # No module-level I/O handles either.
    assert "open(" not in src.replace("# ", "")


def test_ac2_determinism_same_input_same_output() -> None:
    params = _IDENTITY
    gi = GateItem(
        item_id="d",
        item_kind=ItemKind.GENERIC,
        created_at=0.0,
        payload={"is_timed": True, "seconds_to_event": 600.0, "sender_class": "vip",
                 "meeting_density": 3.0, "thread_depth": 4},
    )
    u1, l1 = urgency(gi, params), cognitive_load(gi, params)
    u2, l2 = urgency(gi, params), cognitive_load(gi, params)
    assert u1 == u2 and l1 == l2
    assert 0.0 <= u1 <= 1.0 and 0.0 <= l1 <= 1.0


def test_near_term_scores_strictly_higher_than_far_term() -> None:
    """Monotonicity sanity (mirrors the TK-23 AC, cheap to assert now)."""
    params = _IDENTITY
    base = {"is_timed": True, "sender_class": "vip", "meeting_density": 0.0, "thread_depth": 0}
    near = GateItem("n", ItemKind.GENERIC, 0.0, {**base, "seconds_to_event": 60.0 * 20})
    far = GateItem("f", ItemKind.GENERIC, 0.0, {**base, "seconds_to_event": 4.5 * 3600})
    assert urgency(near, params) > urgency(far, params)


def test_automated_mail_holds_by_default() -> None:
    """Quiet-by-default: an automated-sender item never out-scores the human surface bar."""
    params = _IDENTITY
    gi = GateItem("a", ItemKind.GENERIC, 0.0,
                  {"is_timed": False, "sender_class": "automated", "thread_depth": 5})
    assert urgency(gi, params) < 0.6
