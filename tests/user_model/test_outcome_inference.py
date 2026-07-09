"""TK-50 acceptance criteria — outcome_inference (EP-12, Q-88 ruling).

  AC1 (flagged calendar conflict's event(s) gone from the latest snapshot -> LOAD_BEARING,
      source=inferred, provenance-bearing rule_name; the engine performs zero writes/network/IO):
      ``test_ac1_...``.
  AC2 (no feedback + no inferable downstream change -> IGNORED, source=inferred — one reachable
      outcome among three, not the only one): ``test_ac2_...``.
  AC3 (TK-51's fold-proof: explicit feedback beats a contradicting weaker inference; useful ->
      LOAD_BEARING, not_useful -> REGRETTED, both source=feedback): ``test_ac3_...``.
  AC4 (non-degenerate gradient: a fixture of >=20 resolved items -> >=1 LOAD_BEARING, >=1
      REGRETTED, >=1 IGNORED, one signal per item_ref, deterministic across two runs):
      ``test_ac4_...``.
  AC5 (structural guards: both-directions no-import AST test + no-LLM import scan):
      ``test_ac5_...``.

No DSN, no framework gating, no clock — pure-unit tests over directly-constructed input records
(Q-66/Q-84/Q-88 precedent).
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime
from pathlib import Path

import pytest

from wombat.user_model.feedback_source import FeedbackSignal
from wombat.user_model.outcome_inference import (
    RULE_DRAFT_DELETED_UNSENT,
    RULE_EXPLICIT_FEEDBACK_NOT_USEFUL,
    RULE_EXPLICIT_FEEDBACK_USEFUL,
    RULE_FLAGGED_CONFLICT_DISAPPEARED,
    RULE_IGNORED_DEFAULT,
    CalendarSnapshotDelta,
    DraftFate,
    ItemResolution,
    Outcome,
    OutcomeSignal,
    infer_outcomes,
)

_NOW = datetime(2026, 7, 9, 12, 0, tzinfo=UTC)

_SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "wombat"
_OUTCOME_INFERENCE_PATH = _SRC_ROOT / "user_model" / "outcome_inference.py"
_OUTCOME_INFERENCE_MODULE_NAME = "wombat.user_model.outcome_inference"

# The drain-spine modules the S1 off-path guarantee covers (Q-66/Q-84/Q-88 precedent) — none of
# these may ever import wombat.user_model.outcome_inference.
_DRAIN_SPINE_PATHS: tuple[Path, ...] = (
    _SRC_ROOT / "stages" / "drain_queue.py",
    _SRC_ROOT / "stages" / "gate_stage.py",
    _SRC_ROOT / "stages" / "review_or_speak.py",
    _SRC_ROOT / "stages" / "compose_dispatch_router.py",
    _SRC_ROOT / "stages" / "compose.py",
    *sorted((_SRC_ROOT / "gate").glob("*.py")),
)

# Q-88's closed no-import list: no LLM model/provider/HTTP module, no compose/stages module
# reachable from this module — a pure function cannot place an HTTP call it cannot import.
_FORBIDDEN_IMPORT_PREFIXES = (
    "openai",
    "httpx",
    "requests",
    "cogworx.model",
    "wombat.compose",
    "wombat.stages",
)


def _resolution(
    item_ref: str,
    *,
    disposition: str = "surfaced",
    ttl_expired: bool = True,
) -> ItemResolution:
    return ItemResolution(
        item_ref=item_ref,
        disposition=disposition,  # type: ignore[arg-type]
        resolved_at=_NOW,
        ttl_expired=ttl_expired,
    )


def _imported_module_names(source: str) -> set[str]:
    """AST-based import scan (Q-84/Q-88 wording): every module name this source ``import``s or
    ``from``-imports, absolute (level-0) imports only."""
    tree = ast.parse(source)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module is not None and node.level == 0:
            names.add(node.module)
    return names


# --- AC1 --------------------------------------------------------------------------------


def test_ac1_flagged_conflict_disappeared_from_latest_snapshot_yields_load_bearing() -> None:
    resolution = _resolution("cal-evt-1")
    delta = CalendarSnapshotDelta(
        item_ref="cal-evt-1",
        flagged_event_ids=("evt-conflict-1",),
        event_ids_in_latest_snapshot=("evt-unrelated-2",),
    )

    [signal] = infer_outcomes([resolution], calendar_deltas=[delta])

    assert signal.item_ref == "cal-evt-1"
    assert signal.outcome is Outcome.LOAD_BEARING
    assert signal.source == "inferred"
    assert signal.rule_name == RULE_FLAGGED_CONFLICT_DISAPPEARED


def test_ac1_engine_takes_and_returns_only_plain_values_no_io() -> None:
    """The engine performs zero writes/network/IO: it is a pure function over plain, frozen
    input values, returning a plain tuple — the import guard (AC5) is the structural proof that
    no I/O-capable module is even reachable from this one."""
    resolution = _resolution("cal-evt-2")
    delta = CalendarSnapshotDelta(
        item_ref="cal-evt-2",
        flagged_event_ids=("evt-conflict-2",),
        event_ids_in_latest_snapshot=(),
    )

    result = infer_outcomes([resolution], calendar_deltas=[delta])

    assert isinstance(result, tuple)
    assert result[0].outcome is Outcome.LOAD_BEARING


def test_ac1_conflicting_event_still_present_does_not_fire_the_rule() -> None:
    """Companion negative case: the flagged event is STILL in the latest snapshot, so the rule
    must not fire and the item falls through to the IGNORED default."""
    resolution = _resolution("cal-evt-3")
    delta = CalendarSnapshotDelta(
        item_ref="cal-evt-3",
        flagged_event_ids=("evt-conflict-3",),
        event_ids_in_latest_snapshot=("evt-conflict-3",),
    )

    [signal] = infer_outcomes([resolution], calendar_deltas=[delta])

    assert signal.outcome is Outcome.IGNORED
    assert signal.rule_name == RULE_IGNORED_DEFAULT


# --- AC2 --------------------------------------------------------------------------------


def test_ac2_no_feedback_and_no_inferable_change_yields_ignored_default() -> None:
    resolution = _resolution("item-no-signal")

    [signal] = infer_outcomes([resolution])

    assert signal.item_ref == "item-no-signal"
    assert signal.outcome is Outcome.IGNORED
    assert signal.source == "inferred"
    assert signal.rule_name == RULE_IGNORED_DEFAULT


def test_ac2_ignored_is_one_reachable_outcome_among_three_not_the_only_one() -> None:
    """Proves IGNORED is not the only reachable outcome: the same call produces LOAD_BEARING
    for a different item_ref given a firing inference rule."""
    resolutions = [_resolution("item-default"), _resolution("item-conflict-gone")]
    delta = CalendarSnapshotDelta(
        item_ref="item-conflict-gone",
        flagged_event_ids=("evt-x",),
        event_ids_in_latest_snapshot=(),
    )

    signals = infer_outcomes(resolutions, calendar_deltas=[delta])
    outcomes = {signal.item_ref: signal.outcome for signal in signals}

    assert outcomes["item-default"] is Outcome.IGNORED
    assert outcomes["item-conflict-gone"] is Outcome.LOAD_BEARING


# --- AC3 --------------------------------------------------------------------------------


def test_ac3_explicit_useful_feedback_beats_a_contradicting_ignored_default() -> None:
    resolution = _resolution("item-feedback-useful")
    feedback = FeedbackSignal(item_ref="item-feedback-useful", response="useful")

    # No calendar delta, no draft fate injected — the weaker inference here is the IGNORED
    # default, which explicit feedback must override.
    [signal] = infer_outcomes([resolution], feedback=[feedback])

    assert signal.outcome is Outcome.LOAD_BEARING
    assert signal.source == "feedback"
    assert signal.rule_name == RULE_EXPLICIT_FEEDBACK_USEFUL


def test_ac3_explicit_not_useful_feedback_yields_regretted() -> None:
    resolution = _resolution("item-feedback-not-useful")
    feedback = FeedbackSignal(item_ref="item-feedback-not-useful", response="not_useful")

    [signal] = infer_outcomes([resolution], feedback=[feedback])

    assert signal.outcome is Outcome.REGRETTED
    assert signal.source == "feedback"
    assert signal.rule_name == RULE_EXPLICIT_FEEDBACK_NOT_USEFUL


def test_ac3_explicit_feedback_beats_a_contradicting_stronger_inference() -> None:
    """The fold-proof at full strength: feedback overrides even a FIRING inference rule that
    would otherwise have produced a different outcome (LOAD_BEARING via the calendar rule),
    proving precedence is feedback > inference > default, not merely feedback > default."""
    resolution = _resolution("item-feedback-overrides-inference")
    feedback = FeedbackSignal(item_ref="item-feedback-overrides-inference", response="not_useful")
    delta = CalendarSnapshotDelta(
        item_ref="item-feedback-overrides-inference",
        flagged_event_ids=("evt-y",),
        event_ids_in_latest_snapshot=(),
    )

    [signal] = infer_outcomes([resolution], feedback=[feedback], calendar_deltas=[delta])

    assert signal.outcome is Outcome.REGRETTED
    assert signal.source == "feedback"
    assert signal.rule_name == RULE_EXPLICIT_FEEDBACK_NOT_USEFUL


def test_draft_deleted_unsent_yields_regretted_via_inference() -> None:
    """Rule (b), the other inference rule: a drafted reply deleted unsent -> REGRETTED."""
    resolution = _resolution("item-draft")
    fate = DraftFate(item_ref="item-draft", draft_created=True, deleted_unsent=True)

    [signal] = infer_outcomes([resolution], draft_fates=[fate])

    assert signal.outcome is Outcome.REGRETTED
    assert signal.source == "inferred"
    assert signal.rule_name == RULE_DRAFT_DELETED_UNSENT


def test_draft_created_but_not_deleted_does_not_fire_the_regretted_rule() -> None:
    resolution = _resolution("item-draft-kept")
    fate = DraftFate(item_ref="item-draft-kept", draft_created=True, deleted_unsent=False)

    [signal] = infer_outcomes([resolution], draft_fates=[fate])

    assert signal.outcome is Outcome.IGNORED
    assert signal.rule_name == RULE_IGNORED_DEFAULT


# --- AC4 --------------------------------------------------------------------------------


def _build_fixture() -> tuple[
    list[ItemResolution], list[FeedbackSignal], list[CalendarSnapshotDelta], list[DraftFate]
]:
    """>=20 resolved items spanning feedback-carrying and inference-only cases."""
    resolutions: list[ItemResolution] = []
    feedback: list[FeedbackSignal] = []
    calendar_deltas: list[CalendarSnapshotDelta] = []
    draft_fates: list[DraftFate] = []

    # 6 explicit-feedback items: 3 useful (-> LOAD_BEARING), 3 not_useful (-> REGRETTED).
    for i in range(3):
        item_ref = f"fb-useful-{i}"
        resolutions.append(_resolution(item_ref))
        feedback.append(FeedbackSignal(item_ref=item_ref, response="useful"))
    for i in range(3):
        item_ref = f"fb-not-useful-{i}"
        resolutions.append(_resolution(item_ref))
        feedback.append(FeedbackSignal(item_ref=item_ref, response="not_useful"))

    # 5 inference-only calendar-conflict-disappeared items (-> LOAD_BEARING).
    for i in range(5):
        item_ref = f"cal-gone-{i}"
        resolutions.append(_resolution(item_ref))
        calendar_deltas.append(
            CalendarSnapshotDelta(
                item_ref=item_ref,
                flagged_event_ids=(f"evt-{i}",),
                event_ids_in_latest_snapshot=(),
            )
        )

    # 5 inference-only draft-deleted-unsent items (-> REGRETTED).
    for i in range(5):
        item_ref = f"draft-deleted-{i}"
        resolutions.append(_resolution(item_ref))
        draft_fates.append(
            DraftFate(item_ref=item_ref, draft_created=True, deleted_unsent=True)
        )

    # 7 no-signal items (-> IGNORED default). Total = 3+3+5+5+7 = 23 >= 20.
    for i in range(7):
        item_ref = f"no-signal-{i}"
        resolutions.append(_resolution(item_ref))

    return resolutions, feedback, calendar_deltas, draft_fates


def test_ac4_fixture_of_20_plus_items_yields_a_non_degenerate_gradient() -> None:
    resolutions, feedback, calendar_deltas, draft_fates = _build_fixture()
    assert len(resolutions) >= 20

    signals = infer_outcomes(
        resolutions,
        feedback=feedback,
        calendar_deltas=calendar_deltas,
        draft_fates=draft_fates,
    )

    assert len(signals) == len(resolutions)
    item_refs = [signal.item_ref for signal in signals]
    assert len(item_refs) == len(set(item_refs))  # exactly one signal per item_ref

    outcome_counts = {outcome: 0 for outcome in Outcome}
    for signal in signals:
        outcome_counts[signal.outcome] += 1

    assert outcome_counts[Outcome.LOAD_BEARING] >= 1
    assert outcome_counts[Outcome.REGRETTED] >= 1
    assert outcome_counts[Outcome.IGNORED] >= 1


def test_ac4_deterministic_across_two_runs() -> None:
    resolutions, feedback, calendar_deltas, draft_fates = _build_fixture()

    first_run = infer_outcomes(
        resolutions, feedback=feedback, calendar_deltas=calendar_deltas, draft_fates=draft_fates
    )
    second_run = infer_outcomes(
        resolutions, feedback=feedback, calendar_deltas=calendar_deltas, draft_fates=draft_fates
    )

    assert first_run == second_run


# --- AC5: structural guards -----------------------------------------------------------------


def test_ac5_no_forbidden_llm_provider_or_drain_spine_import_in_outcome_inference() -> None:
    """Structural proof: outcome_inference.py imports NO model/provider/HTTP/compose/stages
    module — a pure function cannot place a call it cannot import."""
    source = _OUTCOME_INFERENCE_PATH.read_text(encoding="utf-8")
    imported = _imported_module_names(source)
    offenders = [
        name
        for name in imported
        if any(
            name == prefix or name.startswith(prefix + ".") for prefix in _FORBIDDEN_IMPORT_PREFIXES
        )
    ]
    assert not offenders, f"outcome_inference.py imports forbidden module(s): {offenders}"


def _references_outcome_inference_module(text: str) -> bool:
    return (
        _OUTCOME_INFERENCE_MODULE_NAME in text
        or "user_model import outcome_inference" in text
    )


def test_ac5_no_drain_spine_module_imports_outcome_inference() -> None:
    offenders = [
        str(path)
        for path in _DRAIN_SPINE_PATHS
        if _references_outcome_inference_module(path.read_text(encoding="utf-8"))
    ]
    assert not offenders, (
        f"drain-spine module(s) import wombat.user_model.outcome_inference, breaking the S1 "
        f"off-path guarantee: {offenders}"
    )


def test_ac5_outcome_inference_does_not_import_any_drain_spine_module() -> None:
    """The other direction: outcome_inference.py itself imports no drain-spine module."""
    imported = _imported_module_names(_OUTCOME_INFERENCE_PATH.read_text(encoding="utf-8"))
    drain_spine_module_names = {
        f"wombat.stages.{path.stem}"
        for path in _DRAIN_SPINE_PATHS
        if path.parent.name == "stages"
    } | {f"wombat.gate.{path.stem}" for path in _DRAIN_SPINE_PATHS if path.parent.name == "gate"}
    offenders = imported & drain_spine_module_names
    assert not offenders, f"outcome_inference.py imports drain-spine module(s): {offenders}"


def test_ac5_drain_spine_path_list_is_non_empty_and_every_path_exists() -> None:
    assert len(_DRAIN_SPINE_PATHS) >= 6
    for path in _DRAIN_SPINE_PATHS:
        assert path.exists(), f"drain-spine guard path does not exist: {path}"


def test_ac5_guard_is_load_bearing_it_would_fail_if_the_import_were_added() -> None:
    """Proves the guard actually detects the forbidden import (without mutating any real
    drain-spine file, out of this ticket's files_in_scope): feed the same detection predicate a
    synthetic drain-spine-shaped source that gained the import, and assert it is flagged; a real,
    clean file is not."""
    hypothetical_offending_source = (
        "from __future__ import annotations\n\n"
        "from wombat.user_model.outcome_inference import infer_outcomes\n\n"
        "def run() -> None:\n"
        "    pass\n"
    )
    assert _references_outcome_inference_module(hypothetical_offending_source)

    clean_source = (_SRC_ROOT / "stages" / "drain_queue.py").read_text(encoding="utf-8")
    assert not _references_outcome_inference_module(clean_source)


# --- OutcomeSignal / ItemResolution invariants ------------------------------------------------


def test_outcome_signal_rejects_a_raw_string_outcome_at_runtime() -> None:
    with pytest.raises(TypeError):
        OutcomeSignal(
            item_ref="x",
            outcome="load_bearing",  # type: ignore[arg-type]
            source="inferred",
            rule_name=RULE_IGNORED_DEFAULT,
        )


def test_item_resolution_rejects_naive_resolved_at() -> None:
    with pytest.raises(ValueError, match="naive"):
        ItemResolution(
            item_ref="x",
            disposition="surfaced",
            resolved_at=datetime(2026, 7, 9, 12, 0),
            ttl_expired=True,
        )


def test_outcome_is_a_closed_enum_of_exactly_three_members() -> None:
    assert {member.name for member in Outcome} == {"LOAD_BEARING", "REGRETTED", "IGNORED"}
