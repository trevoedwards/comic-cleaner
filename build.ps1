<#
Convenience wrapper around build.py for Windows.

The real build logic lives in build.py so that Windows, macOS and Linux all
share one implementation. PyInstaller cannot cross-compile, so this still has
to run on the OS you are targeting.
#>
[CmdletBinding()]
param(
    # Emit a folder instead of one file. Starts faster and is easier to debug.
    [switch]$OneDir,
    # Keep the console window, so tracebacks are visible.
    [switch]$Console,
    [switch]$Clean,
    # Launch the result afterwards and check it does not crash.
    [switch]$SmokeTest
)

$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) { $python = 'python' }

$arguments = @((Join-Path $PSScriptRoot 'build.py'))
if ($OneDir)    { $arguments += '--onedir' }
if ($Console)   { $arguments += '--console' }
if ($Clean)     { $arguments += '--clean' }
if ($SmokeTest) { $arguments += '--smoke-test' }

<#
Windows PowerShell 5.1 turns anything a native .exe writes to stderr into a
NativeCommandError whenever stderr is redirected, which $ErrorActionPreference
= 'Stop' then treats as fatal. PyInstaller logs normal progress to stderr, so
the preference is relaxed for the call and success judged by the exit code.
#>
$previous = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
try { & $python @arguments } finally { $ErrorActionPreference = $previous }

if ($LASTEXITCODE -ne 0) { throw "Build failed (exit code $LASTEXITCODE)." }
