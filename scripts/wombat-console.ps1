<#
.SYNOPSIS
    TK-238: launch the wombat runtime (python -m wombat) in a new, visible,
    detached console window, with output tee'd to a timestamped log file.

.DESCRIPTION
    DEC-42 / Q-116 pinned shape, amended by the root-match orchestrator
    ruling (TK-238): one logical launch stably yields TWO python.exe
    processes with byte-identical "-m wombat" command lines on this host
    (a venv-launcher parent plus its real child), so process IDENTITY for
    the single-instance guard and the post-start assert counts ROOT matches
    only - a matching process whose ParentProcessId is NOT itself in the
    match set. Single-instance guard: refuses loud (exit 1) if a root
    "-m wombat" python is already running (ASMP-2 - at most one drainer).
    Otherwise starts a detached visible powershell.exe console that cds to
    the repo root and runs the venv python -m wombat, piping 2>&1 through
    Tee-Object to logs/runtime-<yyyyMMdd-HHmmss>.log. The console hosts the
    pipeline directly, so closing the window kills the runtime (the kill
    affordance); Start-Process detaches the console from this caller so the
    caller exiting does not end the runtime (the exit-5 incident precedent).
    Exits 0 only after a bounded-wait CIM assert finds exactly ONE matching
    ROOT process; exits nonzero on any failure to reach that state.

.PARAMETER LogDir
    Directory for the timestamped runtime log file. Defaults to a "logs"
    directory under the repo root (gitignored).
#>

param(
    [string]$LogDir
)

$ErrorActionPreference = 'Stop'

# Resolve repo root and venv python from PSScriptRoot only - never from env.
$repoRoot = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $repoRoot '.venv\Scripts\python.exe'

if (-not $LogDir) {
    $LogDir = Join-Path $repoRoot 'logs'
}

# Pinned process-identity shape (Stop-Wombat.ps1:3-4) - used by the guard
# below and by the post-start assert.
function Get-WombatProcesses {
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -match '-m wombat' }
}

# Root-match ruling (TK-238 orchestrator ruling amending Q-116): identity for
# the single-instance guard and the post-start assert is ROOT matches only -
# a matching process whose ParentProcessId is NOT itself in the match set
# (excludes the venv-launcher parent -> real child pair that one logical
# launch stably produces on this host).
function Get-WombatRootProcesses {
    $all = @(Get-WombatProcesses)
    $matchedIds = @($all | ForEach-Object { $_.ProcessId })
    $all | Where-Object { $matchedIds -notcontains $_.ParentProcessId }
}

$existing = @(Get-WombatRootProcesses)
if ($existing.Count -ge 1) {
    Write-Error "wombat runtime already running ($($existing.Count) matching root process(es) found); refusing to start a second instance (ASMP-2)."
    exit 1
}

if (-not (Test-Path -LiteralPath $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
}

$timestamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$logFile = Join-Path $LogDir "runtime-$timestamp.log"

$innerCommand = "Set-Location -LiteralPath '$repoRoot'; & '$venvPython' -m wombat 2>&1 | Tee-Object -FilePath '$logFile'"

Start-Process -FilePath 'powershell.exe' `
    -ArgumentList @('-NoProfile', '-Command', $innerCommand) `
    -WorkingDirectory $repoRoot `
    -WindowStyle Normal | Out-Null

# Bounded-wait CIM assert: exit 0 only once exactly one ROOT match is
# confirmed (root-match ruling - see Get-WombatRootProcesses above).
$deadline = (Get-Date).AddSeconds(10)
$count = 0
while ((Get-Date) -lt $deadline) {
    $count = @(Get-WombatRootProcesses).Count
    if ($count -ge 1) {
        break
    }
    Start-Sleep -Milliseconds 250
}

if ($count -ne 1) {
    Write-Error "expected exactly one wombat runtime root process after launch, found $count."
    exit 1
}

exit 0
