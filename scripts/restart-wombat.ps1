<#
.SYNOPSIS
    TK-238/TK-260: kill any running wombat watchdog host + runtime, prove
    both gone, then start exactly one fresh visible-console watchdog+runtime.

.DESCRIPTION
    DEC-42 / Q-116 pinned shape, amended by the root-match orchestrator
    ruling (TK-238), the TK-260 (DEC-52b) kill-ordering hardening, and the
    TK-337 (DEC-77 r6) extraction: the stop step (kill the watchdog host,
    kill the runtime, bounded-wait proving both gone) now lives in
    scripts/stop-wombat.ps1 - the ONE stop implementation shared with
    wipe-wombat.ps1. This script invokes it and propagates its exit code
    before proceeding; only on success does it invoke wombat-console.ps1
    and exit with its code. Exit codes are the TK-239 contract: 0 =
    restarted/started, nonzero = failed. Works whether the watchdog/runtime
    is currently running, dead, or never started.

.PARAMETER LogDir
    Forwarded to wombat-console.ps1; see that script for the default.
#>

param(
    [string]$LogDir
)

$ErrorActionPreference = 'Stop'

$stopScript = Join-Path $PSScriptRoot 'stop-wombat.ps1'
$consoleScript = Join-Path $PSScriptRoot 'wombat-console.ps1'

# TK-337 (DEC-77 r6): the extracted, single stop implementation. Propagate
# its exit code before proceeding - a failure to prove the runtime stopped
# must not be followed by a fresh launch attempt.
& $stopScript
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

if ($LogDir) {
    & $consoleScript -LogDir $LogDir
} else {
    & $consoleScript
}

exit $LASTEXITCODE
