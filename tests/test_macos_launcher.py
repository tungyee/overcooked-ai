"""用临时游戏目录验证 macOS 启动器传给游戏的环境。"""

import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
BASH = shutil.which("bash")
CC = shutil.which("cc")
pytestmark = pytest.mark.skipif(
    sys.platform != "darwin" or not BASH or not CC, reason="需要 macOS、Bash 和 C 编译器"
)


def write_script(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/bash\n" + body, encoding="utf-8")
    path.chmod(0o755)


def test_restart_launches_game_through_doorstop(tmp_path):
    shutil.copyfile(ROOT / "restart.sh", tmp_path / "restart.sh")
    game = tmp_path / "fake Steam library"
    app = game / "Overcooked2.app"
    preloader = game / "BepInEx/core/BepInEx.Preloader.dll"
    preloader.parent.mkdir(parents=True)
    preloader.touch()
    doorstop = game / "libdoorstop.dylib"
    doorstop_source = tmp_path / "fake_doorstop.c"
    doorstop_source.write_text("void fake_doorstop(void) {}\n", encoding="utf-8")
    subprocess.run([CC, "-dynamiclib", str(doorstop_source), "-o", str(doorstop)], check=True)
    # macOS 会清除系统 shell 的 DYLD_*；模拟游戏时也直接运行二进制。
    source = tmp_path / "fake_game.c"
    source.write_text(r"""
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

int main(void) {
    const char *keys[] = {
        "DOORSTOP_ENABLED", "DOORSTOP_TARGET_ASSEMBLY",
        "DOORSTOP_MONO_DLL_SEARCH_PATH_OVERRIDE", "DYLD_INSERT_LIBRARIES",
        "DYLD_LIBRARY_PATH"
    };
    char cwd[4096];
    FILE *output = fopen("launch-env", "w");
    fprintf(output, "%s\n", getcwd(cwd, sizeof cwd));
    for (size_t i = 0; i < sizeof keys / sizeof keys[0]; i++) {
        const char *value = getenv(keys[i]);
        fprintf(output, "%s\n", value ? value : "");
    }
    fclose(output);
    fclose(fopen("game-started", "w"));
    return 0;
}
""", encoding="utf-8")
    executable = app / "Contents/MacOS/Overcooked2"
    executable.parent.mkdir(parents=True)
    subprocess.run([CC, str(source), "-o", str(executable)], check=True)
    write_script(tmp_path / "plugin/build.sh", """
mkdir -p plugin/build
echo plugin > plugin/build/Oc2AIPlugin.dll
""")
    stubs = tmp_path / "bin"
    write_script(stubs / "pgrep", '[[ -f "$GAME_DIR/game-started" ]] && echo 123')
    write_script(stubs / "lsof", '[[ -f "$GAME_DIR/game-started" ]] && echo 123')
    write_script(stubs / "pkill", "exit 99")
    python = stubs / "python"
    write_script(python, '[[ "$1" == */tools/exec_ping.py || "$*" == "run_agent.py --no-voice" ]]')

    result = subprocess.run(
        [BASH, "restart.sh", "--no-voice"],
        cwd=tmp_path,
        env=dict(os.environ, GAME_DIR=str(game), PYTHON=str(python),
                 PATH=str(stubs) + os.pathsep + os.environ["PATH"]),
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert (game / "BepInEx/plugins/Oc2AIPlugin.dll").is_file()
    assert (game / "launch-env").read_text().splitlines() == [
        str(game.resolve()),
        "1",
        str(preloader),
        str(app / "Contents/Resources/Data/Managed"),
        str(doorstop),
        str(game),
    ]
