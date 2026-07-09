"""TK-115 — psychology KB schema + loader acceptance criteria (EP-23, Q-99a/b).

  AC1 load_psychology_kb() over the packaged file returns a validated list[KBEntry] with every
      required field typed; a fixture YAML missing any required field (top-level version;
      per-entry pattern_id/description/gate_condition/phrasing_hints/autonomy_level/evidence_tag;
      nested gate_condition metric/operator/threshold) raises ValidationError.
  AC2 the packaged YAML has no generated_by/model_authored key anywhere, and every entry has a
      non-empty human-authored evidence_tag (TECH-13).
  AC3 no entry (any string field, phrasing_hints included) contains 'diagnos', 'disorder',
      'therapy', 'symptom', 'ADHD', or a DSM/ICD term, case-insensitive (NG-2).
  AC4 a missing file raises FileNotFoundError; malformed YAML raises ValidationError.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from wombat.kb.loader import load_psychology_kb
from wombat.kb.schema import GateCondition, KBEntry, ValidationError

_PACKAGED_YAML_PATH = (
    Path(__file__).resolve().parents[2] / "src" / "wombat" / "kb" / "psychology_kb.yaml"
)


def _valid_document() -> dict[str, Any]:
    """A minimal, independently-valid one-entry KB document for fixture mutation."""
    return {
        "version": 1,
        "entries": [
            {
                "pattern_id": "fixture_pattern",
                "description": "A fixture pattern used only by tests.",
                "gate_condition": {"metric": "switch_rate", "operator": ">", "threshold": 0.5},
                "phrasing_hints": ["a fixture hint"],
                "autonomy_level": "gentle_note",
                "evidence_tag": "fixture_source_2026",
            }
        ],
    }


def _write_yaml(tmp_path: Path, document: Any) -> Path:
    fixture_path = tmp_path / "fixture.yaml"
    fixture_path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return fixture_path


# --------------------------------------------------------------------------------------- AC1


def test_ac1_loads_the_real_packaged_kb_as_typed_entries() -> None:
    entries = load_psychology_kb()

    assert isinstance(entries, list)
    assert 1 <= len(entries) <= 20
    for entry in entries:
        assert isinstance(entry, KBEntry)
        assert isinstance(entry.pattern_id, str) and entry.pattern_id
        assert isinstance(entry.description, str) and entry.description
        assert isinstance(entry.gate_condition, GateCondition)
        assert isinstance(entry.gate_condition.metric, str)
        assert isinstance(entry.gate_condition.operator, str)
        assert isinstance(entry.gate_condition.threshold, float)
        assert isinstance(entry.phrasing_hints, tuple)
        assert len(entry.phrasing_hints) >= 1
        assert all(isinstance(hint, str) and hint for hint in entry.phrasing_hints)
        assert isinstance(entry.autonomy_level, str) and entry.autonomy_level
        assert isinstance(entry.evidence_tag, str) and entry.evidence_tag
        assert isinstance(entry.version, int)


def test_ac1_default_path_reads_the_same_packaged_file_as_an_explicit_path() -> None:
    default_entries = load_psychology_kb()
    explicit_entries = load_psychology_kb(_PACKAGED_YAML_PATH)
    assert default_entries == explicit_entries


@pytest.mark.parametrize(
    "field",
    ["version"],
)
def test_ac1_missing_top_level_field_raises_validation_error(
    tmp_path: Path, field: str
) -> None:
    document = _valid_document()
    del document[field]
    fixture_path = _write_yaml(tmp_path, document)

    with pytest.raises(ValidationError):
        load_psychology_kb(fixture_path)


@pytest.mark.parametrize(
    "field",
    [
        "pattern_id",
        "description",
        "gate_condition",
        "phrasing_hints",
        "autonomy_level",
        "evidence_tag",
    ],
)
def test_ac1_missing_entry_field_raises_validation_error(tmp_path: Path, field: str) -> None:
    document = _valid_document()
    del document["entries"][0][field]
    fixture_path = _write_yaml(tmp_path, document)

    with pytest.raises(ValidationError):
        load_psychology_kb(fixture_path)


@pytest.mark.parametrize("field", ["metric", "operator", "threshold"])
def test_ac1_missing_gate_condition_field_raises_validation_error(
    tmp_path: Path, field: str
) -> None:
    document = _valid_document()
    del document["entries"][0]["gate_condition"][field]
    fixture_path = _write_yaml(tmp_path, document)

    with pytest.raises(ValidationError):
        load_psychology_kb(fixture_path)


def test_ac1_metric_outside_closed_vocabulary_raises_validation_error(tmp_path: Path) -> None:
    document = _valid_document()
    document["entries"][0]["gate_condition"]["metric"] = "not_a_real_metric"
    fixture_path = _write_yaml(tmp_path, document)

    with pytest.raises(ValidationError):
        load_psychology_kb(fixture_path)


def test_ac1_operator_outside_closed_vocabulary_raises_validation_error(tmp_path: Path) -> None:
    document = _valid_document()
    document["entries"][0]["gate_condition"]["operator"] = "!="
    fixture_path = _write_yaml(tmp_path, document)

    with pytest.raises(ValidationError):
        load_psychology_kb(fixture_path)


def test_ac1_empty_phrasing_hints_raises_validation_error(tmp_path: Path) -> None:
    document = _valid_document()
    document["entries"][0]["phrasing_hints"] = []
    fixture_path = _write_yaml(tmp_path, document)

    with pytest.raises(ValidationError):
        load_psychology_kb(fixture_path)


# --------------------------------------------------------------------------------------- AC2


def test_ac2_no_generated_by_or_model_authored_key_anywhere() -> None:
    raw_text = _PACKAGED_YAML_PATH.read_text(encoding="utf-8")
    document = yaml.safe_load(raw_text)

    banned_keys = {"generated_by", "model_authored"}

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            assert banned_keys.isdisjoint(node.keys())
            for value in node.values():
                _walk(value)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(document)


def test_ac2_every_entry_has_a_non_empty_human_authored_evidence_tag() -> None:
    entries = load_psychology_kb()
    for entry in entries:
        assert entry.evidence_tag.strip() != ""


# --------------------------------------------------------------------------------------- AC3

_BANNED_TERMS = ("diagnos", "disorder", "therapy", "symptom", "adhd", "dsm", "icd")


def test_ac3_no_entry_contains_clinical_language() -> None:
    entries = load_psychology_kb()
    for entry in entries:
        strings = [
            entry.pattern_id,
            entry.description,
            entry.autonomy_level,
            entry.evidence_tag,
            entry.gate_condition.metric,
            entry.gate_condition.operator,
            *entry.phrasing_hints,
        ]
        for text in strings:
            lowered = text.lower()
            for term in _BANNED_TERMS:
                assert term not in lowered, f"banned term {term!r} found in {text!r}"


# --------------------------------------------------------------------------------------- AC4


def test_ac4_missing_file_raises_file_not_found_error(tmp_path: Path) -> None:
    missing_path = tmp_path / "does_not_exist.yaml"

    with pytest.raises(FileNotFoundError):
        load_psychology_kb(missing_path)


def test_ac4_malformed_yaml_syntax_raises_validation_error(tmp_path: Path) -> None:
    fixture_path = tmp_path / "malformed.yaml"
    fixture_path.write_text("version: [1, 2\nentries: {", encoding="utf-8")

    with pytest.raises(ValidationError):
        load_psychology_kb(fixture_path)


def test_ac4_non_mapping_top_level_raises_validation_error(tmp_path: Path) -> None:
    fixture_path = tmp_path / "not_a_mapping.yaml"
    fixture_path.write_text("- just\n- a\n- list\n", encoding="utf-8")

    with pytest.raises(ValidationError):
        load_psychology_kb(fixture_path)


def test_ac4_empty_entries_list_raises_validation_error(tmp_path: Path) -> None:
    document = {"version": 1, "entries": []}
    fixture_path = _write_yaml(tmp_path, document)

    with pytest.raises(ValidationError):
        load_psychology_kb(fixture_path)
