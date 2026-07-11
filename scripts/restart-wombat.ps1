<#
.SYNOPSIS
    TK-238: kill any running wombat runtime, prove it gone, then start
    exactly one fresh visible-console runtime.

.DESCRIPTION
    DEC-42 / Q-116 pinned shape, amended by the root-match orchestrator
    ruling (TK-238): kill remains unconditional on ALL matches (roots AND
    any children - Stop-Wombat.ps1 CIM commandline shape), and prove-gone
    waits bounded for ZERO matches of any kind (root-match identity only
    changes how the single-instance guard and post-start assert in
    wombat-console.ps1 COUNT a running instance, not what gets killed or
    what "gone" means here). Force-kills every "-m wombat" python match,
    polls the same query in a bounded wait (~10s) until the count is zero -
    failing loud nonzero if not proven gone - then invokes wombat-console.ps1
    and exits with its code. Exit codes are the TK-239 contract: 0 =
    restarted/started, nonzero = failed. Works whether the runtime is
    currently running, dead, or never started.

.PARAMETER LogDir
    Forwarded to wombat-console.ps1; see that script for the default.
#>

param(
    [string]$LogDir
)

$ErrorActionPreference = 'Stop'

$consoleScript = Join-Path $PSScriptRoot 'wombat-console.ps1'

# Pinned process-identity shape (Stop-Wombat.ps1:3-4) - the same helper used
# by wombat-console.ps1's single-instance guard and post-start assert.
function Get-WombatProcesses {
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -match '-m wombat' }
}

foreach ($proc in @(Get-WombatProcesses)) {
    Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
}

# Bounded-wait kill-verify: must prove zero matches before starting anew.
$deadline = (Get-Date).AddSeconds(10)
$count = @(Get-WombatProcesses).Count
while ($count -gt 0 -and (Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 250
    $count = @(Get-WombatProcesses).Count
}

if ($count -ne 0) {
    Write-Error "failed to stop all wombat runtime processes within the bounded wait; $count still running."
    exit 1
}

if ($LogDir) {
    & $consoleScript -LogDir $LogDir
} else {
    & $consoleScript
}

exit $LASTEXITCODE
