# Windows 搭建与用法

[English](setup-windows-en.md)

在 Windows 上从零跑起来：装 BepInEx → 编译部署插件 → 启动 → 停止与诊断。

> 仓库只含决策侧（Python）与插件源码（C#），不含游戏目录里的运行时文件（BepInEx 等），每台机器装一次。

## 首次配置

先克隆仓库并进入仓库根目录。 [克隆命令](../README_CN.md#1-环境准备)

### 1. 准备游戏和工具

- Steam 版《Overcooked! 2》。
- Python 3.9+，Windows PowerShell 5.1 或 PowerShell 7。
- 将 **BepInEx 5 的 Windows x86 版**安装到游戏目录，与 `Overcooked2.exe` 同级；运行游戏一次生成配置后退出。下载见 [BepInEx 官方发布页](https://github.com/BepInEx/BepInEx/releases)。

### 2. 创建环境并部署插件

退出游戏，把示例路径换成实际安装目录，然后在仓库根目录的 PowerShell 运行。

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\setup_windows.ps1 -GameDir 'D:\steam\steamapps\common\Overcooked! 2' -Deploy -Voice
```

脚本创建 `.venv`、安装语音依赖，随后编译并备份／替换 `BepInEx\plugins\Oc2AIPlugin.dll`。纯行为树无第三方 Python 运行依赖；首次编译按需从 NuGet 下载 Roslyn 到 `.tools`，不要求安装 .NET SDK。已有离线编译器时：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\plugin\build.ps1 -Compiler '完整路径\csc.exe' -GameDir '游戏目录' -Deploy
```

游戏装在其他目录时，后续启动也要传 `-GameDir`，或在当前 PowerShell 设置 `$env:GAME_DIR = '你的游戏目录'`。可用 `-Python 'python.exe完整路径'` 指定创建虚拟环境的解释器。开发测试依赖用 `-Dev` 安装；语音依赖用 `-Voice`，均可与 `-Deploy` 组合。

### 3. 配置语音

在仓库根目录将 `.env.example` 复制为 `.env`，按所用服务填写：

- **火山引擎语音**填写 `VOLC_ASR_API_KEY` 和 `VOLC_TTS_API_KEY`。
- **大模型**任选一家。火山方舟填写 `ARK_API_KEY` 和 `ARK_MODEL`（模型名或推理接入点 ID）。小米 MiMo 取消注释并设置 `LLM_PROVIDER=mimo`，填写 `MIMO_API_KEY` 和 `MIMO_MODEL`。

其他参数沿用模板默认值。模板注释里有服务控制台入口和自定义配置示例。 [配置模板](../.env.example)

在 Windows 的麦克风隐私设置中，允许桌面应用访问麦克风。默认按住 F8 说话，松开发送。如果 F8 与系统快捷键冲突，可在 `.env` 中设置 `OC2_VOICE_PTT_KEY=caps_lock`。

## 前台启动并加入游戏

启动时使用和部署时相同的游戏路径。

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\start_windows.ps1 -GameDir 'D:\steam\steamapps\common\Overcooked! 2' -Voice
```

启动器按需通过 Steam 启动游戏，等待 **27411 和 27412** 两个端口后在当前控制台运行 AI。

1. 标题画面用键盘加入人类 P1，进入主菜单。
2. **只保留一个人类玩家，不再加入分割键盘 2P**；插件自动把独立虚拟手柄接入为 AI 玩家。
3. 确认菜单中有两名玩家后，选择想玩的本地双人关卡。AI 无需方向键、右 Ctrl 等键盘映射，也不需要管理员权限。

首次加载新版插件时若已在关卡内，先回主菜单让虚拟手柄加入，再进关卡。日志中的 `[EXEC] engaged` 表示虚拟手柄已接入；`not engaged` 表示尚未接入。

## 停止、继续与诊断

**Ctrl+C 停止 AI**，再运行启动命令即可继续。停止 AI 不会退出游戏；只重启 Python 且虚拟手柄仍在时，无须重开关卡。其他窗口可请求安全停止：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\stop_windows.ps1 -Wait
# 停掉本启动器管理的旧实例后，在当前窗口重新启动：
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\start_windows.ps1 -Restart -Voice
# 150 秒限时运行：
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\start_windows.ps1 -Voice -Seconds 150
```

普通重复启动会提示已有实例并返回。不要删除占用中的 `agent.lock`；`-Restart` 会先请求退出并等锁释放，不会强杀进程。直接运行 `run_agent.py` 的实例不受此启动器锁管理，应在它自己的控制台停止。

```powershell
# 只读检查状态，不向 exec 发送命令：
.\.venv\Scripts\python.exe -X utf8 tools\check_windows.py --seconds 5
# 只看行为树决策（NullExec），不连接控制端口：
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\start_windows.ps1 -DryRun -NoVoice -Seconds 15
```

## 语音控制台

```powershell
# 可见语音控制台，Ctrl+C 停止，输入 oc2 回车再次启动
powershell.exe -NoProfile -NoExit -ExecutionPolicy Bypass -File .\voice_console.ps1
```
