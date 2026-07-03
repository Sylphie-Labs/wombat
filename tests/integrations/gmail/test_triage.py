"""TK-76 acceptance criteria — GmailTriageEngine (EP-17, Q-66).

  AC1 (allowlisted sender -> HIGH, matched rule name present, wire round-trip):
      ``test_ac1_...``.
  AC2 (no matching rule -> NORMAL, empty matched_rules): ``test_ac2_...``.
  AC3 (data-driven: a NEW subject-keyword rule, added to the rules DATA with no code
      change, matches; companion proof triage never reads body_text): ``test_ac3_...``.
  AC4 (structural no-import guard over the drain-spine modules; load-bearing):
      ``test_ac4_...``.

No DSN, no framework gating, no clock — pure-unit tests over ``GmailMessageItem`` fixtures
constructed directly (Q-66 ruling 6).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from wombat.config import ConfigurationError
from wombat.integrations.gmail.models import GmailMessageItem
from wombat.integrations.gmail.triage import (
    PriorityBand,
    SenderAllowlistRule,
    SubjectKeywordRule,
    TriageResult,
    TriageRules,
    load_triage_rules,
    triage_message,
)

_NOW = datetime(2026, 7, 2, 12, 0, tzinfo=UTC)

_SRC_ROOT = Path(__file__).resolve().parents[3] / "src" / "wombat"
_SHIPPED_RULES_PATH = _SRC_ROOT / "integrations" / "gmail" / "triage_rules.yaml"

_TRIAGE_MODULE_NAME = "wombat.integrations.gmail.triage"

# The drain-spine modules the S1 off-path guarantee (Q-66 ruling 4) covers — none of these may
# ever import wombat.integrations.gmail.triage.
_DRAIN_SPINE_PATHS: tuple[Path, ...] = (
    _SRC_ROOT / "stages" / "drain_queue.py",
    _SRC_ROOT / "stages" / "gate_stage.py",
    _SRC_ROOT / "stages" / "review_or_speak.py",
    _SRC_ROOT / "stages" / "compose_dispatch_router.py",
    _SRC_ROOT / "stages" / "compose.py",
    *sorted((_SRC_ROOT / "gate").glob("*.py")),
)


def _item(
    *,
    message_id: str = "msg-1",
    subject: str = "hello",
    sender: str = "nobody@example.com",
    body_text: str = "",
) -> GmailMessageItem:
    return GmailMessageItem(
        message_id=message_id,
        subject=subject,
        sender=sender,
        received_at=_NOW,
        body_text=body_text,
    )


def _rules(
    *,
    version: int = 1,
    sender_allowlist_rules: tuple[SenderAllowlistRule, ...] = (),
    subject_keyword_rules: tuple[SubjectKeywordRule, ...] = (),
) -> TriageRules:
    return TriageRules(
        version=version,
        sender_allowlist_rules=sender_allowlist_rules,
        subject_keyword_rules=subject_keyword_rules,
    )


# --- AC1 --------------------------------------------------------------------------------


def test_ac1_allowlisted_sender_scores_high_with_matched_rule_name_and_round_trips() -> None:
    rules = _rules(
        sender_allowlist_rules=(
            SenderAllowlistRule(
                name="vip_sender_allowlist",
                senders=("boss@example.com",),
                urgency_score=0.9,
                priority_band=PriorityBand.HIGH,
            ),
        )
    )
    item = _item(message_id="msg-vip", sender="Boss Person <boss@example.com>")

    result = triage_message(item, rules)

    assert result.priority_band == PriorityBand.HIGH
    assert result.priority_band.value == "high"
    assert "vip_sender_allowlist" in result.matched_rules
    assert result.urgency_score == 0.9

    # Wire round-trip (Q-49): from_payload(to_payload(r)) == r, exactly.
    payload = result.to_payload()
    assert payload == {
        "message_id": "msg-vip",
        "urgency_score": 0.9,
        "priority_band": "high",
        "matched_rules": ["vip_sender_allowlist"],
    }
    assert TriageResult.from_payload(payload) == result


def test_shipped_triage_rules_yaml_produces_the_same_ac1_outcome() -> None:
    """The packaged triage_rules.yaml (not a hand-built fixture) also satisfies AC1."""
    rules = load_triage_rules()
    item = _item(message_id="msg-vip2", sender="boss@example.com", subject="fyi")

    result = triage_message(item, rules)

    assert result.priority_band == PriorityBand.HIGH
    assert "vip_sender_allowlist" in result.matched_rules


# --- AC2 --------------------------------------------------------------------------------


def test_ac2_no_matching_rule_scores_normal_with_empty_matched_rules() -> None:
    rules = _rules(
        sender_allowlist_rules=(
            SenderAllowlistRule(
                name="vip_sender_allowlist",
                senders=("boss@example.com",),
                urgency_score=0.9,
                priority_band=PriorityBand.HIGH,
            ),
        ),
        subject_keyword_rules=(
            SubjectKeywordRule(
                name="urgent_subject_keyword",
                keywords=("urgent",),
                urgency_score=0.8,
                priority_band=PriorityBand.HIGH,
            ),
        ),
    )
    item = _item(sender="stranger@example.com", subject="weekly newsletter")

    result = triage_message(item, rules)

    assert result.priority_band == PriorityBand.NORMAL
    assert result.priority_band.value == "normal"
    assert result.matched_rules == ()
    assert result.urgency_score == 0.0


def test_ac2_empty_rule_set_never_matches() -> None:
    result = triage_message(_item(), _rules())
    assert result.priority_band == PriorityBand.NORMAL
    assert result.matched_rules == ()


# --- AC3 --------------------------------------------------------------------------------


def test_ac3_new_subject_keyword_rule_added_to_rules_data_matches_with_no_code_change() -> None:
    """Data-driven proof: a rule that exists ONLY in the injected rules object (never
    hard-coded in triage.py) changes the outcome."""
    brand_new_rule = SubjectKeywordRule(
        name="brand_new_keyword_never_seen_by_code",
        keywords=("zzyzx-signal",),
        urgency_score=0.55,
        priority_band=PriorityBand.HIGH,
    )
    rules = _rules(subject_keyword_rules=(brand_new_rule,))

    matching_item = _item(subject="re: zzyzx-signal detected")
    result = triage_message(matching_item, rules)

    assert result.priority_band == PriorityBand.HIGH
    assert result.matched_rules == ("brand_new_keyword_never_seen_by_code",)
    assert result.urgency_score == 0.55

    # And the same rule set does NOT match a subject lacking the keyword.
    non_matching_item = _item(subject="unrelated subject")
    other_result = triage_message(non_matching_item, rules)
    assert other_result.priority_band == PriorityBand.NORMAL
    assert other_result.matched_rules == ()


def test_ac3_triage_never_reads_body_text_even_when_body_contains_a_matching_keyword() -> None:
    """Companion metadata-only proof (Q-66 ruling 2): a keyword present ONLY in body_text
    (never in subject/sender) must NOT match, proving triage_message never scans the body."""
    keyword_rule = SubjectKeywordRule(
        name="urgent_subject_keyword",
        keywords=("urgent",),
        urgency_score=0.8,
        priority_band=PriorityBand.HIGH,
    )
    rules = _rules(subject_keyword_rules=(keyword_rule,))

    # The keyword "urgent" appears ONLY in body_text — subject/sender are keyword-free. If
    # triage_message ever scanned the body, this would incorrectly match.
    item = _item(
        subject="weekly newsletter",
        sender="nobody@example.com",
        body_text="This is URGENT, please read the body immediately.",
    )

    result = triage_message(item, rules)

    assert result.priority_band == PriorityBand.NORMAL
    assert result.matched_rules == ()


def test_triage_module_source_never_references_body_text() -> None:
    """Static proof mirroring the Q-65 guard's own style: the triage.py source text never
    contains the body_text literal at all (belt-and-braces on top of the Q-65 build guard,
    which independently fails the whole build if this ever changes)."""
    triage_src = (_SRC_ROOT / "integrations" / "gmail" / "triage.py").read_text(encoding="utf-8")
    assert "body_text" not in triage_src


# --- rules loader: schema-validated, loud on malformed/unversioned -----------------------


def test_load_triage_rules_loads_the_shipped_packaged_yaml() -> None:
    rules = load_triage_rules()
    assert rules.version == 1
    assert len(rules.sender_allowlist_rules) >= 1
    assert len(rules.subject_keyword_rules) >= 1


def test_load_triage_rules_missing_file_raises_configuration_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError):
        load_triage_rules(tmp_path / "does_not_exist.yaml")


def test_load_triage_rules_unversioned_file_raises_configuration_error(tmp_path: Path) -> None:
    dst = tmp_path / "triage_rules.yaml"
    dst.write_text(
        yaml.safe_dump(
            {
                "sender_allowlist_rules": [],
                "subject_keyword_rules": [],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError):
        load_triage_rules(dst)


def test_load_triage_rules_non_mapping_content_raises_configuration_error(tmp_path: Path) -> None:
    dst = tmp_path / "triage_rules.yaml"
    dst.write_text(yaml.safe_dump(["not", "a", "mapping"]), encoding="utf-8")
    with pytest.raises(ConfigurationError):
        load_triage_rules(dst)


def test_load_triage_rules_invalid_yaml_raises_configuration_error(tmp_path: Path) -> None:
    dst = tmp_path / "triage_rules.yaml"
    dst.write_text("version: 1\n  bad_indent: [oops\n", encoding="utf-8")
    with pytest.raises(ConfigurationError):
        load_triage_rules(dst)


def test_load_triage_rules_schema_invalid_rule_raises_configuration_error(tmp_path: Path) -> None:
    dst = tmp_path / "triage_rules.yaml"
    dst.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "sender_allowlist_rules": [
                    {
                        "name": "bad_rule",
                        "senders": ["x@example.com"],
                        "urgency_score": 1.5,  # out of [0.0, 1.0] bounds
                        "priority_band": "high",
                    }
                ],
                "subject_keyword_rules": [],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError):
        load_triage_rules(dst)


def test_load_triage_rules_unknown_field_raises_configuration_error(tmp_path: Path) -> None:
    dst = tmp_path / "triage_rules.yaml"
    dst.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "sender_allowlist_rules": [],
                "subject_keyword_rules": [],
                "some_unexpected_field": "boom",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError):
        load_triage_rules(dst)


def test_shipped_rules_file_has_a_version_field() -> None:
    raw = yaml.safe_load(_SHIPPED_RULES_PATH.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    assert "version" in raw


# --- TriageResult invariants ---------------------------------------------------------------


def test_triage_result_rejects_urgency_score_out_of_bounds() -> None:
    with pytest.raises(ValueError, match="urgency_score"):
        TriageResult(
            message_id="m",
            urgency_score=1.5,
            priority_band=PriorityBand.NORMAL,
            matched_rules=(),
        )


def test_priority_band_is_a_closed_enum_of_exactly_high_and_normal() -> None:
    assert {member.name for member in PriorityBand} == {"HIGH", "NORMAL"}
    assert {member.value for member in PriorityBand} == {"high", "normal"}


def test_triage_rules_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        TriageRules(version=1, unexpected_field="boom")  # type: ignore[call-arg]


# --- AC4: structural no-import hot-path guard -----------------------------------------------


def _references_triage_module(text: str) -> bool:
    """True iff ``text`` references the triage module (import statement or dotted path) —
    the same substring-scan style as the Q-65 body-key guard
    (tests/integrations/gmail/test_body_key_guard.py)."""
    return _TRIAGE_MODULE_NAME in text or "gmail import triage" in text


def test_ac4_no_drain_spine_module_imports_triage() -> None:
    offenders = [
        str(path)
        for path in _DRAIN_SPINE_PATHS
        if _references_triage_module(path.read_text(encoding="utf-8"))
    ]
    assert not offenders, (
        f"drain-spine module(s) import wombat.integrations.gmail.triage, breaking the S1 "
        f"off-path guarantee: {offenders}"
    )


def test_ac4_drain_spine_path_list_is_non_empty_and_every_path_exists() -> None:
    """Guard-scope sanity (mirrors the Q-65 guard's own sanity test): an empty or all-missing
    path list would make the guard above pass vacuously without proving anything."""
    assert len(_DRAIN_SPINE_PATHS) >= 6
    for path in _DRAIN_SPINE_PATHS:
        assert path.exists(), f"drain-spine guard path does not exist: {path}"


def test_ac4_guard_is_load_bearing_it_would_fail_if_the_import_were_added() -> None:
    """Proves the guard actually detects the forbidden import (without mutating any real
    drain-spine file, which is out of this ticket's files_in_scope): feed the SAME detection
    predicate the guard uses a synthetic drain-spine-shaped source that gained the import, and
    assert it is flagged."""
    hypothetical_offending_source = (
        "from __future__ import annotations\n\n"
        "from wombat.integrations.gmail.triage import triage_message\n\n"
        "def run() -> None:\n"
        "    pass\n"
    )
    assert _references_triage_module(hypothetical_offending_source)

    clean_source = (_SRC_ROOT / "stages" / "drain_queue.py").read_text(encoding="utf-8")
    assert not _references_triage_module(clean_source)
