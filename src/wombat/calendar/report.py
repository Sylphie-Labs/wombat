"""Human-readable slot report for the RISK-6 rating exercise (TK-73).

Renders the proposed alternative slots per fixture scenario as a plain-text rating
sheet Jim can mark up. This is the spike's *deliverable surface* — the bar for
RISK-6 is human judgement, not a green CI run (TK-73 AC2). Pure formatting; the
only I/O is reading the fixture path the caller hands in.

Run:  python -m wombat.calendar.report tests/calendar/fixtures/conflicts.yaml
"""

from __future__ import annotations

import sys
from pathlib import Path

from .fixture_loader import load_fixture
from .slots import detect_conflicts, propose_alternatives


def _fmt(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def render(fixture_path: Path) -> str:
    """Render the rating sheet for every scenario in the fixture."""
    fixture = load_fixture(fixture_path)
    lines: list[str] = []
    lines.append("# RISK-6 slot-quality rating sheet (TK-73)")
    lines.append(
        f"# working hours: {_fmt(fixture.working_hours.start)}"
        f"-{_fmt(fixture.working_hours.end)}"
    )
    lines.append("# Mark each slot [x] reasonable or leave [ ] unhelpful.")
    lines.append("")
    for scenario in fixture.scenarios:
        events = list(scenario.events)
        conflicts = detect_conflicts(events)
        conflict = conflicts[0]
        movable = conflict.movable
        incumbent = conflict.incumbent
        slots = propose_alternatives(conflict, events, fixture.working_hours)
        lines.append(f"## {scenario.name}")
        if scenario.note:
            lines.append(f"   {scenario.note}")
        lines.append(
            f"   move {movable.title!r} "
            f"({_fmt(movable.start)}-{_fmt(movable.end)}) "
            f"— clashes with {incumbent.title!r} "
            f"({_fmt(incumbent.start)}-{_fmt(incumbent.end)})"
        )
        for slot in slots:
            lines.append(
                f"     [ ] rank {slot.rank}: "
                f"{_fmt(slot.start)}-{_fmt(slot.end)}"
            )
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        sys.stderr.write("usage: python -m wombat.calendar.report <fixture.yaml>\n")
        return 2
    sys.stdout.write(render(Path(argv[1])))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
