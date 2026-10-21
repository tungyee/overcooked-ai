# Overcooked! 2 AI 陪玩（oc2）

让 AI 作为 2P 与你合作玩 [Overcooked! 2](https://store.steampowered.com/app/728880/Overcooked_2/)。支持 Windows 和 macOS，行为树在本机运行，做菜和移动不需要模型 API；语音对话可选接入 ASR → LLM → TTS。

## 演示视频

[Level 1–6 关卡演示（约 4 分半）](media/Level-1-6-demo.mp4)

## 工作原理

```text
BepInEx 插件 ──27411 / NDJSON / 5 Hz──→ Python 行为树（20 Hz）
     ↑                                     │
     └────27412 / exec 命令与结果────────────┘
     ↓
游戏内逐帧执行器 + InControl 虚拟手柄 → AI 厨师 P2
```

当前插件为 **0.20.0 / 状态协议 4**。两平台均使用插件执行通道，旧版键盘注入已删除。更新旧安装时必须重新编译、部署 DLL 并重启游戏；只替换 Python 无法给旧 DLL 增加 27412 端口。

## 获取代码

```bash
git clone https://github.com/tungyee/overcooked-ai.git
cd overcooked-ai
```

已有工作副本可运行 `git remote set-url origin https://github.com/tungyee/overcooked-ai.git` 更新地址，再拉取 `main`。若旧副本有未提交修改或历史已分叉，先保留旧目录并重新克隆，避免覆盖本机配置。

## Windows 使用方法

需要 Steam 版游戏、Python 3.9+、Windows PowerShell 5.1 或 PowerShell 7。先将 **BepInEx 5 的 Windows x86 版本**安装到游戏目录，与 `Overcooked2.exe` 同级；运行游戏一次生成配置后退出。BepInEx 下载见[官方发布页](https://github.com/BepInEx/BepInEx/releases)。

### 1. 创建环境并部署插件

在仓库根目录的 PowerShell 中执行；**部署时游戏必须已退出**：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\setup_windows.ps1 -GameDir 'D:\steam\steamapps\common\Overcooked! 2' -Deploy
```

脚本创建 `.venv`，编译并备份／替换 `BepInEx\plugins\Oc2AIPlugin.dll`。纯行为树无第三方 Python 运行依赖；首次编译按需从 NuGet 下载 Roslyn 到 `.tools`，不要求安装 .NET SDK。已有离线编译器可用 `plugin\build.ps1 -Compiler '完整路径\csc.exe' -GameDir '游戏目录' -Deploy`。

游戏装在其他目录时，后续启动也要传 `-GameDir`，或在当前 PowerShell 设置 `$env:GAME_DIR = '你的游戏目录'`。可用 `-Python 'python.exe完整路径'` 指定创建虚拟环境的解释器。开发测试依赖通过 `setup_windows.ps1 -Dev` 安装；语音依赖用 `-Voice`，均可与 `-Deploy` 组合。

### 2. 前台启动并加入游戏

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\start_windows.ps1 -GameDir 'D:\steam\steamapps\common\Overcooked! 2' -NoVoice
```

也可双击 `start_windows.cmd`。启动器按需通过 Steam 启动游戏，等待 **27411 和 27412** 两个端口后在当前控制台运行 AI。

1. 标题画面用键盘加入人类 P1，进入主菜单。
2. **只保留一个人类玩家，不再加入分割键盘 2P**；插件自动把独立虚拟手柄接入为 AI 玩家。
3. 确认菜单中有两名玩家后进入本地合作关卡，建议先试故事 1-3 或 1-4。人类按游戏设置操作，AI 无需方向键、右 Ctrl 等键盘映射，也不需要管理员权限。

首次加载新版插件时若已在关卡内，先回主菜单让虚拟手柄加入，再进入关卡。日志中的 `[EXEC] engaged` 表示虚拟手柄已接入；`not engaged` 表示尚未接入。

### 3. 停止、继续与诊断

**Ctrl+C 停止 AI**，再运行启动命令即可继续。停止 AI 不会退出游戏；只重启 Python 且虚拟手柄仍在时，无须重开关卡。其他窗口可请求安全停止：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\stop_windows.ps1 -Wait
# 停掉本启动器管理的旧实例后，在当前窗口重新启动：
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\start_windows.ps1 -Restart -NoVoice
# 150 秒限时运行：
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\start_windows.ps1 -NoVoice -Seconds 150
```

普通重复启动会提示已有实例并返回。不要删除占用中的 `agent.lock`；`-Restart` 会先请求退出并等锁释放，不会强杀进程。直接运行 `run_agent.py` 的实例不受此启动器锁管理，应在它自己的控制台停止。

```powershell
# 只读检查状态，不向 exec 发送命令：
.\.venv\Scripts\python.exe -X utf8 tools\check_windows.py --seconds 5
# 只看行为树决策（NullExec），不连接控制端口：
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\start_windows.ps1 -DryRun -NoVoice -Seconds 15
```

## macOS 使用方法

需要 Steam 版游戏、Python 3.9+、BepInEx 5 macOS/Unix Unity Mono x64 和 .NET SDK（用于 Roslyn 编译）。BepInEx、doorstop 和游戏目录内的 `run_game.sh` 首次安装方法见 [macOS 从零搭建](docs/setup.md)。仓库不包含这些第三方运行时文件。

### 1. 创建环境、编译并部署

在仓库根目录执行；部署前退出游戏：

```bash
python3 -m venv .venv
source .venv/bin/activate
export GAME_DIR="$HOME/Library/Application Support/Steam/steamapps/common/Overcooked! 2"
./plugin/build.sh
mkdir -p "$GAME_DIR/BepInEx/plugins"
cp plugin/build/Oc2AIPlugin.dll "$GAME_DIR/BepInEx/plugins/Oc2AIPlugin.dll"
```

`GAME_DIR` 必须指向实际 Steam 游戏目录。脚本从 `dotnet --list-sdks` 查找 Roslyn，支持 PATH 中的 dotnet、Homebrew 或 `~/.dotnet`；也可用 `DOTNET` 指定 dotnet 路径、`CSC_DLL` 指定 `csc.dll`。构建脚本不自动下载 SDK。

### 2. 启动游戏与 AI

```bash
# 终端 A：经 doorstop 启动，普通 Steam 启动不保证加载 BepInEx
"$GAME_DIR/run_game.sh"
```

在游戏标题画面加入一个人类 P1，到主菜单等待插件虚拟手柄作为 P2 加入，确认两名玩家后再进关卡。在终端 B 的仓库根目录运行：

```bash
.venv/bin/python run_agent.py --no-voice
# 只读决策：
.venv/bin/python run_agent.py --dry-run --no-voice
```

也可在虚拟环境建好后使用一键重启：

```bash
./restart.sh --no-voice              # 停旧实例和游戏 → 编译部署 → 起游戏 → 前台运行 AI
./restart.sh --skip-build --no-voice # 只改 Python 时跳过编译，仍会重启游戏
```

`restart.sh` 默认使用仓库 `.venv/bin/python`，可通过 `PYTHON=/解释器完整路径` 覆盖。脚本会结束旧 Agent 和游戏，请先完成当前关卡。新 Agent 保持在终端前台，**Ctrl+C 停止**；随后重新执行 `.venv/bin/python run_agent.py ...` 即可启动，不必每次重启游戏。

游戏控制不需要 macOS「辅助功能」权限；**可选语音 PTT 的 Quartz 热键监听**需要 pyobjc 及「辅助功能」，部分系统还需要「输入监控」。

## 可选语音与大模型

行为树负责实时做菜；大模型解释玩家对话、分配合作职责并生成回复。需要分别开通兼容的 ASR、TTS 和方舟 LLM 服务，可能产生服务费用。

Windows：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-voice.txt
# 仅首次创建，已有 .env 时不要覆盖：
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
notepad .env
```

macOS：

```bash
.venv/bin/python -m pip install -r requirements-voice.txt
# 使用 PTT 热键时再安装；纯 VAD 不需要 Quartz：
.venv/bin/python -m pip install pyobjc-framework-Quartz
[ -f .env ] || cp .env.example .env
```

在本机 `.env` 填写 `VOLC_ASR_API_KEY`、`VOLC_TTS_API_KEY`、`ARK_API_KEY`、`ARK_MODEL`；按账户使用的产品设置 `ARK_BASE_URL`。模型名或推理接入点必须在该端点可用。音色须与 TTS 资源版本匹配，具体变量见 [.env.example](.env.example)。**不要把真实密钥写入 README、示例文件或提交记录**；`.env` 已被 Git 忽略。

`run_agent.py` 自动读取仓库根 `.env`，已有环境变量优先，无需手动导出。Windows 支持 PowerShell 保存的 UTF-8 BOM 文件。Windows 建议配置 `OC2_VOICE_PTT_KEY=caps_lock`，按住录音、松开发送；避免 Alt/Ctrl/Shift 快捷键冲突及笔记本 F8 媒体功能。macOS 可用 `f8`（必要时 Fn+F8）；留空为 VAD 自动切句。

```powershell
# Windows：可见语音控制台，Ctrl+C 停止，输入 oc2 回车再次启动
powershell.exe -NoProfile -NoExit -ExecutionPolicy Bypass -File .\voice_console.ps1
```

```bash
# macOS：当前终端前台运行
.venv/bin/python run_agent.py --voice --voice-debug --voice-log voice.jsonl
```

Windows 控制台使用默认游戏路径或 `GAME_DIR` 环境变量。也可直接 `start_windows.ps1 -Voice -VoiceDebug -VoiceLog voice.jsonl`。`--voice` 缺配置会明确报错；不带开关时 key 齐才自动开语音；`--no-voice` / Windows `-NoVoice` 强制关闭。

可用虚拟环境解释器运行 `tools/voice_smoke.py doctor` 检查配置、依赖和音频设备，不调用模型。`all` 则会调用真实语音和模型服务。详细说明见 [语音配置与排障](docs/dialog_system.md)。

## 常见问题

| 现象 | 检查方法 |
| --- | --- |
| 27411 能连，27412 超时 | 仍加载旧 DLL；退出游戏，重新部署最新插件并启动，查看 `BepInEx/LogOutput.log`。 |
| `not engaged`，AI 不动 | 回主菜单，只保留人类 P1，等待虚拟手柄加入为 P2 后重新进关卡。不要继续使用旧分割键盘 2P 配置。 |
| 重复启动提示锁被占用 | 在原控制台 Ctrl+C，或 `stop_windows.ps1 -Wait`；需要重启时用 `-Restart`。 |
| 能识别但听不到回复 | 检查默认输出设备、TTS 音色/资源与服务权限，并查看 `voice.jsonl`；PTT 须松开才发送。 |
| 修改 Python 后未生效 | 停止并重新启动 Agent。修改 C# 后还必须重新编译部署、重启游戏。 |

地图、配方和合作策略仍在改进；已有关卡回归不代表所有关卡都能自动通关。Windows 测试不能代替 Mac 实测。

## 开发与测试

```bash
# 使用各平台的虚拟环境 Python：Windows 为 .venv\Scripts\python.exe
python -m pip install -r requirements-dev.txt
python -m pytest tests/
```

| 路径 | 用途 |
| --- | --- |
| `run_agent.py` | Python 启动入口 |
| `ai/` | 状态、行为树、规划、exec 客户端、语音对话 |
| `plugin/` | BepInEx 数据源、虚拟手柄、逐帧执行器和构建脚本 |
| `tests/` | 单元测试、帧回放和平台回归 |
| `tools/` | 只读诊断、录帧和调试工具 |
| `docs/` | 架构、配置、关卡和设计文档 |

开发约定见 [AGENTS.md](AGENTS.md)，架构见 [docs/agent/architecture.md](docs/agent/architecture.md)。本项目采用 [MIT](LICENSE) 许可证。
