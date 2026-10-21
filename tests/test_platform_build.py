"""在假 SDK/游戏目录检查构建脚本，绝不部署到游戏。"""
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
BASH = shutil.which('bash') or shutil.which('bash.exe')
if not BASH and os.name == 'nt':
    git = shutil.which('git')
    candidate = Path(git).parent.parent/'bin/bash.exe' if git else None
    if candidate and candidate.exists():
        BASH = str(candidate)


@pytest.mark.skipif(not BASH, reason='Bash unavailable')
def test_mac_build_discovers_sdk_outside_home_dotnet_with_spaces(tmp_path):
    plugin = tmp_path/'project/plugin'
    plugin.mkdir(parents=True)
    shutil.copyfile(ROOT/'plugin/build.sh', plugin/'build.sh')
    (plugin/'Fake.cs').touch()
    sdk_root = tmp_path/'SDK location with spaces/sdk'
    compiler = sdk_root/'8.0.100/Roslyn/bincore/csc.dll'
    compiler.parent.mkdir(parents=True)
    compiler.touch()
    dotnet = tmp_path/'fake dotnet'
    dotnet.write_text('''#!/bin/bash
if [ "$1" = --list-sdks ]; then
    printf '8.0.100 [%s]\\n' "$FAKE_SDK_ROOT"
else
    printf '%s\\n' "$@" > "$FAKE_ARGS"
fi
''', encoding='utf-8')
    dotnet.chmod(0o755)
    args_file = tmp_path/'compiler-args.txt'
    game = tmp_path/'Steam library/Overcooked! 2'
    env = dict(os.environ, DOTNET=dotnet.as_posix(), GAME_DIR=game.as_posix(),
               FAKE_SDK_ROOT=sdk_root.as_posix(), FAKE_ARGS=args_file.as_posix())
    env.pop('CSC_DLL', None)
    r = subprocess.run([BASH, str(plugin/'build.sh')], env=env,
                       capture_output=True, text=True, errors='replace', timeout=10.)
    assert r.returncode == 0, r.stderr
    args = args_file.read_text(encoding='utf-8').splitlines()
    assert args[0] == compiler.as_posix()
    managed = game/'Overcooked2.app/Contents/Resources/Data/Managed'
    for name in ('Assembly-CSharp.dll', 'Assembly-CSharp-firstpass.dll',
                 'System.Core.dll', 'UnityEngine.PhysicsModule.dll'):
        assert '-r:'+(managed/name).as_posix() in args
