param(
    [string]$GameDir = $(if ($env:GAME_DIR) { $env:GAME_DIR } else { 'D:\steam\steamapps\common\Overcooked! 2' }),
    [string]$Compiler,
    [switch]$Deploy
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$managedDir = Join-Path $GameDir 'Overcooked2_Data\Managed'
$bepCore = Join-Path $GameDir 'BepInEx\core'
$buildDir = Join-Path $PSScriptRoot 'build'
$compiler = if ($Compiler) { $Compiler } else { Join-Path $projectRoot '.tools\roslyn-4.8.0\tasks\net472\csc.exe' }
$references = @('mscorlib.dll', 'System.dll', 'System.Core.dll', 'UnityEngine.dll',
    'UnityEngine.CoreModule.dll', 'UnityEngine.PhysicsModule.dll',
    'Assembly-CSharp.dll', 'Assembly-CSharp-firstpass.dll')
foreach ($reference in $references) {
    if (-not (Test-Path -LiteralPath (Join-Path $managedDir $reference))) { throw "Missing game assembly: $reference" }
}
if (-not (Test-Path -LiteralPath (Join-Path $bepCore 'BepInEx.dll'))) { throw 'Install BepInEx 5 x86 in the game directory first.' }
if ($Deploy -and (Get-Process -Name Overcooked2 -ErrorAction SilentlyContinue)) { throw 'Exit the game before deploying the plugin.' }
if (-not (Test-Path -LiteralPath $compiler)) {
    if ($Compiler) { throw "Compiler not found: $Compiler" }
    # 使用本地 Roslyn 编译器，不改变系统 SDK 或 PATH。
    $toolsDir = Join-Path $projectRoot '.tools'
    New-Item -ItemType Directory -Force -Path $toolsDir | Out-Null
    $archive = Join-Path $toolsDir 'roslyn-4.8.0.zip'
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $ProgressPreference = 'SilentlyContinue'
    Invoke-WebRequest -UseBasicParsing -TimeoutSec 60 -Uri 'https://api.nuget.org/v3-flatcontainer/microsoft.net.compilers.toolset/4.8.0/microsoft.net.compilers.toolset.4.8.0.nupkg' -OutFile $archive
    Expand-Archive -LiteralPath $archive -DestinationPath (Join-Path $toolsDir 'roslyn-4.8.0') -Force
}
New-Item -ItemType Directory -Force -Path $buildDir | Out-Null
$outputDll = Join-Path $buildDir 'Oc2AIPlugin.dll'
$compilerArgs = @('/nologo', '/nostdlib+', '/noconfig', '/target:library', '/langversion:7.3',
    "/reference:$bepCore\BepInEx.dll", "/out:$outputDll")
$compilerArgs += @($references | ForEach-Object { "/reference:$(Join-Path $managedDir $_)" })
$compilerArgs += @(Get-ChildItem -LiteralPath $PSScriptRoot -Filter *.cs -File | ForEach-Object { $_.FullName })
& $compiler @compilerArgs
if ($LASTEXITCODE -ne 0) { throw "Plugin compilation failed: $LASTEXITCODE" }
Write-Host "Built: $outputDll"
if ($Deploy) {
    if (Get-Process -Name Overcooked2 -ErrorAction SilentlyContinue) { throw 'Exit the game before deploying the plugin.' }
    $pluginsDir = Join-Path $GameDir 'BepInEx\plugins'
    New-Item -ItemType Directory -Force -Path $pluginsDir | Out-Null
    $targetDll = Join-Path $pluginsDir 'Oc2AIPlugin.dll'
    if (Test-Path -LiteralPath $targetDll) {
        $backupDir = Join-Path $projectRoot ('.runtime\backups\' + (Get-Date -Format 'yyyyMMdd-HHmmss-fff'))
        New-Item -ItemType Directory -Force -Path $backupDir | Out-Null
        Copy-Item -LiteralPath $targetDll -Destination $backupDir
    }
    Copy-Item -LiteralPath $outputDll -Destination $targetDll -Force
    Write-Host "Deployed: $targetDll"
}
