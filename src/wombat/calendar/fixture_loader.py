"""Dependency-free loader for the hand-authored conflict fixture (TK-73).

The venv has no PyYAML and the spike may not add dependencies, so this parses the
*specific, narrow* block-style YAML subset used by
``tests/calendar/fixtures/conflicts.yaml`` and nothing else. It is deliberately
strict: anything outside the expected shape raises, so a malformed fixture fails
loudly rather than silently mis-loading.

Supported subset:
  - a top-level ``working_hours`` mapping with ``start``/``end`` "HH:MM" strings
  - a top-level ``scenarios`` sequence; each scenario is a mapping with ``name``
    (str), optional ``note`` (str), and ``events`` (sequence of mappings with
    ``id``, ``title``, ``start`` "HH:MM", ``end`` "HH:MM").
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .models import CalendarEventItem, WorkingHours


@dataclass(frozen=True, slots=True)
class Scenario:
    """One hand-authored conflict scenario from the fixture."""

    name: str
    note: str
    events: tuple[CalendarEventItem, ...]


@dataclass(frozen=True, slots=True)
class Fixture:
    """The whole fixture: shared working hours plus the list of scenarios."""

    working_hours: WorkingHours
    scenarios: tuple[Scenario, ...]


def _hhmm_to_minutes(value: str, *, context: str) -> int:
    parts = value.split(":")
    if len(parts) != 2:
        msg = f"{context}: expected HH:MM time, got {value!r}"
        raise ValueError(msg)
    try:
        hours = int(parts[0])
        minutes = int(parts[1])
    except ValueError as exc:
        msg = f"{context}: non-numeric time {value!r}"
        raise ValueError(msg) from exc
    if not (0 <= hours <= 24 and 0 <= minutes < 60):
        msg = f"{context}: time out of range {value!r}"
        raise ValueError(msg)
    return hours * 60 + minutes


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _strip_value(raw: str) -> str:
    text = raw.strip()
    if len(text) >= 2 and text[0] in {'"', "'"} and text[-1] == text[0]:
        return text[1:-1]
    return text


def _parse_event(block: list[str], *, context: str) -> CalendarEventItem:
    """Parse one ``- id: ... / title: ... / start: ... / end: ...`` event block."""
    fields: dict[str, str] = {}
    for line in block:
        key, _, value = line.strip().lstrip("- ").partition(":")
        fields[key.strip()] = _strip_value(value)
    required = {"id", "title", "start", "end"}
    missing = required - fields.keys()
    if missing:
        msg = f"{context}: event missing fields {sorted(missing)}"
        raise ValueError(msg)
    return CalendarEventItem(
        event_id=fields["id"],
        title=fields["title"],
        start=_hhmm_to_minutes(fields["start"], context=f"{context}.start"),
        end=_hhmm_to_minutes(fields["end"], context=f"{context}.end"),
    )


def load_fixture(path: Path) -> Fixture:
    """Load and validate the conflict fixture from ``path``.

    Raises ``ValueError`` on any structural surprise. This is a narrow parser, not
    a general YAML engine — keep the fixture inside the documented subset.
    """
    lines = [
        line.rstrip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]

    wh_start: int | None = None
    wh_end: int | None = None
    scenarios: list[Scenario] = []

    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        indent = _indent(line)
        stripped = line.strip()

        if indent == 0 and stripped == "working_hours:":
            i += 1
            while i < n and _indent(lines[i]) > 0:
                key, _, value = lines[i].strip().partition(":")
                if key.strip() == "start":
                    wh_start = _hhmm_to_minutes(
                        _strip_value(value), context="working_hours.start"
                    )
                elif key.strip() == "end":
                    wh_end = _hhmm_to_minutes(
                        _strip_value(value), context="working_hours.end"
                    )
                i += 1
            continue

        if indent == 0 and stripped == "scenarios:":
            i += 1
            i = _parse_scenarios(lines, i, scenarios)
            continue

        msg = f"unexpected top-level line: {line!r}"
        raise ValueError(msg)

    if wh_start is None or wh_end is None:
        msg = "fixture missing working_hours.start/end"
        raise ValueError(msg)
    if not scenarios:
        msg = "fixture defines no scenarios"
        raise ValueError(msg)

    return Fixture(
        working_hours=WorkingHours(start=wh_start, end=wh_end),
        scenarios=tuple(scenarios),
    )


def _parse_scenarios(lines: list[str], start: int, out: list[Scenario]) -> int:
    """Parse the ``scenarios:`` sequence starting at ``start``; return next index."""
    n = len(lines)
    i = start
    while i < n and _indent(lines[i]) > 0:
        # Each scenario begins with a "- name: ..." item at the sequence indent.
        if "name:" not in lines[i]:
            msg = f"scenario item must start with name: got {lines[i]!r}"
            raise ValueError(msg)
        _, _, name_val = lines[i].strip().lstrip("- ").partition(":")
        name = _strip_value(name_val)
        item_indent = _indent(lines[i])
        i += 1

        note = ""
        events: list[CalendarEventItem] = []
        while i < n and _indent(lines[i]) > item_indent:
            sub = lines[i].strip()
            if sub.startswith("note:"):
                note = _strip_value(sub.partition(":")[2])
                i += 1
            elif sub == "events:":
                i += 1
                i = _parse_event_seq(lines, i, name, events)
            else:
                msg = f"scenario {name!r}: unexpected line {lines[i]!r}"
                raise ValueError(msg)
        out.append(Scenario(name=name, note=note, events=tuple(events)))
    return i


def _parse_event_seq(
    lines: list[str], start: int, scenario_name: str, out: list[CalendarEventItem]
) -> int:
    """Parse an ``events:`` sequence of ``- id:`` blocks; return next index."""
    n = len(lines)
    i = start
    if i >= n or "- id:" not in lines[i]:
        msg = f"scenario {scenario_name!r}: events: must be followed by '- id:' items"
        raise ValueError(msg)
    seq_indent = _indent(lines[i])
    while i < n and _indent(lines[i]) >= seq_indent:
        if lines[i].strip().startswith("- "):
            block = [lines[i]]
            i += 1
            while i < n and _indent(lines[i]) > seq_indent:
                block.append(lines[i])
                i += 1
            out.append(_parse_event(block, context=f"scenario {scenario_name!r}"))
        else:
            break
    return i
