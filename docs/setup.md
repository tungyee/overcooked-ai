# 新机器（macOS）从零搭建

[English](setup-en.md)

本文说明如何在一台新 Mac 上把本项目跑起来：先装 BepInEx 和 doorstop，再用 `restart.sh` 编译部署插件并启动游戏和 Agent。

> 代码仓库只含决策侧（Python）与插件源码（C#），**不含游戏目录里的运行时文件**（BepInEx、doorstop）。这些每台机器要装一次。

## 首次配置

先克隆仓库并进入仓库根目录。 [克隆命令](../README_CN.md#1-环境准备)

### 1. 准备游戏和工具

- Steam 已安装，且已安装 **Overcooked! 2**（macOS 版）。
- Python 3.9 或更高版本，终端能运行 `python3`。
- 安装 .NET SDK。启动脚本用 SDK 的 Roslyn 编译插件，可用 `dotnet --list-sdks` 检查安装。

### 2. 安装 BepInEx 和 doorstop

1. 到 <https://github.com/BepInEx/BepInEx/releases> 下载 **BepInEx 5.4.x 的 Unity Mono x64（macOS/Unix）** 包。
   - 识别标志：解压后有 `run_bepinex.sh`、`libdoorstop.dylib`、`BepInEx/`、`.doorstop_version`。
   - 本仓库实机验证的版本：BepInEx `5.4.23.5`、doorstop `4.5.0`。
2. 解压到游戏安装目录。下面是 Steam 默认路径，自定义 Steam 库请将 `GAME_DIR` 换成实际路径。

   ```bash
   export GAME_DIR="$HOME/Library/Application Support/Steam/steamapps/common/Overcooked! 2"
   ls "$GAME_DIR/BepInEx/core/BepInEx.Preloader.dll" "$GAME_DIR/libdoorstop.dylib"
   ```

### 3. 配置 Python 和语音

在仓库根目录创建虚拟环境并安装语音依赖。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-voice.txt
```

在仓库根目录将 `.env.example` 复制为 `.env`，按所用服务填写：

- **火山引擎语音**填写 `VOLC_ASR_API_KEY` 和 `VOLC_TTS_API_KEY`。
- **大模型**任选一家。火山方舟填写 `ARK_API_KEY` 和 `ARK_MODEL`（模型名或推理接入点 ID）。小米 MiMo 取消注释并设置 `LLM_PROVIDER=mimo`，填写 `MIMO_API_KEY` 和 `MIMO_MODEL`。

其他参数沿用模板默认值。模板注释里有服务控制台入口和自定义配置示例。 [配置模板](../.env.example)

在 macOS 的「系统设置 → 隐私与安全性」中，为运行 CoChef 的终端开启麦克风和辅助功能权限。如果按键说话仍不生效，检查「输入监控」权限。默认按住 F8 说话，松开发送。若 F8 是媒体键，请用 Fn+F8。

## 启动游戏和 CoChef

在仓库根目录执行。

```bash
./restart.sh --voice
```

脚本会编译部署插件，设置 doorstop 所需的环境变量并启动游戏，再等待插件端口就绪、启动 Agent。游戏目录里无须另建启动脚本。macOS 上从 Steam 直接启动游戏不会加载插件。

在标题画面按 A 或空格加入人类 P1。进入主菜单后，等插件的虚拟手柄接入为 P2，再进入双人关卡。

## 日常使用

- **非默认 Steam 库**需要在运行启动命令的终端设置 `GAME_DIR`，新开终端时再设置一次，或将它加入 shell 配置文件。
- **无语音模式**：需要单独验证游戏控制时，可用 `./restart.sh --no-voice`。
- **退出与继续**：Agent 在终端前台运行（包括 `restart.sh`），Ctrl+C 停止后再退游戏。只重启 Python 用 `.venv/bin/python run_agent.py`，不必重启游戏；`restart.sh` 会重启游戏。
- **Python 解释器**：`restart.sh` 默认用仓库 `.venv/bin/python`，也可用 `PYTHON=/完整路径/python3` 覆盖。
- **玩家加入**：只加入一个人类 P1，插件在主菜单自动接入虚拟手柄 P2，不再使用分割键盘 2P。

## 参考

- 插件协议版本：`0.24.9 / protocol 10`，需同时开放本机状态端口 27411 与执行端口 27412（见 `README.md`）。
- 架构与约定：`docs/agent/architecture.md`、`AGENTS.md`。
