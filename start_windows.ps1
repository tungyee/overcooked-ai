param(
    [string]$GameDir = $(if ($env:GAME_DIR) { $env:GAME_DIR } else { 'D:\steam\steamapps\common\Overcooked! 2' }),
    [switch]$DryRun,
    [switch]$Restart,
    [ValidateRange(0, 86400)][double]$Seconds = 0,
    [switch]$Voice,
    [switch]$NoVoice,
    [switch]$VoiceDebug,
    [string]$EnvFile,
    [switch]$NoEnvFile,
    [string]$VoiceLog
)
$ErrorActionPreference = 'Stop'
$pythonExe = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonExe)) { throw 'Run setup_windows.ps1 first.' }
if (-not (Test-Path -LiteralPath (Join-Path $GameDir 'BepInEx\plugins\Oc2AIPlugin.dll'))) {
    throw 'AI plugin is missing. Run setup_windows.ps1 -Deploy after exiting the game.'
}
$runtimeDir = Join-Path $PSScriptRoot '.runtime'
New-Item -ItemType Directory -Force -Path $runtimeDir | Out-Null
$lockFile = Join-Path $runtimeDir 'agent.lock'
try { $agentLock = [IO.File]::Open($lockFile, 'OpenOrCreate', 'ReadWrite', 'None') }
catch [IO.IOException] {
    # Only sharing/lock errors mean an existing instance owns the lock.
    if (($_.Exception.HResult -band 0xffff) -notin @(32, 33)) { throw }
    if (-not $Restart) {
        Write-Host 'An existing AI instance still owns the launcher lock. No second agent was started.'
        Write-Host 'Stop and wait: .\stop_windows.ps1 -Wait'
        Write-Host 'Restart: .\start_windows.ps1 -Restart'
        return
    }
    Write-Host 'Requesting the existing AI to stop before restarting ...'
    & (Join-Path $PSScriptRoot 'stop_windows.ps1') -Wait -TimeoutSeconds 15
    $agentLock = [IO.File]::Open($lockFile, 'OpenOrCreate', 'ReadWrite', 'None')
}
$stopFile = Join-Path $runtimeDir 'stop-agent'
try {
    if (Test-Path -LiteralPath $stopFile) { Remove-Item -LiteralPath $stopFile }
    if (-not (Get-Process -Name Overcooked2 -ErrorAction SilentlyContinue)) {
        Start-Process 'steam://rungameid/728880' -WindowStyle Normal
    }
    # Dry-run only reads snapshots. Real control also needs the NEW exec DLL.
    $ports = if ($DryRun) { @(27411) } else { @(27411, 27412) }
    Write-Host ('Waiting for game plugin on 127.0.0.1 ports ' + ($ports -join ', ') + ' ...')
    $connected = $false
    for ($attempt = 0; $attempt -lt 60; $attempt++) {
        if (Test-Path -LiteralPath $stopFile) { return }
        $connected = $true
        foreach ($port in $ports) {
            $probe = New-Object Net.Sockets.TcpClient
            try {
                $task = $probe.ConnectAsync('127.0.0.1', $port)
                if (-not ($task.Wait(500) -and $probe.Connected)) { $connected = $false }
            } catch { $connected = $false } finally { $probe.Close() }
        }
        if ($connected) { break }
        Start-Sleep -Milliseconds 500
    }
    if (-not $connected) {
        throw "Plugin ports are unavailable. Rebuild/deploy the latest DLL and restart the game. Check $GameDir\BepInEx\LogOutput.log"
    }
    $agentArgs = @('-X', 'utf8', '-u', (Join-Path $PSScriptRoot 'run_agent.py'), '--stop-file', $stopFile)
    if ($DryRun) { $agentArgs += '--dry-run' }
    if ($Seconds -gt 0) { $agentArgs += @('--seconds', $Seconds.ToString([Globalization.CultureInfo]::InvariantCulture)) }
    if ($Voice) { $agentArgs += '--voice' }
    if ($NoVoice) { $agentArgs += '--no-voice' }
    if ($VoiceDebug) { $agentArgs += '--voice-debug' }
    if ($EnvFile) { $agentArgs += @('--env-file', $EnvFile) }
    if ($NoEnvFile) { $agentArgs += '--no-env-file' }
    if ($VoiceLog) { $agentArgs += @('--voice-log', $VoiceLog) }
    Write-Host 'At the title screen, join P1 with your keyboard and reach the main menu.'
    Write-Host 'Keep ONE human player; the plugin joins its virtual gamepad as P2. Do not add a split-keyboard P2.'
    Write-Host 'Run in this console. Ctrl+C stops AI; run this script again to continue. No administrator access is needed.'
    Push-Location $PSScriptRoot
    try {
        & $pythonExe @agentArgs
        $agentExitCode = $LASTEXITCODE
    } finally { Pop-Location }
    if ($agentExitCode -ne 0) { throw "Agent exited with code $agentExitCode" }
} finally { if ($agentLock) { $agentLock.Dispose() } }
