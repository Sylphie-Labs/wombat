"""tests/unit/test_wipe_wombat_ps1.py — TK-337: the one-click quiesce-then-wipe operator
wrapper (scripts/wipe-wombat.ps1), reusing scripts/stop-wombat.ps1 (DEC-77 r6's extraction
of restart-wombat.ps1's former inline stop block) and deliberately leaving the runtime
stopped afterward (DEC-75f).

Every check here is static source/order inspection, plus one dynamic exercise of the
stop-then-wipe exit-code chaining against a stubbed stop-wombat.ps1 (no live wombat
runtime, no live python — builders must never launch a live wombat runtime).

  AC1 sequence: the stop invocation appears before the wipe-CLI invocation; --confirm and
      -ArchiveDir forwarding (--archive-dir) are present; the CLI's own stdout (which
      prints the archive dir on success) is never redirected/swallowed.
  AC2 exactly one stop implementation: Stop-Process appears only in stop-wombat.ps1 across
      all three scripts.
  AC3 failure propagation: $LASTEXITCODE is checked and propagated after both the stop
      invocation and the CLI invocation; a stubbed stop-wombat.ps1 failure is dynamically
      proven to short-circuit before the wipe CLI would run.
  AC4 no restart: no wombat-console.ps1/Start-Process/restart-wombat.ps1 *invocation*
      appears in wipe-wombat.ps1 (the closing message may still NAME restart-wombat.ps1 as
      the operator's next step — that naming is required, not forbidden).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WIPE_SCRIPT = _REPO_ROOT / "scripts" / "wipe-wombat.ps1"
_STOP_SCRIPT = _REPO_ROOT / "scripts" / "stop-wombat.ps1"
_RESTART_SCRIPT = _REPO_ROOT / "scripts" / "restart-wombat.ps1"

_POWERSHELL = shutil.which("powershell.exe") or shutil.which("pwsh.exe")

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="wipe-wombat.ps1/stop-wombat.ps1 are Windows-only scripts"
)


def _run_powershell(command: str) -> subprocess.CompletedProcess[str]:
    assert _POWERSHELL is not None, "powershell.exe/pwsh.exe not found on PATH"
    return subprocess.run(
        [_POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


# ---------------------------------------------------------------------------
# AC1 — sequence: stop first, then the wipe CLI
# ---------------------------------------------------------------------------


def test_stop_invocation_precedes_wipe_cli_invocation() -> None:
    content = _WIPE_SCRIPT.read_text(encoding="utf-8")
    stop_idx = content.index("& $stopScript")
    wipe_idx = content.index("& $venvPython @wipeArgs")
    assert stop_idx < wipe_idx, (
        "wipe-wombat.ps1 must stop the runtime before invoking the wipe CLI"
    )


def test_confirm_flag_and_archive_dir_forwarding_present() -> None:
    content = _WIPE_SCRIPT.read_text(encoding="utf-8")
    assert "'--confirm'" in content
    assert "[string]$ArchiveDir" in content
    assert "'--archive-dir'" in content
    assert "$ArchiveDir" in content.split("'--archive-dir'", 1)[1][:40]


def test_wipe_cli_invocation_does_not_redirect_or_swallow_output() -> None:
    content = _WIPE_SCRIPT.read_text(encoding="utf-8")
    line = next(ln for ln in content.splitlines() if "& $venvPython @wipeArgs" in ln)
    assert "Out-Null" not in line
    assert ">" not in line
    assert "|" not in line


# ---------------------------------------------------------------------------
# AC2 — exactly one stop implementation
# ---------------------------------------------------------------------------


def test_stop_process_appears_only_in_stop_wombat_script() -> None:
    stop_content = _STOP_SCRIPT.read_text(encoding="utf-8")
    restart_content = _RESTART_SCRIPT.read_text(encoding="utf-8")
    wipe_content = _WIPE_SCRIPT.read_text(encoding="utf-8")
    assert "Stop-Process" in stop_content
    assert "Stop-Process" not in restart_content
    assert "Stop-Process" not in wipe_content


def test_restart_and_wipe_scripts_both_invoke_stop_wombat() -> None:
    restart_content = _RESTART_SCRIPT.read_text(encoding="utf-8")
    wipe_content = _WIPE_SCRIPT.read_text(encoding="utf-8")
    assert "stop-wombat.ps1" in restart_content
    assert "& $stopScript" in restart_content
    assert "stop-wombat.ps1" in wipe_content
    assert "& $stopScript" in wipe_content


# ---------------------------------------------------------------------------
# AC3 — failure propagation
# ---------------------------------------------------------------------------


def test_lastexitcode_checked_after_stop_and_after_cli() -> None:
    content = _WIPE_SCRIPT.read_text(encoding="utf-8")
    stop_idx = content.index("& $stopScript")
    stop_check_idx = content.index("if ($LASTEXITCODE -ne 0) {")
    wipe_idx = content.index("& $venvPython @wipeArgs")
    assert stop_idx < stop_check_idx < wipe_idx

    assert "$wipeExitCode = $LASTEXITCODE" in content
    assert "if ($wipeExitCode -ne 0) {" in content
    assert "exit $wipeExitCode" in content


def test_stop_failure_short_circuits_before_wipe_cli(tmp_path: Path) -> None:
    """Dynamic exercise (no live python, no live wombat runtime): a stubbed
    stop-wombat.ps1 that fails must cause wipe-wombat.ps1 to exit with that
    same nonzero code, proving it never falls through to the CLI step."""
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    wipe_copy = scripts_dir / "wipe-wombat.ps1"
    wipe_copy.write_text(_WIPE_SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
    stub_stop = scripts_dir / "stop-wombat.ps1"
    stub_stop.write_text("exit 7\n", encoding="utf-8")

    result = _run_powershell(f"& '{wipe_copy}'; exit $LASTEXITCODE")
    assert result.returncode == 7, (result.stdout, result.stderr)


# ---------------------------------------------------------------------------
# AC4 — no restart
# ---------------------------------------------------------------------------


def test_no_restart_invocation() -> None:
    content = _WIPE_SCRIPT.read_text(encoding="utf-8")
    assert "Start-Process" not in content
    assert "wombat-console.ps1" not in content

    restart_mentions = [ln for ln in content.splitlines() if "restart-wombat.ps1" in ln]
    assert restart_mentions, (
        "the closing message must name restart-wombat.ps1 as the operator's next step"
    )
    for line in restart_mentions:
        assert "&" not in line
        assert "Start-Process" not in line


def test_closing_message_names_restart_as_next_step() -> None:
    content = _WIPE_SCRIPT.read_text(encoding="utf-8")
    assert "Write-Output" in content
    assert "NOT restarted" in content
