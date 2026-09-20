<#
Builds dist\ComicCleaner.exe - a single portable file, no installer.

PyInstaller cannot cross-compile, so this has to run on Windows. Everything it
needs comes from the project-local .venv.
#>
[CmdletBinding()]
param(
    # Emit a folder instead of one file. Starts faster and is easier to debug.
    [switch]$OneDir,
    # Keep the console window, so tracebacks are visible.
    [switch]$Console,
    [switch]$Clean
)

$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

<#
Windows PowerShell 5.1 turns anything a native .exe writes to stderr into a
NativeCommandError whenever stderr is redirected - which $ErrorActionPreference
= 'Stop' then treats as fatal. PyInstaller and pip both log normal progress to
stderr, so every native call goes through here: preference relaxed for the call
itself, success judged by the exit code instead.
#>
function Invoke-Native {
    param(
        [Parameter(Mandatory)][string]$File,
        [string[]]$Arguments = @(),
        [Parameter(Mandatory)][string]$What
    )
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { & $File @Arguments } finally { $ErrorActionPreference = $previous }
    if ($LASTEXITCODE -ne 0) { throw "$What failed (exit code $LASTEXITCODE)." }
}

$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) {
    throw "No .venv found. Run: py -3.12 -m venv .venv; .\.venv\Scripts\python.exe -m pip install -e `".[dev]`""
}

# Check the venv on disk rather than shelling out. PowerShell 5.1 mangles quotes
# inside arguments to a native command, so `python -c "import PyInstaller"` is
# not reliable here - and a filesystem probe cannot fail halfway.
$sitePackages = Join-Path $PSScriptRoot '.venv\Lib\site-packages'
$hasPyInstaller = Test-Path (Join-Path $sitePackages 'PyInstaller')

if (-not $hasPyInstaller) {
    Write-Host 'Installing PyInstaller into the venv...' -ForegroundColor Cyan
    Invoke-Native -File $python -Arguments @('-m', 'pip', 'install', '--quiet', 'pyinstaller') `
        -What 'pip install pyinstaller'
}

if ($Clean) {
    foreach ($dir in 'build', 'dist') {
        if (Test-Path $dir) { Remove-Item -Recurse -Force $dir }
    }
    Get-ChildItem -Filter '*.spec' | Remove-Item -Force -ErrorAction SilentlyContinue
}

# Qt ships a lot we never touch. These are safe to drop - QtNetwork,
# QtPrintSupport and QtOpenGL are deliberately NOT excluded, because QtWidgets
# can pull them in at import time.
$excluded = @(
    'PySide6.QtWebEngineCore', 'PySide6.QtWebEngineWidgets', 'PySide6.QtWebEngineQuick',
    'PySide6.QtQuick', 'PySide6.QtQuick3D', 'PySide6.QtQml',
    'PySide6.Qt3DCore', 'PySide6.Qt3DRender',
    'PySide6.QtCharts', 'PySide6.QtDataVisualization',
    'PySide6.QtMultimedia', 'PySide6.QtMultimediaWidgets',
    'PySide6.QtSql', 'PySide6.QtTest', 'PySide6.QtDesigner',
    'PySide6.QtBluetooth', 'PySide6.QtPositioning', 'PySide6.QtSerialPort',
    'tkinter', 'pydoc_data'
)

$arguments = @(
    '-m', 'PyInstaller',
    '--noconfirm',
    '--name', 'ComicCleaner',
    '--paths', 'src',
    '--collect-submodules', 'comiccleaner',
    # --collect-submodules only gathers code, so the icon needs saying too.
    '--add-data', 'src/comiccleaner/assets;comiccleaner/assets'
)
$arguments += if ($OneDir) { '--onedir' } else { '--onefile' }
$arguments += if ($Console) { '--console' } else { '--windowed' }
foreach ($module in $excluded) { $arguments += @('--exclude-module', $module) }

$icon = Join-Path $PSScriptRoot 'assets\comiccleaner.ico'
if (Test-Path $icon) { $arguments += @('--icon', $icon) }

$arguments += 'src\comiccleaner\__main__.py'

Write-Host 'Building (this takes a few minutes)...' -ForegroundColor Cyan
Invoke-Native -File $python -Arguments $arguments -What 'PyInstaller'

$output = if ($OneDir) { 'dist\ComicCleaner\ComicCleaner.exe' } else { 'dist\ComicCleaner.exe' }
if (-not (Test-Path $output)) { throw "Build reported success but $output is missing." }

$sizeMb = [math]::Round((Get-Item $output).Length / 1MB, 1)
Write-Host ''
Write-Host "Built $output ($sizeMb MB)" -ForegroundColor Green
Write-Host 'Verify it starts with: .\smoke-test.ps1'
Write-Host 'Note: .cbr/.cb7 support still needs 7-Zip or WinRAR on the target machine.'
