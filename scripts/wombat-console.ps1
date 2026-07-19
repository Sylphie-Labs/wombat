<#
.SYNOPSIS
    TK-238/TK-260: launch the wombat runtime (python -m wombat) inside a
    visible, detached console window hosting a relaunch-with-backoff
    watchdog loop (DEC-52b); the runtime writes its own per-boot log file
    (TK-259, DEC-52a).

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
    the repo root and hosts a relaunch loop (TK-260, DEC-52b) around the
    venv python -m wombat: on every exit it prints a LOUD marker line
    (timestamp + exit code) to the console, waits a bounded backoff (starts
    at 5s, doubles per consecutive failure to a 300s cap, resets to 5s
    after >=10 minutes of healthy uptime), then respawns - the loop never
    exits on its own. The hosted console's command line carries the literal
    marker token "wombat-watchdog-host" so restart-wombat.ps1 can find and
    kill this host BEFORE the runtime python matches (kill-ordering
    hardened there so an intentional restart can never race a watchdog
    respawn). Per-boot file logging (logs/runtime-<yyyyMMdd-HHmmss>.log) is
    runtime-owned (TK-259, DEC-52a) - python -m wombat writes it directly,
    so the hosted loop never pipes through Tee-Object or writes any file of
    its own (custody lives in exactly one place; the Tee path proved
    unreliable - block-buffered piped stderr plus Tee-Object only creating
    its file on the first object meant healthy boots could produce zero
    bytes on disk). The console hosts the watchdog+runtime directly, so
    closing the window kills both (the DEC-42 kill affordance); Start-Process
    detaches the console from this caller so the caller exiting does not end
    the watchdog or the runtime (the exit-5 incident precedent).
    Exits 0 only after a bounded-wait CIM assert finds exactly ONE matching
    ROOT process; exits nonzero on any failure to reach that state.

    TESTABILITY: this file is dot-source-safe - all the functions below
    (including the pure backoff calculator) are defined unconditionally,
    but the launch/guard/assert action code only runs when the file is
    invoked directly (not dot-sourced), so Pester can `. .\wombat-console.ps1`
    to unit-test the functions without ever starting a process.

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
    # ISS-24: anchored to end-or-whitespace so 'python -m wombat.settings_app'
    # never false-positives as a runtime match. CIM CommandLines can carry a
    # trailing space (TK-238) - that trailing space IS whitespace, so `\s`
    # still honors it.
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -match '-m wombat(\s|$)' }
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

# TK-260 (DEC-52b): pure backoff calculator for the watchdog loop hosted in
# the spawned console - kept as a standalone, side-effect-free function so
# it is directly unit-testable. Given the delay just used and how long that
# boot stayed up, returns the delay to use for the NEXT respawn: reset to
# the 5s floor once uptime clears the healthy threshold, otherwise double
# the previous delay up to the cap.
function Get-WombatNextBackoffDelaySeconds {
    param(
        [Parameter(Mandatory)][int]$CurrentDelaySeconds,
        [Parameter(Mandatory)][double]$UptimeSeconds,
        [int]$CapSeconds = 300,
        [double]$HealthyThresholdSeconds = 600,
        [int]$FloorSeconds = 5
    )
    if ($UptimeSeconds -ge $HealthyThresholdSeconds) {
        return $FloorSeconds
    }
    $doubled = $CurrentDelaySeconds * 2
    if ($doubled -gt $CapSeconds) {
        return $CapSeconds
    }
    return $doubled
}

# Builds the text of the command hosted in the spawned console: the marker
# token "wombat-watchdog-host" (literal, matched by restart-wombat.ps1's
# CIM query) plus a relaunch loop around `& $VenvPython -m wombat` using the
# backoff calculator above. Embedded as text (not invoked in this process)
# because the loop must run INSIDE the spawned console, which owns the
# runtime as its child (the DEC-42 kill-affordance and TK-260 watchdog
# both require the loop to live in that hosted process, not this launcher).
function New-WombatWatchdogInnerCommand {
    param(
        [Parameter(Mandatory)][string]$VenvPython,
        [Parameter(Mandatory)][string]$RepoRoot
    )
    $backoffFnBody = ${function:Get-WombatNextBackoffDelaySeconds}.ToString()
    @"
function Get-WombatNextBackoffDelaySeconds {
$backoffFnBody
}
`$WombatWatchdogMarker = 'wombat-watchdog-host'
Set-Location -LiteralPath '$RepoRoot'
`$delay = 5
while (`$true) {
    `$bootStart = Get-Date
    & '$VenvPython' -m wombat
    `$exitCode = `$LASTEXITCODE
    `$uptimeSeconds = ((Get-Date) - `$bootStart).TotalSeconds
    `$ts = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    Write-Host "[`$WombatWatchdogMarker] `$ts EXIT code=`$exitCode uptime=`${uptimeSeconds}s - respawning in `${delay}s"
    Start-Sleep -Seconds `$delay
    `$delay = Get-WombatNextBackoffDelaySeconds -CurrentDelaySeconds `$delay -UptimeSeconds `$uptimeSeconds
}
"@
}

# Action code below only runs on direct invocation (`powershell -File ...` or
# `& .\wombat-console.ps1`), never on dot-source - see TESTABILITY above.
if ($MyInvocation.InvocationName -ne '.') {
    $existing = @(Get-WombatRootProcesses)
    if ($existing.Count -ge 1) {
        Write-Error "wombat runtime already running ($($existing.Count) matching root process(es) found); refusing to start a second instance (ASMP-2)."
        exit 1
    }

    if (-not (Test-Path -LiteralPath $LogDir)) {
        New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
    }

    # TK-260 (DEC-52b): the hosted command is now a relaunch-with-backoff
    # watchdog loop, not a single run - see New-WombatWatchdogInnerCommand
    # above. TK-259 (DEC-52a): per-boot file logging is runtime-owned -
    # python -m wombat writes its own logs/runtime-<yyyyMMdd-HHmmss>.log
    # directly, so this stays free of Tee-Object or any file write of its
    # own (custody lives in exactly one place).
    $innerCommand = New-WombatWatchdogInnerCommand -VenvPython $venvPython -RepoRoot $repoRoot

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
}
