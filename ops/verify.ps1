<#
.SYNOPSIS
  Local verification for Windows PowerShell 5.1 and PowerShell 7.

.DESCRIPTION
  Every step is a single Python invocation whose output goes to a log file. The exit code is
  read from $LASTEXITCODE immediately after the native command (never after a pipeline such as
  `| Select-Object -Last 3`). All requested steps run, and the script exits with the original
  exit code of the first failing step.

  Steps: lint format types tests audit secrets build demo (default: all of them)
  Negative-control steps (never run by default): selftest-pass selftest-fail

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File ops\verify.ps1 -Steps tests,demo
#>
[CmdletBinding()]
param(
    [string[]]$Steps = @('lint', 'format', 'types', 'tests', 'audit', 'secrets', 'build', 'demo'),
    [string]$Python = 'python',
    [string]$LogDir = 'reports\verify'
)

$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)
# A missing interpreter must fail loudly; otherwise $LASTEXITCODE would keep a stale value.
if (-not (Get-Command $Python -ErrorAction SilentlyContinue)) {
    [Console]::Error.WriteLine("Python interpreter not found: $Python")
    exit 2
}
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$env:PYTHONPATH = @('src', 'lab', 'evaluations', 'sampleapp') -join [IO.Path]::PathSeparator

$definitions = @{
    'lint'          = @('-m', 'ruff', 'check', '.')
    'format'        = @('-m', 'ruff', 'format', '--check', '.')
    'types'         = @('-m', 'mypy')
    'tests'         = @('-m', 'unittest', 'discover', '-s', 'tests', '-t', '.')
    'audit'         = @('-m', 'pip_audit', '--strict', '--require-hashes', '-r', 'requirements-dev.txt')
    'secrets'       = @('ops/ci/secret_scan.py')
    'build'         = @('ops/ci/build_check.py')
    'demo'          = @('-m', 'accessguard_lab', 'demo', '--out', (Join-Path $LogDir 'demo'))
    'selftest-pass' = @('-c', "print('negative control: passing step')")
    'selftest-fail' = @('-c', "print('negative control: deliberately failing step'); raise SystemExit(3)")
}

# Accept both -Steps a,b and -Steps "a,b" (the latter is how -File passes a single string).
$Steps = @($Steps | ForEach-Object { $_ -split ',' } | Where-Object { $_ })
foreach ($step in $Steps) {
    if (-not $definitions.ContainsKey($step)) {
        [Console]::Error.WriteLine("Unknown step: $step")
        exit 2
    }
}

$firstFailure = 0
$summary = @()
foreach ($step in $Steps) {
    $arguments = $definitions[$step]
    $log = Join-Path $LogDir "$step.log"
    Write-Output "==> ${step}: $Python $($arguments -join ' ')"
    # Native stderr must not become a terminating error in Windows PowerShell 5.1.
    $ErrorActionPreference = 'Continue'
    $global:LASTEXITCODE = $null
    & $Python @arguments *> $log
    $code = $LASTEXITCODE
    if ($null -eq $code) { $code = 1 }  # the native command did not run at all
    $ErrorActionPreference = 'Stop'
    if (Test-Path $log) { Get-Content -Path $log -Tail 15 }  # display only; $code is kept
    if ($code -eq 0) {
        $summary += "ok      $step"
    } else {
        $summary += "FAILED  $step (exit $code, log: $log)"
        if ($firstFailure -eq 0) { $firstFailure = $code }
    }
}

Write-Output ''
Write-Output 'Verification summary:'
$summary | ForEach-Object { Write-Output "  $_" }
if ($firstFailure -ne 0) {
    Write-Output "Verification FAILED (exit $firstFailure)"
} else {
    Write-Output 'Verification passed'
}
exit $firstFailure
