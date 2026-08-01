<#
.SYNOPSIS
    TK-337: the one-click quiesce-then-wipe operator wrapper - stop the
    runtime, run `python -m wombat wipe --confirm`, and deliberately leave
    the runtime stopped (DEC-75f).

.DESCRIPTION
    Homed in scripts/ per DEC-42 (scripts/ owns launch/kill/restart; the
    desktop launcher is a thin caller). Sequence, in order:
      (1) invoke scripts/stop-wombat.ps1 (the ONE stop implementation,
          shared with restart-wombat.ps1 - DEC-77 r6) and propagate its
          exit code. A failure to prove the runtime stopped aborts before
          any archive or destructive act.
      (2) invoke `python -m wombat wipe --confirm` (TK-334/TK-335's
          already-complete and already-safe archive-then-truncate engine),
          forwarding -ArchiveDir as --archive-dir when supplied, and
          propagate its exit code.
      (3) STOP THERE. This script deliberately does NOT restart the
          runtime - leaving it down is the designed end state (DEC-75f).
          The operator, or TK-336's UI prompt bound to the existing TK-239
          restart control, performs the restart as a separate, visible act.

    Exit-code contract is exactly TK-238/TK-239's: 0 on success, nonzero on
    any failure (stop failure or CLI failure) - TK-336's main-process seam
    keys off it. This script adds NO data-lifecycle logic and NO safety the
    CLI lacks; every archival and destructive decision lives in
    src/wombat/wipe.py.

.PARAMETER ArchiveDir
    Optional archive directory, forwarded to the CLI as --archive-dir.
    TK-336's Electron seam passes this so the main process knows the
    archive path without reading stdout. Absent, the CLI's own
    archives/wipe-<timestamp>/ default applies.
#>

param(
    [string]$ArchiveDir
)

$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $repoRoot '.venv\Scripts\python.exe'
$stopScript = Join-Path $PSScriptRoot 'stop-wombat.ps1'

# (1) Stop the runtime FIRST - see .DESCRIPTION. Propagate failure before
# any archive or destructive act runs.
& $stopScript
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

# (2) Run the CLI's already-safe archive-then-wipe engine.
$wipeArgs = @('-m', 'wombat', 'wipe', '--confirm')
if ($ArchiveDir) {
    $wipeArgs += @('--archive-dir', $ArchiveDir)
}

& $venvPython @wipeArgs
$wipeExitCode = $LASTEXITCODE
if ($wipeExitCode -ne 0) {
    exit $wipeExitCode
}

# (3) STOP HERE - deliberately no restart (DEC-75f). The operator (or
# TK-336's UI prompt bound to the existing TK-239 restart control) performs
# the restart as a separate, visible act.
Write-Output "wombat wipe complete. The runtime is stopped and was NOT restarted - use restart-wombat.ps1 (or the app's restart control) to bring it back up."

exit 0
