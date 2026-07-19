<#
.SYNOPSIS
    TK-238/TK-260: kill any running wombat watchdog host + runtime, prove
    both gone, then start exactly one fresh visible-console watchdog+runtime.

.DESCRIPTION
    DEC-42 / Q-116 pinned shape, amended by the root-match orchestrator
    ruling (TK-238) and by the TK-260 (DEC-52b) kill-ordering hardening:
    wombat-console.ps1's hosted console now runs a relaunch-with-backoff
    watchdog loop around the runtime, identified by the literal marker
    token "wombat-watchdog-host" carried in its command line. An
    intentional restart MUST kill that watchdog HOST first - killing only
    the runtime python and leaving the watchdog alive would let the
    surviving watchdog observe the kill as an ordinary crash and respawn a
    second runtime, racing the fresh one this script is about to start.
    Ordering: (1) force-kill every watchdog-host match, (2) force-kill every
    "-m wombat" python match (roots AND any children - Stop-Wombat.ps1 CIM
    commandline shape; root-match identity only changes how the single-
    instance guard and post-start assert in wombat-console.ps1 COUNT a
    running instance, not what gets killed or what "gone" means here),
    (3) bounded-wait (~10s) proving ZERO matches of BOTH kinds, failing
    loud nonzero if not proven gone - only then does it invoke
    wombat-console.ps1 and exit with its code. Exit codes are the TK-239
    contract: 0 = restarted/started, nonzero = failed. Works whether the
    watchdog/runtime is currently running, dead, or never started.

.PARAMETER LogDir
    Forwarded to wombat-console.ps1; see that script for the default.
#>

param(
    [string]$LogDir
)

$ErrorActionPreference = 'Stop'

$consoleScript = Join-Path $PSScriptRoot 'wombat-console.ps1'

# TK-260 (DEC-52b): the watchdog host is the powershell.exe console spawned
# by wombat-console.ps1 hosting the relaunch loop - identified by the
# literal marker token in its command line (see
# New-WombatWatchdogInnerCommand in wombat-console.ps1).
function Get-WombatWatchdogHostProcesses {
    Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" |
        Where-Object { $_.CommandLine -match 'wombat-watchdog-host' }
}

# Pinned process-identity shape (Stop-Wombat.ps1:3-4) - the same helper used
# by wombat-console.ps1's single-instance guard and post-start assert.
function Get-WombatProcesses {
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -match '-m wombat' }
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
# starting anew.
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

if ($LogDir) {
    & $consoleScript -LogDir $LogDir
} else {
    & $consoleScript
}

exit $LASTEXITCODE
