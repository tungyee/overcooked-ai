param(
    [string]$GameDir = $(if ($env:GAME_DIR) { $env:GAME_DIR } else { 'D:\steam\steamapps\common\Overcooked! 2' }),
    [switch]$DryRun,
    [switch]$Background,
    [double]$Seconds = 0,
    [ValidateSet('right_ctrl', 'right_shift', 'slash')][string]$PickupKey = 'right_shift',
    [ValidateSet('right_ctrl', 'right_shift', 'slash')][string]$ChopKey = 'right_ctrl'
)
$ErrorActionPreference = 'Stop'
$pythonExe = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonExe)) { throw 'Run setup_windows.ps1 first.' }
if (-not (Test-Path -LiteralPath (Join-Path $GameDir 'BepInEx\plugins\Oc2AIPlugin.dll'))) { throw 'AI plugin is missing. Run setup_windows.ps1.' }
$runtimeDir = Join-Path $PSScriptRoot '.runtime'
New-Item -ItemType Directory -Force -Path $runtimeDir | Out-Null
$lockFile = Join-Path $runtimeDir 'agent.lock'
try { $agentLock = [IO.File]::Open($lockFile, 'OpenOrCreate', 'ReadWrite', 'None') }
catch { throw 'An agent started by this script is already running.' }
$stopFile = Join-Path $runtimeDir 'stop-agent'
try {
    if (Test-Path -LiteralPath $stopFile) { Remove-Item -LiteralPath $stopFile }
    if (-not (Get-Process -Name Overcooked2 -ErrorAction SilentlyContinue)) {
        # Steam 负责启动，Windows Doorstop 自动注入 BepInEx。
        Start-Process 'steam://rungameid/728880' -WindowStyle Normal
    }
    Write-Host 'Waiting for game plugin on 127.0.0.1:27411 ...'
    $connected = $false
    for ($attempt = 0; $attempt -lt 60; $attempt++) {
        $probe = New-Object Net.Sockets.TcpClient
        try {
            $task = $probe.ConnectAsync('127.0.0.1', 27411)
            if ($task.Wait(500) -and $probe.Connected) { $connected = $true; break }
        } catch {} finally { $probe.Close() }
        Start-Sleep -Milliseconds 500
    }
    if (-not $connected) { throw "Plugin did not start. Check $GameDir\BepInEx\LogOutput.log" }
    $gameProcess = Get-Process -Name Overcooked2 -ErrorAction Stop | Select-Object -First 1
    $privilegeJson = & $pythonExe -X utf8 (Join-Path $PSScriptRoot 'tools\windows_privileges.py') --pid $gameProcess.Id
    if ($LASTEXITCODE -ne 0) { throw 'Cannot determine game input privileges.' }
    $privileges = $privilegeJson | ConvertFrom-Json
    if (-not $DryRun -and $privileges.requires_elevation) {
        Write-Host 'The game is elevated. Windows UAC permission is required to inject P2 input.'
        $agentLock.Dispose()
        $agentLock = $null
        $elevatedArgs = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"' + $PSCommandPath + '"'),
            '-GameDir', ('"' + $GameDir + '"'), '-PickupKey', $PickupKey, '-ChopKey', $ChopKey)
        if ($Seconds -gt 0) { $elevatedArgs += @('-Seconds', "$Seconds") }
        if ($Background) { $elevatedArgs += '-Background' }
        $style = if ($Background) { 'Hidden' } else { 'Normal' }
        $elevatedProcess = Start-Process -FilePath 'powershell.exe' -Verb RunAs -ArgumentList $elevatedArgs -WorkingDirectory $PSScriptRoot -WindowStyle $style -PassThru
        Set-Content -LiteralPath (Join-Path $runtimeDir 'launcher.pid') -Value $elevatedProcess.Id
        return
    }
    $agentArgs = @('-X', 'utf8', '-u', (Join-Path $PSScriptRoot 'run_agent.py'), '--stop-file', $stopFile,
        '--pickup-key', $PickupKey, '--chop-key', $ChopKey)
    if ($DryRun) { $agentArgs += '--dry-run' }
    if ($Seconds -gt 0) { $agentArgs += @('--seconds', "$Seconds") }
    Write-Host 'AI ready. Select Split Keyboard and enter a local two-player level (recommended: story 1-4).'
    Write-Host 'AI pauses while the game is unfocused. Stop with Ctrl+C or stop_windows.ps1.'
    Push-Location $PSScriptRoot
    try {
        if ($Background) {
            # PowerShell 5.1 会把 Python stderr 当成 NativeCommandError；
            # 用独立进程重定向，避免连接提示被 ErrorActionPreference=Stop 中断。
            $agentProcess = Start-Process -FilePath $pythonExe -ArgumentList $agentArgs -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $runtimeDir 'agent-stdout.log') -RedirectStandardError (Join-Path $runtimeDir 'agent-stderr.log') -PassThru
            Set-Content -LiteralPath (Join-Path $runtimeDir 'agent.pid') -Value $agentProcess.Id
            $processHandle = $agentProcess.Handle
            $agentProcess.WaitForExit()
            $agentExitCode = $agentProcess.ExitCode
        } else {
            & $pythonExe @agentArgs
            $agentExitCode = $LASTEXITCODE
        }
    } finally { Pop-Location }
    if ($agentExitCode -ne 0) { throw "Agent exited with code $agentExitCode" }
} catch {
    $_ | Out-String | Set-Content -LiteralPath (Join-Path $runtimeDir 'launcher-error.log')
    throw
} finally { if ($agentLock) { $agentLock.Dispose() } }
