param(
    [switch]$Wait,
    [ValidateRange(1, 60)][int]$TimeoutSeconds = 15
)
$ErrorActionPreference = 'Stop'
$runtimeDir = Join-Path $PSScriptRoot '.runtime'
New-Item -ItemType Directory -Force -Path $runtimeDir | Out-Null
Set-Content -LiteralPath (Join-Path $runtimeDir 'stop-agent') -Value 'stop'
Write-Host 'Stop requested. The agent will halt its virtual gamepad and exit on its next tick.'
if ($Wait) {
    $deadlineUtc = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    $lockFile = Join-Path $runtimeDir 'agent.lock'
    do {
        try {
            $stopLock = [IO.File]::Open($lockFile, 'OpenOrCreate', 'ReadWrite', 'None')
            $stopLock.Dispose()
            Write-Host 'AI stopped; launcher lock released.'
            return
        } catch [IO.IOException] {
            if (($_.Exception.HResult -band 0xffff) -notin @(32, 33)) { throw }
        }
        if ([DateTime]::UtcNow -ge $deadlineUtc) {
            throw "AI did not exit within $TimeoutSeconds seconds; its launcher lock is still held. No process was force-stopped."
        }
        Start-Sleep -Milliseconds 150
    } while ($true)
}
