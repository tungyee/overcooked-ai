"""临时目录内测试启动锁，不启动游戏、不注入按键、不结束外部进程。"""
from pathlib import Path
import json
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which('powershell.exe')
pytestmark = pytest.mark.skipif(sys.platform!='win32' or not POWERSHELL, reason='Windows PowerShell')


def prepare(tmp_path):
    for name in ('start_windows.ps1','stop_windows.ps1'):
        shutil.copyfile(ROOT/name,tmp_path/name)
    (tmp_path/'.runtime').mkdir()
    python = tmp_path/'.venv/Scripts/python.exe'
    python.parent.mkdir(parents=True)
    python.write_bytes(b'not executed')
    game = tmp_path/'fake game'
    dll = game/'BepInEx/plugins/Oc2AIPlugin.dll'
    dll.parent.mkdir(parents=True)
    dll.write_bytes(b'not loaded')


def run_ps(tmp_path, code):
    script = tmp_path/'test.ps1'
    script.write_text("$ErrorActionPreference='Stop'\n"+code, encoding='utf-8-sig')
    return subprocess.run([POWERSHELL,'-NoProfile','-ExecutionPolicy','Bypass','-File',str(script)],
        cwd=tmp_path, capture_output=True, text=True, errors='replace',timeout=8.)


def test_duplicate_start_reports_existing_instance_without_starting_second(tmp_path):
    prepare(tmp_path)
    r=run_ps(tmp_path, r'''
$held = [IO.File]::Open((Join-Path $PWD '.runtime/agent.lock'),'OpenOrCreate','ReadWrite','None')
try {
    & .\start_windows.ps1 -GameDir (Join-Path $PWD 'fake game')
    Write-Output 'RETURNED_OK'
} finally { $held.Dispose() }
''')
    assert r.returncode==0, r.stderr
    assert 'No second agent was started' in r.stdout
    assert '-Restart' in r.stdout and 'RETURNED_OK' in r.stdout
    assert 'Waiting for game plugin' not in r.stdout


def test_restart_requests_wait_before_any_new_launch(tmp_path):
    prepare(tmp_path)
    (tmp_path/'stop_windows.ps1').write_text('''
param([switch]$Wait,[int]$TimeoutSeconds)
if (-not $Wait) { throw 'MISSING_WAIT' }
throw 'EXPECTED_STOP_REQUEST'
''',encoding='utf-8-sig')
    r=run_ps(tmp_path, r'''
$held = [IO.File]::Open((Join-Path $PWD '.runtime/agent.lock'),'OpenOrCreate','ReadWrite','None')
try { & .\start_windows.ps1 -Restart -GameDir (Join-Path $PWD 'fake game') }
finally { $held.Dispose() }
''')
    assert r.returncode!=0
    assert 'EXPECTED_STOP_REQUEST' in r.stderr and 'MISSING_WAIT' not in r.stderr
    assert 'Waiting for game plugin' not in r.stdout


def test_stale_unlocked_lock_file_does_not_block_stop_wait(tmp_path):
    prepare(tmp_path)
    (tmp_path/'.runtime/agent.lock').touch()
    r=run_ps(tmp_path, r'& .\stop_windows.ps1 -Wait -TimeoutSeconds 1')
    assert r.returncode==0, r.stderr
    assert 'lock released' in r.stdout
    assert (tmp_path/'.runtime/stop-agent').exists()
    assert (tmp_path/'.runtime/agent.lock').exists()


def test_wait_timeout_does_not_bypass_or_delete_held_lock(tmp_path):
    prepare(tmp_path)
    r=run_ps(tmp_path, r'''
$held = [IO.File]::Open((Join-Path $PWD '.runtime/agent.lock'),'OpenOrCreate','ReadWrite','None')
try { & .\stop_windows.ps1 -Wait -TimeoutSeconds 1 }
finally { $held.Dispose() }
''')
    assert r.returncode!=0
    assert 'No process was force-stopped' in r.stderr
    assert (tmp_path/'.runtime/agent.lock').exists()


def test_lock_path_error_is_not_misreported_as_running_agent(tmp_path):
    prepare(tmp_path)
    (tmp_path/'.runtime/agent.lock').mkdir()
    r=run_ps(tmp_path, r"& .\start_windows.ps1 -GameDir (Join-Path $PWD 'fake game')")
    assert r.returncode!=0
    assert 'No second agent was started' not in r.stdout
    assert 'already running' not in r.stderr


def test_visible_voice_console_can_start_again_without_background(tmp_path):
    shutil.copyfile(ROOT/'voice_console.ps1',tmp_path/'voice_console.ps1')
    (tmp_path/'start_windows.ps1').write_text('''
param([switch]$Voice,[switch]$VoiceDebug,[string]$VoiceLog,[switch]$Background)
if ($Background -or -not $Voice -or -not $VoiceDebug -or $VoiceLog -ne 'voice.jsonl') {
    throw 'WRONG_CONSOLE_ARGS'
}
Write-Output 'VISIBLE_VOICE_START'
''',encoding='utf-8-sig')
    r=run_ps(tmp_path,r'''
& .\voice_console.ps1
oc2
''')
    assert r.returncode==0,r.stderr
    assert r.stdout.count('VISIBLE_VOICE_START')==2


@pytest.mark.parametrize('dry_run', [False, True])
def test_launcher_runs_foreground_with_exec_ports_and_current_cli(tmp_path, dry_run):
    prepare(tmp_path)
    # 运行真正的 Python 包装器及参数解析，只替换测试目录中的 run_agent。
    # venv 配置仍指向原始 Python 安装，不复制库或连接游戏。
    source = Path(sys.executable)
    shutil.copyfile(source, tmp_path/'.venv/Scripts/python.exe')
    shutil.copyfile(source.parent.parent/'pyvenv.cfg', tmp_path/'.venv/pyvenv.cfg')
    (tmp_path/'run_agent.py').write_text('''
import argparse, json, sys
print('TEST_STARTUP_LOG', file=sys.stderr)
p=argparse.ArgumentParser()
p.add_argument('--stop-file')
p.add_argument('--seconds', type=float)
p.add_argument('--dry-run', action='store_true')
p.add_argument('--voice', action='store_true')
p.add_argument('--voice-debug', action='store_true')
p.add_argument('--env-file')
p.add_argument('--voice-log')
a=p.parse_args()
print('CLI_JSON='+json.dumps(vars(a)))
''', encoding='utf-8')
    code = r'''
$global:OC2TestPorts = [Collections.Generic.List[int]]::new()
function global:Get-Process { param($Name, $ErrorAction); return @{ Id = 1 } }
function global:Start-Process { throw 'UNEXPECTED_BACKGROUND_OR_UAC' }
function global:New-Object {
    param($TypeName)
    if ($TypeName -ne 'Net.Sockets.TcpClient') { throw 'UNEXPECTED_OBJECT' }
    $probe = [pscustomobject]@{ Connected = $true }
    $probe | Add-Member ScriptMethod ConnectAsync {
        param($ServerName, $PortNumber)
        $global:OC2TestPorts.Add($PortNumber)
        return [Threading.Tasks.Task]::Delay(0)
    }
    $probe | Add-Member ScriptMethod Close { }
    return $probe
}
& .\start_windows.ps1 -GameDir (Join-Path $PWD 'fake game') -Seconds 1.5 -Voice -VoiceDebug -EnvFile 'config with spaces.env' -VoiceLog 'voice with spaces.jsonl' DRY_FLAG
Write-Output ('PORTS=' + ($global:OC2TestPorts -join ','))
'''.replace('DRY_FLAG', '-DryRun' if dry_run else '')
    r = run_ps(tmp_path, code)
    assert r.returncode == 0, r.stderr
    args = json.loads(next(line[9:] for line in r.stdout.splitlines() if line.startswith('CLI_JSON=')))
    assert args['seconds'] == 1.5
    assert args['voice'] and args['voice_debug']
    assert args['dry_run'] is dry_run
    assert args['env_file'] == 'config with spaces.env'
    assert args['voice_log'] == 'voice with spaces.jsonl'
    assert args['stop_file'] == str(tmp_path/'.runtime/stop-agent')
    assert ('PORTS=27411\n' if dry_run else 'PORTS=27411,27412\n') in r.stdout
    # 正常退出时 finally 已释放启动锁。
    assert run_ps(tmp_path, r'& .\stop_windows.ps1 -Wait -TimeoutSeconds 1').returncode == 0
