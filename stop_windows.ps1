$runtimeDir = Join-Path $PSScriptRoot '.runtime'
New-Item -ItemType Directory -Force -Path $runtimeDir | Out-Null
Set-Content -LiteralPath (Join-Path $runtimeDir 'stop-agent') -Value 'stop'
Write-Host 'Stop requested. The agent will release its keys and exit on its next tick.'
