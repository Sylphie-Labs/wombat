"""TK-118 — PhrasingHintContributor acceptance criteria (EP-24, Q-102a).

  AC1 real seed pattern_id + the REAL load_psychology_kb(), registered on a ContextAssembler with
      a preferred head-band 'reflection_hints' slot alongside a required tail 'task' slot -> the
      assembled head system message contains every hint text. Also: contribute() chunk texts
      match extract_phrasing_hints() exactly, and the contributor conforms to the runtime-
      checkable ContextContributor Protocol.
  AC2 pattern_id absent from KB -> zero chunks, status 'empty', no exception; a kb stub whose
      entry access raises -> status 'degraded', no exception (the contributor NEVER raises, S8).
  AC3 scoping (the buildable form): 'phrasing_hint' / 'reflection_hints' appear in NO other
      src/wombat module outside the kb package (and, after TK-114, behavior/stages/
      reflection_compose.py) — stages/compose.py and stages/brief_compose_stage.py are
      specifically asserted clean. Full scoping (a LOCAL per-turn assembler, never a globally
      registered contributor) is completed structurally by TK-114.
  AC4 (hints never verbatim in rendered OUTPUT) is deliberately NOT proven here — that is
      TK-114's suite's job once the reflection turn's full render path exists.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import overload

import pytest
from cogworx.context.assembler import ContextAssembler
from cogworx.context.contributor import ContextContributor
from cogworx.context.types import ContextRequest, SlotAllocation, SlotSpec

from wombat.kb.contributors.phrasing_hint_contributor import PhrasingHintContributor
from wombat.kb.loader import load_psychology_kb
from wombat.kb.phrasing_hints import extract_phrasing_hints
from wombat.kb.schema import GateCondition, KBEntry

_SEED_PATTERN_ID = "rapid_context_switching"

_SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "wombat"


# --------------------------------------------------------------------------------------- AC1


async def test_ac1_conforms_to_context_contributor_protocol() -> None:
    kb = load_psychology_kb()
    contributor = PhrasingHintContributor(_SEED_PATTERN_ID, kb)

    assert isinstance(contributor, ContextContributor)


async def test_ac1_contribute_chunks_match_extract_phrasing_hints_exactly() -> None:
    kb = load_psychology_kb()
    contributor = PhrasingHintContributor(_SEED_PATTERN_ID, kb)

    content = await contributor.contribute(
        ContextRequest(task="x"), SlotAllocation(max_tokens=None)
    )

    expected = extract_phrasing_hints(_SEED_PATTERN_ID, kb)
    assert [chunk.text for chunk in content.chunks] == expected
    assert content.status == "ok"
    assert all(chunk.source_slot == "reflection_hints" for chunk in content.chunks)
    assert [chunk.key for chunk in content.chunks] == [
        f"reflection_hints:{i}" for i in range(len(expected))
    ]


async def test_ac1_assembled_head_system_message_contains_every_hint() -> None:
    kb = load_psychology_kb()
    contributor = PhrasingHintContributor(_SEED_PATTERN_ID, kb)
    hints = extract_phrasing_hints(_SEED_PATTERN_ID, kb)
    assert hints, "seed pattern_id must have >=1 phrasing hint for this test to mean anything"

    assembler = ContextAssembler(
        slots=(
            SlotSpec(name="reflection_hints", band="head", necessity="preferred"),
            SlotSpec(name="task", band="tail", necessity="required"),
        )
    )
    assembler.register("reflection_hints", contributor)

    assembled = await assembler.assemble(ContextRequest(task="reflect on today"))

    system_messages = [m for m in assembled.messages if m.role == "system"]
    assert len(system_messages) == 1
    for hint in hints:
        assert hint in system_messages[0].content


# --------------------------------------------------------------------------------------- AC2


async def test_ac2_unknown_pattern_id_yields_empty_status_no_chunks_no_exception() -> None:
    kb = load_psychology_kb()
    contributor = PhrasingHintContributor("no_such_pattern", kb)

    content = await contributor.contribute(
        ContextRequest(task="x"), SlotAllocation(max_tokens=None)
    )

    assert content.status == "empty"
    assert content.chunks == ()


class _RaisingKB(Sequence[KBEntry]):
    """A kb stub whose entry access raises — proves the contributor degrades, never raises."""

    def __len__(self) -> int:
        return 1

    @overload
    def __getitem__(self, index: int) -> KBEntry: ...
    @overload
    def __getitem__(self, index: slice) -> Sequence[KBEntry]: ...
    def __getitem__(self, index: int | slice) -> KBEntry | Sequence[KBEntry]:
        raise RuntimeError("kb entry access exploded")


async def test_ac2_kb_entry_access_raising_yields_degraded_status_no_exception() -> None:
    contributor = PhrasingHintContributor(_SEED_PATTERN_ID, _RaisingKB())

    content = await contributor.contribute(
        ContextRequest(task="x"), SlotAllocation(max_tokens=None)
    )

    assert content.status == "degraded"
    assert content.detail is not None
    assert content.chunks == ()


async def test_ac2_empty_kb_yields_empty_status() -> None:
    contributor = PhrasingHintContributor(_SEED_PATTERN_ID, [])

    content = await contributor.contribute(
        ContextRequest(task="x"), SlotAllocation(max_tokens=None)
    )

    assert content.status == "empty"
    assert content.chunks == ()


def test_ac2_fixture_kb_sanity_matching_pattern_still_resolves() -> None:
    # Sanity check that _RaisingKB's failure mode is specific to raising access, not to every
    # non-loader-produced sequence — a normal fixture entry still resolves fine.
    fixture = KBEntry(
        pattern_id="fixture_pattern",
        description="A fixture pattern used only by this test.",
        gate_condition=GateCondition(metric="switch_rate", operator=">", threshold=0.6),
        phrasing_hints=("a fixture hint",),
        autonomy_level="gentle_note",
        evidence_tag="fixture_source_2026",
        version=1,
    )
    assert extract_phrasing_hints("fixture_pattern", [fixture]) == ["a fixture hint"]


# --------------------------------------------------------------------------------------- AC3

# The kb package is these terms' natural home (schema field name + TK-117's lookup module + this
# ticket's own contributor). TK-114's future reflection_compose.py is the one sanctioned consumer
# outside the kb package; it need not exist yet for this guard to hold.
_ALLOWED_DIR = _SRC_ROOT / "kb"
_ALLOWED_FUTURE_FILE = _SRC_ROOT / "behavior" / "stages" / "reflection_compose.py"

_SCAN_TERMS = ("phrasing_hint", "reflection_hints")


def test_ac3_scoping_terms_appear_in_no_other_src_wombat_module() -> None:
    offenders: list[str] = []
    for py in _SRC_ROOT.rglob("*.py"):
        if _ALLOWED_DIR in py.parents or py == _ALLOWED_FUTURE_FILE:
            continue
        text = py.read_text(encoding="utf-8")
        if any(term in text for term in _SCAN_TERMS):
            offenders.append(str(py))
    assert not offenders, (
        f"phrasing-hint/reflection_hints scoping terms leaked outside the kb package: {offenders}"
    )


@pytest.mark.parametrize(
    "relative_path",
    ["stages/compose.py", "stages/brief_compose_stage.py"],
)
def test_ac3_named_composer_stages_are_specifically_clean(relative_path: str) -> None:
    path = _SRC_ROOT / relative_path
    assert path.exists(), f"expected source file missing: {path}"
    text = path.read_text(encoding="utf-8")
    for term in _SCAN_TERMS:
        assert term not in text, f"{relative_path} unexpectedly references {term!r}"
