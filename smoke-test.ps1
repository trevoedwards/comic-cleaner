<#
Smoke-tests a built ComicCleaner.exe.

A PyInstaller build can succeed and still produce a binary that dies on startup
from a missing or unresolvable import, so this launches the real executable and
checks both that it survives and that it printed no traceback.
#>
[CmdletBinding()]
param(
    [string]$Exe = 'dist\ComicCleaner.exe',
    [int]$Seconds = 15
)

$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

if (-not (Test-Path $Exe)) { throw "Not found: $Exe. Run .\build.ps1 first." }

$full = (Resolve-Path $Exe).Path
$name = [System.IO.Path]::GetFileNameWithoutExtension($full)
Write-Host "Testing $Exe ($([math]::Round((Get-Item $full).Length / 1MB, 1)) MB)" -ForegroundColor Cyan

# System.Diagnostics.Process rather than Start-Process: it gives a handle that
# is never null and lets the two pipes be drained asynchronously, which avoids
# both the null-method and pipe-deadlock traps.
$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = $full
$psi.UseShellExecute = $false
$psi.CreateNoWindow = $true
$psi.RedirectStandardOutput = $true
$psi.RedirectStandardError = $true
# Render to an offscreen surface so no window appears and no display is needed.
$psi.EnvironmentVariables['QT_QPA_PLATFORM'] = 'offscreen'

$proc = [System.Diagnostics.Process]::Start($psi)
$errTask = $proc.StandardError.ReadToEndAsync()
$outTask = $proc.StandardOutput.ReadToEndAsync()

$exitedEarly = $proc.WaitForExit($Seconds * 1000)
$exitCode = if ($exitedEarly) { $proc.ExitCode } else { $null }

<#
A one-file build spawns a bootloader child that survives killing the parent and
keeps an exclusive lock on the .exe - enough to make the next build fail when it
tries to delete dist\. Clear every instance by name.
#>
Get-Process -Name $name -ErrorAction SilentlyContinue |
    Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Milliseconds 800

# The pipes close as the processes die, so these now complete.
$output = ([string]$errTask.Result + [string]$outTask.Result).Trim()

if ($exitedEarly) {
    Write-Host "FAILED - exited on its own with code $exitCode" -ForegroundColor Red
    if ($output) { Write-Host $output -ForegroundColor Red }
    exit 1
}

<#
Staying alive is not sufficient proof: the bootloader parent can outlive a
crashed child, so the process lingers while the app is already dead. That is
exactly how a broken build passed once - treat any traceback as a failure.
#>
if ($output -match 'Traceback \(most recent call last\)|ImportError|ModuleNotFoundError|Fatal Python error') {
    Write-Host 'FAILED - process survived but the app crashed on startup:' -ForegroundColor Red
    Write-Host $output -ForegroundColor Red
    exit 1
}

Write-Host "PASS - ran ${Seconds}s, no startup errors, exited cleanly on request." -ForegroundColor Green
if ($output) { Write-Host "Startup output:`n$output" -ForegroundColor Yellow }
exit 0
