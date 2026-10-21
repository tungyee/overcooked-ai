param(
    [string]$GameDir = $(if ($env:GAME_DIR) { $env:GAME_DIR } else { 'D:\steam\steamapps\common\Overcooked! 2' }),
    [string]$Python = 'python',
    [switch]$Deploy,
    [switch]$Voice,
    [switch]$Dev
)
$ErrorActionPreference = 'Stop'
if (-not (Test-Path -LiteralPath (Join-Path $GameDir 'BepInEx\core\BepInEx.dll'))) {
    throw 'Install BepInEx 5 x86 for the game before running this setup.'
}
$pythonExe = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonExe)) {
    & $Python -m venv (Join-Path $PSScriptRoot '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Virtual environment creation failed.' }
}
& $pythonExe -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'
if ($LASTEXITCODE -ne 0) { throw 'Python 3.9 or newer is required.' }
if ($Voice -or $Dev) {
    $requirements = if ($Dev) { 'requirements-dev.txt' } else { 'requirements-voice.txt' }
    & $pythonExe -m pip install -r (Join-Path $PSScriptRoot $requirements)
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
}
& (Join-Path $PSScriptRoot 'plugin\build.ps1') -GameDir $GameDir -Deploy:$Deploy
if ($LASTEXITCODE -ne 0) { throw 'Plugin build failed.' }
