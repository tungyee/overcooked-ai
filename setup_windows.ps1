param(
    [string]$GameDir = 'D:\steam\steamapps\common\Overcooked! 2',
    [string]$Python = 'python',
    [switch]$Deploy
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
& $pythonExe -m pip install -r (Join-Path $PSScriptRoot 'requirements-dev.txt')
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
& (Join-Path $PSScriptRoot 'plugin\build.ps1') -GameDir $GameDir -Deploy:$Deploy
if ($LASTEXITCODE -ne 0) { throw 'Plugin build failed.' }
