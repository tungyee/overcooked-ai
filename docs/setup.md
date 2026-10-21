# 新机器（macOS）从零搭建

本文说明如何在一台新 Mac 上把本项目跑起来：装 BepInEx/doorstop → 建启动脚本 → 编译部署插件 → 起 Agent。

> 代码仓库只含决策侧（Python）与插件源码（C#），**不含游戏目录里的运行时文件**（BepInEx、doorstop、`run_game.sh`）。这些每台机器要装一次。

## 0. 前置

- Steam 已安装，且已安装 **Overcooked! 2**（macOS 版）。
- 可用的 `python3`（macOS 默认不带；用 Xcode Command Line Tools 或 Homebrew 装一个）。
- 首次编译插件需要 Roslyn 编译器：`plugin/build.sh` 会自动探测/下载，或用 `CSC_DLL` / `DOTNET` 环境变量指定（见 `plugin/build.sh`）。

## 1. 安装 BepInEx + doorstop

1. 到 <https://github.com/BepInEx/BepInEx/releases> 下载 **BepInEx 5.4.x 的 Unity Mono x64（macOS/Unix）** 包。
   - 识别标志：解压后有 `run_bepinex.sh`、`libdoorstop.dylib`、`BepInEx/`、`.doorstop_version`。
   - 本仓库实机验证的版本：BepInEx `5.4.23.5`、doorstop `4.5.0`。
2. 解压到游戏根目录：

   ```bash
   G="$HOME/Library/Application Support/Steam/steamapps/common/Overcooked! 2"
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
cp plugin/build/Oc2AIPlugin.dll "$G/BepInEx/plugins/Oc2AIPlugin.dll"
```

## 4. 启动

```bash
# 一键（停旧 → 编译部署 → 起游戏 → 起 Agent）：
./restart.sh            # 或 ./restart.sh --voice / --no-voice

# 或手动三步：
"$G/run_game.sh"        # ① 起游戏（到标题画面）
# ② 标题画面按 A/空格进 P1、到主菜单（虚拟手柄只在主菜单能 engage 成 P2）
python3 run_agent.py    # ③ 起 Agent（须在游戏主菜单时启动）
```

## 5. 配置

- **非默认 Steam 库**：设 `GAME_DIR=/你的游戏目录`（`restart.sh` / `run_game.sh` / `plugin/build.sh` 都认这个环境变量）。
- **语音（可选）**：仓库根建 `.env`，填 `VOLC_ASR_API_KEY`、`VOLC_TTS_API_KEY`、`ARK_API_KEY`、`ARK_MODEL`（模板见 `.env.example`）。`./restart.sh --voice` 显式开，缺 key 会报错退出。
- **退出**：先 `pkill -f run_agent.py` 再退游戏；或直接退游戏，Agent 会检测到 27411 断连而自动退出。

## 参考

- 插件协议版本：`0.19.0 / protocol 4`（见 `README.md`）。
- 架构与约定：`docs/agent/architecture.md`、`AGENTS.md`。
- 语音排障：`docs/dialog_system.md`。
