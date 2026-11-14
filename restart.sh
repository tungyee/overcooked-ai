#!/bin/bash
# 一键重启 Overcooked! 2 + AI Agent：
#   停旧的（Agent 先、游戏后）→ 重编译并部署插件 dll → 起游戏 → 起 Agent。
#
# 用法：
#   ./restart.sh              # 重编译插件后重启（语音自动：有 key 就开）
#   ./restart.sh --voice      # 显式开语音（缺 key 直接报错退出）
#   ./restart.sh --no-voice   # 强制关语音
#   ./restart.sh --skip-build # 跳过编译（仅重启；只改了 Python 侧时用）
#   ./restart.sh --nav-debug              # 打印 [NAV] 走位诊断（过街/取盘）
#   ./restart.sh --voice-debug            # 语音调试日志开到 DEBUG（有语音时默认开）
#   ./restart.sh --voice-log PATH         # 指定对话日志；默认 voice-<trace 时间戳>.jsonl
# 各 flag 可组合，如 ./restart.sh --skip-build --voice --voice-debug --voice-log voice.jsonl
# 游戏目录默认用 Steam 默认安装路径，可用 GAME_DIR 环境变量覆盖（非默认 Steam 库时用）。
set -u

G="${GAME_DIR:-$HOME/Library/Application Support/Steam/steamapps/common/Overcooked! 2}"
ROOT="$(cd "$(dirname "$0")" && pwd)"
PYTHON="${PYTHON:-$ROOT/.venv/bin/python}"
if [ ! -x "$PYTHON" ]; then
    echo "[restart] 找不到 Python: ${PYTHON}；先 python3 -m venv .venv，或设置 PYTHON 的绝对路径" >&2
    exit 1
fi
PORT=27411
EXEC_PORT=27412
SKIP_BUILD=0
PRE_GAME_PIDS=""        # 启动新游戏前就存在的进程（通常是没退干净的旧实例）
VOICE_FLAG=""          # 空=自动（有 key 就开）；--voice 显式开；--no-voice 显式关
VOICE_DEBUG_FLAG=""
VOICE_LOG_PATH=""
NAV_DEBUG_FLAG=""
while [ $# -gt 0 ]; do
    case "$1" in
        --skip-build) SKIP_BUILD=1 ;;
        --voice)      VOICE_FLAG="--voice" ;;
        --no-voice)   VOICE_FLAG="--no-voice" ;;
        --nav-debug) NAV_DEBUG_FLAG="--nav-debug" ;;
        --voice-debug) VOICE_DEBUG_FLAG="--voice-debug" ;;
        --voice-log)
            if [ $# -lt 2 ]; then
                echo "[restart] --voice-log 需要一个路径参数" >&2
                exit 2
            fi
            VOICE_LOG_PATH="$2"
            shift
            ;;
        *) echo "[restart] 未知参数: $1" >&2; exit 2 ;;
    esac
    shift
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
        "$PYTHON" -c "import socket; s=socket.socket(); s.settimeout(1); s.connect(('127.0.0.1',$port)); s.close()" 2>/dev/null
    fi
}

# 监听者 PID（lsof 看的是内核监听表，不像 nc 那样会被卡死进程骗过；
# 卡死的旧游戏进程会一直占着 27412，连接能进 backlog 但没人处理）。
listener_pid() {
    local port="$1"
    if command -v lsof >/dev/null 2>&1; then
        lsof -nP -iTCP:"$port" -sTCP:LISTEN -t 2>/dev/null | head -1
    fi
}

# 端口是否由本次启动的游戏进程持有（防止把残留监听器当成就绪）。
listener_is_game() {
    local port="$1" pid p
    if ! command -v lsof >/dev/null 2>&1; then
        # 没有 lsof 时只能退化成「端口是否可连」，无法核对属主。
        port_open "$port"
        return $?
    fi
    pid="$(listener_pid "$port")"
    [ -n "$pid" ] || return 1
    # 启动前就存在的游戏进程一律不算：那是没退干净的旧实例残留的监听器。
    case " $PRE_GAME_PIDS " in *" $pid "*) return 1 ;; esac
    for p in $(pgrep -f "$GAME_PATTERN" 2>/dev/null); do
        [ "$p" = "$pid" ] && return 0
    done
    return 1
}

report_port_owners() {
    echo "[restart] 当前监听者：" >&2
    if command -v lsof >/dev/null 2>&1; then
        lsof -nP -iTCP:"$PORT" -iTCP:"$EXEC_PORT" -sTCP:LISTEN >&2 || true
    fi
}

# 退出前检查端口是否真的释放；卡在 FMOD/CoreAudio 退出死锁的旧进程
# 即使 kill -9 也可能一时退不掉，此时继续启动只会又绑不上 27412。
check_ports_free() {
    local port pid wait=0
    while [ "$wait" -lt 10 ]; do
        local busy=""
        for port in "$PORT" "$EXEC_PORT"; do
            [ -n "$(listener_pid "$port")" ] && busy="$busy $port"
        done
        [ -z "$busy" ] && return 0
        sleep 0.5
        wait=$((wait + 1))
    done
    for port in "$PORT" "$EXEC_PORT"; do
        pid="$(listener_pid "$port")"
        if [ -n "$pid" ]; then
            echo "[restart] 端口 $port 仍被 PID $pid 占用，旧游戏进程没有退干净：" >&2
            ps -o pid,stat,etime,command -p "$pid" >&2 || true
        fi
    done
    report_port_owners
    echo "[restart] 先结束占用者（kill -9 <PID>；若杀不掉只能重启机器）再重试" >&2
    exit 1
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

# 4. 起游戏。先确认旧进程真的释放了端口，否则新游戏绑不上 27412。
check_ports_free
PRE_GAME_PIDS="$(pgrep -f "$GAME_PATTERN" 2>/dev/null | tr '\n' ' ' || true)"
echo "[restart] 启动游戏 ..."
"$G/run_game.sh" &

# 5. 等插件两个端口就绪（最多 120s）：27411 快照 + 27412 exec。
#    必须核对监听者属于本次启动的游戏，并让 exec 真回一次 ping——残留的
#    僵死监听器会让「端口可连」误判成就绪（P2 全程不动却没有任何报错）。
echo -n "[restart] 等待插件端口 $PORT/$EXEC_PORT ..."
READY=0
for _ in $(seq 1 120); do
    if listener_is_game "$PORT" && listener_is_game "$EXEC_PORT" \
        && "$PYTHON" "$ROOT/tools/exec_ping.py" --timeout 1 >/dev/null 2>&1; then
        READY=1; echo " 就绪"; break
    fi
    sleep 1
done
if [ "$READY" -ne 1 ]; then
    echo " 超时（120s）" >&2
    report_port_owners
    echo "[restart] 27412 的监听者若不是本次游戏进程，先结束它再重试" >&2
    exit 1
fi

# 6. 前台起 Agent，Ctrl+C 可停；不再把 Agent 留在隐藏的后台。
# trace 与 voice 日志共用同一个时间戳，方便一一对应。
STAMP="$(date +%Y%m%d-%H%M%S)"
export OC2_RUN_STAMP="$STAMP"
if [ "$VOICE_FLAG" != "--no-voice" ]; then
    [ -z "$VOICE_DEBUG_FLAG" ] && VOICE_DEBUG_FLAG="--voice-debug"
    [ -z "$VOICE_LOG_PATH" ] && VOICE_LOG_PATH="runs/voice-$STAMP.jsonl"
fi
echo "[restart] 启动 Agent ..."
echo "[restart] trace: runs/trace-$STAMP.jsonl"
if [ -n "$VOICE_LOG_PATH" ]; then
    echo "[restart] voice: $VOICE_LOG_PATH"
fi
echo "[restart] 提示：虚拟手柄只在主菜单能 engage 成 P2——标题画面按 A/空格进 P1、到主菜单后会自动接入。"
AGENT_ARGS=("$PYTHON" run_agent.py)
[ -n "$VOICE_FLAG" ] && AGENT_ARGS+=("$VOICE_FLAG")
[ -n "$VOICE_DEBUG_FLAG" ] && AGENT_ARGS+=("$VOICE_DEBUG_FLAG")
[ -n "$NAV_DEBUG_FLAG" ] && AGENT_ARGS+=("$NAV_DEBUG_FLAG")
[ -n "$VOICE_LOG_PATH" ] && AGENT_ARGS+=("--voice-log" "$VOICE_LOG_PATH")
exec "${AGENT_ARGS[@]}"
