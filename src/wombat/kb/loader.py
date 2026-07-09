"""load_psychology_kb — the validated, versioned loader for the psychology KB (TK-115, Q-99a/b).

Reads packaged ``psychology_kb.yaml`` via ``importlib.resources`` (the ``wombat.migrations``
idiom in ``wombat.behavior.event_log.ensure_schema``) and validates it into a
``list[wombat.kb.schema.KBEntry]``. ``load_psychology_kb`` takes an optional ``path`` override
(tests only, e.g. malformed-file fixtures) — production and default-test callers always resolve
the packaged file.

Every required field (file-level ``version``; per-entry ``pattern_id``, ``description``,
``gate_condition`` with its nested ``metric``/``operator``/``threshold``, ``phrasing_hints``,
``autonomy_level``, ``evidence_tag``) is checked, and ``gate_condition.metric``/``operator`` are
checked against the closed v1 vocabularies (``wombat.kb.schema.METRIC_VOCABULARY`` /
``OPERATOR_VOCABULARY``). Any violation raises ``ValidationError`` with a message naming the
offending entry and field. A missing file raises ``FileNotFoundError``.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from wombat.kb.schema import (
    METRIC_VOCABULARY,
    OPERATOR_VOCABULARY,
    GateCondition,
    KBEntry,
    ValidationError,
)

_KB_PACKAGE = "wombat.kb"
_KB_FILENAME = "psychology_kb.yaml"

_REQUIRED_ENTRY_FIELDS = (
    "pattern_id",
    "description",
    "gate_condition",
    "phrasing_hints",
    "autonomy_level",
    "evidence_tag",
)
_REQUIRED_CONDITION_FIELDS = ("metric", "operator", "threshold")


def load_psychology_kb(path: Path | None = None) -> list[KBEntry]:
    """Load and validate the psychology KB, returning its entries in file order.

    Defaults to the packaged ``src/wombat/kb/psychology_kb.yaml``. Pass ``path`` to load a
    different file (test fixtures only). Raises ``FileNotFoundError`` if ``path`` does not exist,
    or ``ValidationError`` if the YAML fails to parse or any required field / closed vocabulary
    is violated (TK-115 AC1/AC4).
    """
    if path is None:
        raw_text = resources.files(_KB_PACKAGE).joinpath(_KB_FILENAME).read_text(
            encoding="utf-8"
        )
    else:
        if not path.exists():
            raise FileNotFoundError(f"psychology KB file not found: {path}")
        raw_text = path.read_text(encoding="utf-8")

    try:
        document = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise ValidationError(f"psychology KB YAML failed to parse: {exc}") from exc

    if not isinstance(document, dict):
        raise ValidationError("psychology KB YAML must be a mapping at the top level")

    if "version" not in document:
        raise ValidationError(
            "psychology KB YAML is missing the required top-level 'version' field"
        )
    file_version = document["version"]

    entries_raw = document.get("entries")
    if not isinstance(entries_raw, list) or not entries_raw:
        raise ValidationError(
            "psychology KB YAML must have a non-empty top-level 'entries' list"
        )

    return [
        _parse_entry(raw_entry, index, file_version)
        for index, raw_entry in enumerate(entries_raw)
    ]


def _parse_entry(raw_entry: Any, index: int, file_version: Any) -> KBEntry:
    if not isinstance(raw_entry, dict):
        raise ValidationError(f"KB entry #{index} is not a mapping")

    for field in _REQUIRED_ENTRY_FIELDS:
        if field not in raw_entry:
            raise ValidationError(f"KB entry #{index} is missing required field '{field}'")

    condition_raw = raw_entry["gate_condition"]
    if not isinstance(condition_raw, dict):
        raise ValidationError(f"KB entry #{index} 'gate_condition' must be a mapping")
    for field in _REQUIRED_CONDITION_FIELDS:
        if field not in condition_raw:
            raise ValidationError(
                f"KB entry #{index} gate_condition is missing required field '{field}'"
            )

    metric = condition_raw["metric"]
    if metric not in METRIC_VOCABULARY:
        raise ValidationError(
            f"KB entry #{index} gate_condition.metric {metric!r} is not in the closed v1 "
            f"metric vocabulary {sorted(METRIC_VOCABULARY)}"
        )

    operator = condition_raw["operator"]
    if operator not in OPERATOR_VOCABULARY:
        raise ValidationError(
            f"KB entry #{index} gate_condition.operator {operator!r} is not in the closed "
            f"operator vocabulary {sorted(OPERATOR_VOCABULARY)}"
        )

    phrasing_hints_raw = raw_entry["phrasing_hints"]
    if not isinstance(phrasing_hints_raw, list) or not phrasing_hints_raw:
        raise ValidationError(f"KB entry #{index} 'phrasing_hints' must be a non-empty list")

    try:
        threshold = float(condition_raw["threshold"])
    except (ValueError, TypeError) as exc:
        raise ValidationError(
            f"KB entry #{index} gate_condition.threshold {condition_raw['threshold']!r} is not "
            f"a valid number"
        ) from exc

    try:
        version = int(file_version)
    except (ValueError, TypeError) as exc:
        raise ValidationError(
            f"psychology KB YAML top-level 'version' {file_version!r} is not a valid integer"
        ) from exc

    return KBEntry(
        pattern_id=str(raw_entry["pattern_id"]),
        description=str(raw_entry["description"]),
        gate_condition=GateCondition(
            metric=str(metric),
            operator=str(operator),
            threshold=threshold,
        ),
        phrasing_hints=tuple(str(hint) for hint in phrasing_hints_raw),
        autonomy_level=str(raw_entry["autonomy_level"]),
        evidence_tag=str(raw_entry["evidence_tag"]),
        version=version,
    )
