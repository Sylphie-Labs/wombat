"""TK-228 (DEC-40) — the DEC-21/Q-15 timezone invariant made STRUCTURAL.

LIVE DEFECT (ISS-8): the morning brief fired at 2026-07-10T07:00:08Z (03:00 EDT). Root cause was
a UTC default (``bootstrap._UTC_ZONE``) silently threaded through every tz-consuming composition
seam. This module proves the fix at the two levels DEC-40 rules:

  AC1 the runnable 07:00-LOCAL proof — ``next_fire_at`` over ``resolve_wombat_zone``'s resolution
      of an explicit ``WOMBAT_TIMEZONE=America/New_York`` lands the brief at 07:00 EDT (11:00Z) —
      plus a spy on ``wombat.runtime.serve()`` proving it threads THAT SAME resolved zone into
      ``assemble_runtime`` as ``tz`` (never a UTC default sneaking back in).
  AC3 the structural closure — ``inspect.signature`` proves ``tz`` carries NO default on the five
      composition factories (so a future caller cannot silently inherit UTC by omission), and a
      source scan proves the literal ``ZoneInfo("UTC")`` default no longer exists anywhere under
      ``src/wombat``.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, time
from pathlib import Path
from typing import NoReturn
from zoneinfo import ZoneInfo

import pytest

import wombat.runtime as runtime_module
from wombat import bootstrap
from wombat.config import WombatConfig, resolve_wombat_zone
from wombat.domain.brief_schedule import next_fire_at
from wombat.pathways import dream_substrate

# --- AC1(a): the runnable 07:00-LOCAL proof ------------------------------------------------


def test_ac1_next_fire_at_lands_at_07_00_local_under_america_new_york() -> None:
    """``resolve_wombat_zone`` over an explicit ``America/New_York`` feeds ``next_fire_at`` (the
    EXACT math ``BriefTimerStage`` runs) — the fix's whole point: 2026-07-10T08:00:00Z resolves
    to 07:00 EDT (11:00Z), not 03:00 EDT (07:00Z, the live incident)."""
    config = WombatConfig(
        deepseek_api_key="sk-test",
        deepseek_base_url="https://x.example",
        wombat_timezone="America/New_York",
    )
    tz = resolve_wombat_zone(config)

    now = datetime(2026, 7, 10, 8, 0, tzinfo=UTC)
    fire = next_fire_at(now, tz, time(7, 0))

    assert fire == datetime(2026, 7, 10, 11, 0, tzinfo=UTC)  # == 2026-07-10T07:00:00-04:00
    assert fire.astimezone(tz).isoformat() == "2026-07-10T07:00:00-04:00"


# --- AC1(b): serve() threads the SAME resolved zone into assemble_runtime, never UTC -------


async def test_ac1_serve_threads_resolve_wombat_zone_into_assemble_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)  # isolate from any real repo-root .env
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://x.example")
    monkeypatch.setenv("WOMBAT_PG_DSN", "postgresql://localhost/wombat")
    monkeypatch.setenv("WOMBAT_TIMEZONE", "America/New_York")

    captured: dict[str, object] = {}

    class _StopEarly(Exception):
        """Raised by the spy right after capturing kwargs so ``serve()`` never reaches
        ``_drive_and_serve`` (which would need a real, fully-wired bundle)."""

    def _spy_assemble_runtime(**kwargs: object) -> NoReturn:
        captured.update(kwargs)
        raise _StopEarly()

    monkeypatch.setattr(runtime_module, "assemble_runtime", _spy_assemble_runtime)

    with pytest.raises(_StopEarly):
        await runtime_module.serve()

    assert captured["tz"] == ZoneInfo("America/New_York")
    assert captured["tz"] != ZoneInfo("UTC")  # never a silent UTC default


# --- AC3: the tz parameter carries NO default on the five composition factories ------------


def test_ac3_tz_parameter_has_no_default_on_the_five_factories() -> None:
    targets = (
        bootstrap.assemble_runtime,
        bootstrap.build_compose_stage,
        bootstrap.build_brief_compose_stage,
        bootstrap.build_brief_deliver_stage,
        dream_substrate.build_dream_substrate,
    )
    for fn in targets:
        sig = inspect.signature(fn)
        tz_param = sig.parameters["tz"]
        assert tz_param.default is inspect.Parameter.empty, (
            f"{fn.__qualname__}'s tz parameter must carry NO default (TK-228 AC3) — got "
            f"{tz_param.default!r}"
        )


# --- AC3: the literal ZoneInfo("UTC") default is structurally gone from src/wombat ---------


def test_ac3_no_literal_utc_zoneinfo_default_anywhere_under_src_wombat() -> None:
    src_root = Path(__file__).resolve().parents[2] / "src" / "wombat"
    offenders = [
        str(path)
        for path in src_root.rglob("*.py")
        if 'ZoneInfo("UTC")' in path.read_text(encoding="utf-8")
        or "ZoneInfo('UTC')" in path.read_text(encoding="utf-8")
    ]
    assert not offenders, f"found a literal UTC ZoneInfo under src/wombat: {offenders}"
