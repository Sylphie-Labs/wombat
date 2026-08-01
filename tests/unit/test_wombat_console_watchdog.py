"""tests/unit/test_wombat_console_watchdog.py — TK-260 (DEC-52b): the console-host
watchdog. wombat-console.ps1's hosted command becomes a relaunch-with-backoff loop
around ``python -m wombat``, carrying the literal marker token "wombat-watchdog-host"
so restart-wombat.ps1 can find and kill it FIRST, before the runtime python matches.

Builders must never launch a live wombat runtime. Every check here is either static
text/order inspection of the two scripts, or a direct call into the pure, side-effect-free
``Get-WombatNextBackoffDelaySeconds`` function via ``powershell -NoProfile -Command
". <script>; Get-WombatNextBackoffDelaySeconds ..."`` — dot-sourcing is safe because
wombat-console.ps1 guards all of its action code (the singleton check, Start-Process,
and the post-start assert) behind ``if ($MyInvocation.InvocationName -ne '.')``, so
dot-sourcing only defines functions and never starts a process.

  AC1 (external force-kill -> loud marker + respawn): the hosted loop prints a marker
      line containing the timestamp+exit code on every exit, and the marker token
      "wombat-watchdog-host" is present in the script text (static).
  AC2 (crash-loop backoff): Get-WombatNextBackoffDelaySeconds doubles 5->10->20...
      capped at 300, and resets to 5 once uptime clears the healthy threshold
      (direct pure-function calls, no process spawned).
  AC3 (stop-wombat.ps1 ordering): TK-337 (DEC-77 r6) extracted the watchdog-host-then-
      runtime kill loop and bounded-wait out of restart-wombat.ps1 into standalone
      scripts/stop-wombat.ps1 — the ONE stop implementation, also used by
      wipe-wombat.ps1. These checks now read stop-wombat.ps1's source for the ordering
      and bounded-wait shape, and assert restart-wombat.ps1 invokes it.
  AC4 (closing the console kills both): unchanged single-console-hosts-both-processes
      architecture (structural; not independently re-provable without a live process —
      reviewer/operator-driven per the briefing).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CONSOLE_SCRIPT = _REPO_ROOT / "scripts" / "wombat-console.ps1"
_RESTART_SCRIPT = _REPO_ROOT / "scripts" / "restart-wombat.ps1"
_STOP_SCRIPT = _REPO_ROOT / "scripts" / "stop-wombat.ps1"

_POWERSHELL = shutil.which("powershell.exe") or shutil.which("pwsh.exe")

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="wombat-console.ps1/restart-wombat.ps1 are Windows-only scripts"
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
# AC1 + static structure
# ---------------------------------------------------------------------------


def test_watchdog_marker_token_present() -> None:
    content = _CONSOLE_SCRIPT.read_text(encoding="utf-8")
    assert "wombat-watchdog-host" in content


def test_console_script_loud_exit_marker_has_timestamp_and_exit_code() -> None:
    content = _CONSOLE_SCRIPT.read_text(encoding="utf-8")
    assert "EXIT code=" in content
    assert "$ts" in content and "Get-Date -Format" in content


def test_console_script_loop_never_self_exits() -> None:
    content = _CONSOLE_SCRIPT.read_text(encoding="utf-8")
    assert "while (`$true) {" in content or "while ($true) {" in content


def test_console_script_writes_no_files_of_its_own() -> None:
    # TK-260 non_goal: log custody is runtime-owned (TK-259); the watchdog writes
    # only to the console, never a file. (Docstring prose may still reference
    # Tee-Object by name to explain why it was removed - the piping itself is gone.)
    content = _CONSOLE_SCRIPT.read_text(encoding="utf-8")
    assert "| Tee-Object" not in content
    assert "Out-File" not in content
    assert "New-Item -ItemType File" not in content


def test_console_script_dot_source_safe(tmp_path: Path) -> None:
    """Dot-sourcing must define functions only and never start a process (no hang,
    no error) — proves the guard around the action code works."""
    script_literal = str(_CONSOLE_SCRIPT).replace("'", "''")
    command = (
        f". '{script_literal}'; "
        "if (Get-Command Get-WombatNextBackoffDelaySeconds -ErrorAction SilentlyContinue) "
        "{ Write-Output 'FUNCTION_DEFINED' } else { Write-Output 'MISSING' }"
    )
    result = _run_powershell(command)
    assert result.returncode == 0, result.stderr
    assert "FUNCTION_DEFINED" in result.stdout


# ---------------------------------------------------------------------------
# AC2 — pure backoff calculator, called for real (no process spawned)
# ---------------------------------------------------------------------------


def _next_delay(current: int, uptime: float) -> int:
    script_literal = str(_CONSOLE_SCRIPT).replace("'", "''")
    command = (
        f". '{script_literal}'; "
        f"Get-WombatNextBackoffDelaySeconds -CurrentDelaySeconds {current} -UptimeSeconds {uptime}"
    )
    result = _run_powershell(command)
    assert result.returncode == 0, result.stderr
    return int(result.stdout.strip())


def test_backoff_starts_at_five_and_doubles() -> None:
    assert _next_delay(5, 1.0) == 10
    assert _next_delay(10, 1.0) == 20
    assert _next_delay(20, 1.0) == 40


def test_backoff_caps_at_300() -> None:
    assert _next_delay(200, 1.0) == 300
    assert _next_delay(300, 1.0) == 300


def test_backoff_resets_after_healthy_uptime() -> None:
    assert _next_delay(300, 600.0) == 5
    assert _next_delay(150, 900.0) == 5


def test_backoff_does_not_reset_below_healthy_threshold() -> None:
    assert _next_delay(40, 599.0) == 80


# ---------------------------------------------------------------------------
# AC3 — stop-wombat.ps1 kill ordering (TK-337/DEC-77 r6: extracted out of
# restart-wombat.ps1 into the one shared stop implementation)
# ---------------------------------------------------------------------------


def test_restart_script_kills_watchdog_host_before_runtime_python() -> None:
    content = _STOP_SCRIPT.read_text(encoding="utf-8")
    watchdog_kill_idx = content.index("Get-WombatWatchdogHostProcesses)) {")
    runtime_kill_idx = content.index("Get-WombatProcesses)) {")
    assert watchdog_kill_idx < runtime_kill_idx, (
        "stop-wombat.ps1 must kill watchdog-host matches before runtime python "
        "matches, or a surviving watchdog can respawn a second runtime"
    )


def test_restart_script_proves_zero_matches_of_both_kinds() -> None:
    content = _STOP_SCRIPT.read_text(encoding="utf-8")
    assert "watchdogCount" in content
    assert "procCount" in content
    assert "watchdogCount -ne 0 -or $procCount -ne 0" in content


def test_restart_script_watchdog_marker_matches_console_script_marker() -> None:
    stop_content = _STOP_SCRIPT.read_text(encoding="utf-8")
    console_content = _CONSOLE_SCRIPT.read_text(encoding="utf-8")
    assert "'wombat-watchdog-host'" in console_content
    assert "wombat-watchdog-host" in stop_content


def test_restart_script_invokes_stop_wombat_script() -> None:
    """TK-337 (DEC-77 r6): restart-wombat.ps1 no longer kills anything inline — it
    invokes the extracted scripts/stop-wombat.ps1 and propagates its exit code."""
    restart_content = _RESTART_SCRIPT.read_text(encoding="utf-8")
    assert "stop-wombat.ps1" in restart_content
    assert "& $stopScript" in restart_content
    assert "Stop-Process" not in restart_content
