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
  repair (batch-review major finding): the script pins its working directory to the repo
      root (Set-Location -LiteralPath $repoRoot, mirroring wombat-console.ps1's own
      convention) BEFORE either the stop or the wipe-CLI invocation — wombat's .env and
      every cwd-relative path (brief/feedback/trail/drop dirs, the archives default) would
      otherwise resolve against whatever cwd wipe-control.ts's cwd-less spawn happens to
      leave it in.
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


# ---------------------------------------------------------------------------
# repair — working directory pinned to the repo root before any invocation
# ---------------------------------------------------------------------------


def test_working_directory_pinned_to_repo_root_before_any_invocation() -> None:
    content = _WIPE_SCRIPT.read_text(encoding="utf-8")
    repo_root_idx = content.index("$repoRoot = Split-Path -Parent $PSScriptRoot")
    set_location_idx = content.index("Set-Location -LiteralPath $repoRoot")
    stop_idx = content.index("& $stopScript")
    wipe_idx = content.index("& $venvPython @wipeArgs")
    assert repo_root_idx < set_location_idx < stop_idx < wipe_idx, (
        "wipe-wombat.ps1 must pin cwd to the repo root before either invocation, since "
        "wombat's .env and its cwd-relative paths depend on it and wipe-control.ts spawns "
        "the script with no cwd"
    )


def test_set_location_precedes_stub_stop_when_invoked_from_unrelated_caller_cwd(
    tmp_path: Path,
) -> None:
    """Dynamic exercise (no live python, no live wombat runtime): invoke a copy of the
    script from a caller cwd that is deliberately NOT the script's own repo root, with a
    stub stop-wombat.ps1 that records $PWD before exiting. Proves Set-Location has
    already repointed cwd to the repo root by the time the FIRST invocation runs —
    the wipe-CLI step inherits whatever cwd stop-wombat.ps1 observed, so checking the
    earlier of the two invocations is the stronger proof."""
    fake_repo = tmp_path / "fake-repo"
    scripts_dir = fake_repo / "scripts"
    scripts_dir.mkdir(parents=True)
    wipe_copy = scripts_dir / "wipe-wombat.ps1"
    wipe_copy.write_text(_WIPE_SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")

    cwd_marker = tmp_path / "observed-cwd.txt"
    stub_stop = scripts_dir / "stop-wombat.ps1"
    stub_stop.write_text(
        f"Set-Content -LiteralPath '{cwd_marker}' -Value $PWD.Path\nexit 1\n",
        encoding="utf-8",
    )

    caller_cwd = tmp_path  # deliberately NOT fake_repo
    result = _run_powershell(
        f"Set-Location -LiteralPath '{caller_cwd}'; & '{wipe_copy}'; exit $LASTEXITCODE"
    )
    assert result.returncode == 1, (result.stdout, result.stderr)
    observed_cwd = cwd_marker.read_text(encoding="utf-8").strip()
    assert Path(observed_cwd).resolve() == fake_repo.resolve(), (
        f"expected stop-wombat.ps1 to observe cwd={fake_repo}, got {observed_cwd}"
    )
