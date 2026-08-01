<#
.SYNOPSIS
    TK-337 (DEC-77 r6): the standalone stop step - force-kill any running
    wombat watchdog host and runtime, then prove both gone.

.DESCRIPTION
    Extracted VERBATIM from restart-wombat.ps1's former inline stop block so
    exactly one stop implementation exists process-wide; both
    restart-wombat.ps1 and wipe-wombat.ps1 invoke this script and propagate
    its exit code before proceeding to their own next step.

    Ordering (TK-260, DEC-52b kill-ordering hardening): wombat-console.ps1's
    hosted console runs a relaunch-with-backoff watchdog loop around the
    runtime, identified by the literal marker token "wombat-watchdog-host"
    carried in its command line. Killing only the runtime python and leaving
    the watchdog alive would let the surviving watchdog observe the kill as
    an ordinary crash and respawn a second runtime, racing whatever the
    caller does next. Steps: (1) force-kill every watchdog-host match, (2)
    force-kill every "-m wombat" python match (roots AND any children -
    root-match identity only changes how wombat-console.ps1's single-instance
    guard and post-start assert COUNT a running instance, not what gets
    killed or what "gone" means here), (3) bounded-wait (~10s) proving ZERO
    matches of BOTH kinds, failing loud nonzero if not proven gone.

    Exits 0 once proven gone; Write-Error + nonzero otherwise. Works whether
    the watchdog/runtime is currently running, dead, or never started.
#>

$ErrorActionPreference = 'Stop'

# TK-260 (DEC-52b): the watchdog host is the powershell.exe console spawned
# by wombat-console.ps1 hosting the relaunch loop - identified by the
# literal marker token in its command line (see
# New-WombatWatchdogInnerCommand in wombat-console.ps1).
function Get-WombatWatchdogHostProcesses {
    Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" |
        Where-Object { $_.CommandLine -match 'wombat-watchdog-host' }
}

# Pinned process-identity shape - the same helper used by
# wombat-console.ps1's single-instance guard and post-start assert.
function Get-WombatProcesses {
    # ISS-24: anchored to end-or-whitespace so 'python -m wombat.settings_app'
    # never false-positives as a runtime match. CIM CommandLines can carry a
    # trailing space (TK-238) - that trailing space IS whitespace, so `\s`
    # still honors it.
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -match '-m wombat(\s|$)' }
}

# (1) Kill the watchdog host(s) FIRST - see .DESCRIPTION ordering rationale
# above. Without this ordering, a surviving watchdog would respawn a second
# runtime after step (2) kills the one below it.
foreach ($proc in @(Get-WombatWatchdogHostProcesses)) {
    Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
}

# (2) Kill all runtime python matches.
foreach ($proc in @(Get-WombatProcesses)) {
    Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
}

# (3) Bounded-wait kill-verify: must prove zero matches of BOTH kinds before
# the caller proceeds.
$deadline = (Get-Date).AddSeconds(10)
$watchdogCount = @(Get-WombatWatchdogHostProcesses).Count
$procCount = @(Get-WombatProcesses).Count
while (($watchdogCount -gt 0 -or $procCount -gt 0) -and (Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 250
    $watchdogCount = @(Get-WombatWatchdogHostProcesses).Count
    $procCount = @(Get-WombatProcesses).Count
}

if ($watchdogCount -ne 0 -or $procCount -ne 0) {
    Write-Error "failed to stop all wombat watchdog/runtime processes within the bounded wait; $watchdogCount watchdog host(s), $procCount runtime process(es) still running."
    exit 1
}

exit 0
