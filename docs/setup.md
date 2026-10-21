# 新机器（macOS）从零搭建

本文说明如何在一台新 Mac 上把本项目跑起来：装 BepInEx/doorstop → 建启动脚本 → 编译部署插件 → 起 Agent。

> 代码仓库只含决策侧（Python）与插件源码（C#），**不含游戏目录里的运行时文件**（BepInEx、doorstop、`run_game.sh`）。这些每台机器要装一次。

## 0. 前置

- Steam 已安装，且已安装 **Overcooked! 2**（macOS 版）。
- 可用的 `python3`（macOS 默认不带；用 Xcode Command Line Tools 或 Homebrew 装一个）。
- 安装 .NET SDK（不是仅有运行时）：`plugin/build.sh` 从 `dotnet --list-sdks` 探测 Roslyn，或用 `CSC_DLL` / `DOTNET` 环境变量指定；脚本不自动下载 SDK。
- 在仓库根创建 Python 环境：`python3 -m venv .venv`。可选语音依赖见 [README](../README.md#可选语音与大模型)。

## 1. 安装 BepInEx + doorstop

1. 到 <https://github.com/BepInEx/BepInEx/releases> 下载 **BepInEx 5.4.x 的 Unity Mono x64（macOS/Unix）** 包。
   - 识别标志：解压后有 `run_bepinex.sh`、`libdoorstop.dylib`、`BepInEx/`、`.doorstop_version`。
   - 本仓库实机验证的版本：BepInEx `5.4.23.5`、doorstop `4.5.0`。
2. 解压到游戏根目录：

   ```bash
   export GAME_DIR="$HOME/Library/Application Support/Steam/steamapps/common/Overcooked! 2"
   G="$GAME_DIR"
   # 解压后应能看到：
   #   "$G/BepInEx/"、"$G/libdoorstop.dylib"、"$G/run_bepinex.sh"
   ```

## 2. 建 run_game.sh（启动脚本）

游戏必须经 doorstop 启动才会加载插件（Steam 直接启动不会）。把下面内容保存成 `"$G/run_game.sh"` 并 `chmod +x`：

```bash
#!/bin/bash
# 用 BepInEx(doorstop) 启动 Overcooked! 2；Steam 直接启动不会加载插件。
G="${GAME_DIR:-$HOME/Library/Application Support/Steam/steamapps/common/Overcooked! 2}"
APP="$G/Overcooked2.app"
cd "$G" || exit 1
DOORSTOP_ENABLED=1 \
DOORSTOP_TARGET_ASSEMBLY="$G/BepInEx/core/BepInEx.Preloader.dll" \
DOORSTOP_MONO_DLL_SEARCH_PATH_OVERRIDE="$APP/Contents/Resources/Data/Managed" \
DYLD_INSERT_LIBRARIES="$G/libdoorstop.dylib" \
DYLD_LIBRARY_PATH="$G" \
"$APP/Contents/MacOS/Overcooked2"
```

## 3. 编译并部署插件

在仓库根执行：

```bash
./plugin/build.sh
mkdir -p "$G/BepInEx/plugins"
cp plugin/build/Oc2AIPlugin.dll "$G/BepInEx/plugins/Oc2AIPlugin.dll"
```

## 4. 启动

```bash
# 一键（停旧 → 编译部署 → 起游戏 → 起 Agent）：
./restart.sh            # 或 ./restart.sh --voice / --no-voice

# 或手动三步：
"$G/run_game.sh"        # ① 起游戏（到标题画面）
# ② 标题画面按 A/空格进 P1、到主菜单（虚拟手柄只在主菜单能 engage 成 P2）
.venv/bin/python run_agent.py --no-voice # ③ 起 Agent；确认插件虚拟手柄已在主菜单加入 P2 后再进关
```

## 5. 配置

- **非默认 Steam 库**：设 `GAME_DIR=/你的游戏目录`（`restart.sh` / `run_game.sh` / `plugin/build.sh` 都认这个环境变量）。
- **语音（可选）**：仓库根建 `.env`，填 `VOLC_ASR_API_KEY`、`VOLC_TTS_API_KEY`、`ARK_API_KEY`、`ARK_MODEL`（模板见 `.env.example`）。`./restart.sh --voice` 显式开，缺 key 会报错退出。
- **退出与继续**：Agent 在终端前台运行（包括 `restart.sh`），Ctrl+C 停止后再退游戏。只重启 Python 用 `.venv/bin/python run_agent.py`，不必重启游戏；`restart.sh` 会重启游戏。
- **Python 解释器**：`restart.sh` 默认用仓库 `.venv/bin/python`，也可用 `PYTHON=/完整路径/python3` 覆盖。
- **玩家加入**：只加入一个人类 P1，插件在主菜单自动接入虚拟手柄 P2，不再使用分割键盘 2P。

## 参考

- 插件协议版本：`0.20.0 / protocol 4`，需同时开放本机状态端口 27411 与执行端口 27412（见 `README.md`）。
- 架构与约定：`docs/agent/architecture.md`、`AGENTS.md`。
- 语音排障：`docs/dialog_system.md`。
