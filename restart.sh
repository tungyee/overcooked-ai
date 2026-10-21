#!/bin/bash
# 一键重启 Overcooked! 2 + AI Agent：
#   停旧的（Agent 先、游戏后）→ 重编译并部署插件 dll → 起游戏 → 起 Agent。
#
# 用法：
#   ./restart.sh              # 重编译插件后重启（语音自动：有 key 就开）
#   ./restart.sh --voice      # 显式开语音（缺 key 直接报错退出）
#   ./restart.sh --no-voice   # 强制关语音
#   ./restart.sh --skip-build # 跳过编译（仅重启；只改了 Python 侧时用）
# 各 flag 可组合，如 ./restart.sh --skip-build --voice
# 游戏目录默认用 Steam 默认安装路径，可用 GAME_DIR 环境变量覆盖（非默认 Steam 库时用）。
set -u

G="${GAME_DIR:-$HOME/Library/Application Support/Steam/steamapps/common/Overcooked! 2}"
ROOT="$(cd "$(dirname "$0")" && pwd)"
PORT=27411
EXEC_PORT=27412
SKIP_BUILD=0
VOICE_FLAG=""          # 空=自动（有 key 就开）；--voice 显式开；--no-voice 显式关
for arg in "$@"; do
    case "$arg" in
        --skip-build) SKIP_BUILD=1 ;;
        --voice)      VOICE_FLAG="--voice" ;;
        --no-voice)   VOICE_FLAG="--no-voice" ;;
        *) echo "[restart] 未知参数: $arg" >&2; exit 2 ;;
    esac
done

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
    local port="${1:-$PORT}"
    if command -v nc >/dev/null 2>&1; then
        nc -z 127.0.0.1 "$port" 2>/dev/null
    else
        python3 -c "import socket; s=socket.socket(); s.settimeout(1); s.connect(('127.0.0.1',$port)); s.close()" 2>/dev/null
    fi
}

cd "$ROOT"

# 1. 停 Agent（先）——避免残留命令/输入状态。
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

# 5. 等插件两个端口就绪（最多 120s）：27411 快照 + 27412 exec。
echo -n "[restart] 等待插件端口 $PORT/$EXEC_PORT ..."
READY=0
for _ in $(seq 1 120); do
    if port_open "$PORT" && port_open "$EXEC_PORT"; then READY=1; echo " 就绪"; break; fi
    sleep 1
done
if [ "$READY" -ne 1 ]; then
    echo " 超时（120s）" >&2
    exit 1
fi

# 6. 起 Agent。
echo "[restart] 启动 Agent ..."
python3 run_agent.py $VOICE_FLAG &

echo "[restart] 完成。下次再跑本脚本会自动先停掉它们。"
echo "[restart] 提示：虚拟手柄只在主菜单能 engage 成 P2——标题画面按 A/空格进 P1、到主菜单后会自动接入。"
