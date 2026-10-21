# Open with: powershell.exe -NoExit -File .\voice_console.ps1
$global:OC2VoiceRepoRoot = $PSScriptRoot
Set-Location -LiteralPath $PSScriptRoot
function global:oc2 {
    & (Join-Path $global:OC2VoiceRepoRoot 'start_windows.ps1') -Voice -VoiceDebug -VoiceLog voice.jsonl
}
Write-Host 'Overcooked AI voice console (Caps Lock: hold to speak, release to send).'
Write-Host 'Ctrl+C: stop AI and halt its virtual gamepad. Type oc2 and press Enter to start again.'
Write-Host 'After stopping, type exit to close this console.'
oc2
