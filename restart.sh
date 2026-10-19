#!/bin/bash
# 一键重启 Overcooked! 2 + AI Agent：
#   停旧的（Agent 先、游戏后）→ 重编译并部署插件 dll → 起游戏 → 起 Agent。
#
# 用法：
#   ./restart.sh              # 重编译插件后重启
#   ./restart.sh --skip-build # 跳过编译（仅重启；只改了 Python 侧时用）
set -u

G="$HOME/Library/Application Support/Steam/steamapps/common/Overcooked! 2"
ROOT="$(cd "$(dirname "$0")" && pwd)"
PORT=27411
SKIP_BUILD=0
[ "${1:-}" = "--skip-build" ] && SKIP_BUILD=1

# 用进程命令行精确匹配，避免误杀 Steam/脚本自身。
GAME_PATTERN="Overcooked2\.app/Contents/MacOS/Overcooked2"
AGENT_PATTERN="run_agent\.py"

stop_if_running() {
    local pattern="$1" name="$2"
    if pgrep -f "$pattern" >/dev/null 2>&1; then
        echo "[restart] 停止 $name ..."
        pkill -f "$pattern" 2>/dev/null || true
        for _ in $(seq 1 10); do
            pgrep -f "$pattern" >/dev/null 2>&1 || { echo "[restart] $name 已停止"; return 0; }
            sleep 0.5
        done
        echo "[restart] $name 未退出，强制结束"
        pkill -9 -f "$pattern" 2>/dev/null || true
        sleep 1
    fi
}

port_open() {
    if command -v nc >/dev/null 2>&1; then
        nc -z 127.0.0.1 "$PORT" 2>/dev/null
    else
        python3 -c "import socket; s=socket.socket(); s.settimeout(1); s.connect(('127.0.0.1',$PORT)); s.close()" 2>/dev/null
    fi
}

cd "$ROOT"

# 1. 停 Agent（先）——避免残留按住的方向键。
stop_if_running "$AGENT_PATTERN" "Agent"
# 2. 停游戏（后）。
stop_if_running "$GAME_PATTERN" "游戏"

# 3. 编译 + 部署。
if [ "$SKIP_BUILD" -eq 0 ]; then
    echo "[restart] 编译插件 ..."
    "$ROOT/plugin/build.sh" || { echo "[restart] 编译失败，中止" >&2; exit 1; }
    cp "$ROOT/plugin/build/Oc2AIPlugin.dll" "$G/BepInEx/plugins/Oc2AIPlugin.dll"
    echo "[restart] 已部署 dll"
fi

# 4. 起游戏。
echo "[restart] 启动游戏 ..."
"$G/run_game.sh" &

# 5. 等插件 socket 就绪（最多 120s）。
echo -n "[restart] 等待插件端口 $PORT ..."
READY=0
for _ in $(seq 1 120); do
    if port_open; then READY=1; echo " 就绪"; break; fi
    sleep 1
done
if [ "$READY" -ne 1 ]; then
    echo " 超时（120s）" >&2
    exit 1
fi

# 6. 起 Agent。
echo "[restart] 启动 Agent ..."
python3 run_agent.py &

echo "[restart] 完成。下次再跑本脚本会自动先停掉它们。"
