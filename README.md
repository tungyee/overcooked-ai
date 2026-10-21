# Overcooked! 2 AI 陪玩（oc2）

给合作烹饪游戏 [Overcooked! 2](https://store.steampowered.com/app/728880/Overcooked_2/) 写的 **AI 陪玩（P2 自动玩家）**：游戏里由 BepInEx 插件把整局状态以 NDJSON 快照推到本机，Python 进程读快照、做决策，再通过插件 exec 通道（27412）发命令驱动虚拟手柄控制分屏的 2P，与人类玩家（1P）协作做菜、出餐。运行行为树不需要模型 API。

## 工作原理（一句话）

```
游戏（BepInEx 插件，5 Hz 推 NDJSON → 127.0.0.1:27411）
        ↓
Python 主循环（20 Hz 收帧 → 对账 → 推断 P1 → 仲裁 → 规划 → 执行 → 日志）
        ↓
插件 exec 通道 + InControl 虚拟手柄（控制 P2）
```

决策采用「三层 + 黑板」架构：`World` 快照 → `Arbiter` 选模式 → `Planner` 选任务 → `Executor` 用行为树产出 exec 命令。详细设计见 `docs/superpowers/specs/`。

## 前置条件

- **macOS**（插件驱动虚拟手柄，无需「辅助功能」权限）
- Steam 版 **Overcooked! 2**，且已装 **BepInEx**（插件宿主，游戏目录下有 `BepInEx/`）
- **Python 3.9+**（运行仅依赖标准库）
- **.NET SDK**（仅编译插件时需要，见下文环境变量）

## Windows

键盘注入（SendInput）后端已移除；Windows 与 macOS 一样走插件 exec 通道 + 虚拟手柄（InControl），编译部署插件后启动游戏与 `run_agent.py` 即可。本分支配套插件 **0.19.0 / protocol 4**；旧插件不能与新版 Python 混用。

## macOS 快速开始

> 新机器首次搭建（装 BepInEx/doorstop、建 `run_game.sh`）见 [`docs/setup.md`](docs/setup.md)。

### 1. 编译并部署插件（仅改插件源码时需要）

```bash
./plugin/build.sh
cp plugin/build/Oc2AIPlugin.dll "$GAME_DIR/BepInEx/plugins/Oc2AIPlugin.dll"
```

`GAME_DIR` 默认为 `$HOME/Library/Application Support/Steam/steamapps/common/Overcooked! 2`。换机器可用环境变量覆盖：`GAME_DIR`（游戏安装目录）、`DOTNET`（dotnet 可执行文件）、`CSC_DLL`（Roslyn `csc.dll` 路径）；未指定时脚本自动探测。

### 2. 启动游戏

```bash
"$GAME_DIR/run_game.sh"   # 必须走脚本启动（注入 BepInEx），进入关卡 Sushi_1_4_2P
```

### 3. 启动 AI

```bash
python3 run_agent.py            # 正式：连游戏 + 插件 exec（虚拟手柄）
python3 run_agent.py --dry-run  # 只读：不连 exec（NullExec），只看决策
```

退出顺序：先 `Ctrl-C` 停 Agent（会关闭 exec 通道），再退出游戏。

## 测试

```bash
python3 -m pip install -r requirements-dev.txt
python3 -m pytest tests/
```

## 目录结构

```
ai/        纯库：决策与执行（agent/arbiter/planner/executor + primitives/tasks）
plugin/    C# BepInEx 插件（数据源）+ 编译脚本
tests/     pytest 测试
tools/     开发工具（录帧 / 树可视化 / DLL 探针）
docs/      架构 spec、交付总结、浸泡报告
run_agent.py  唯一入口
```

## 已知限制

- 关卡相关数据（传送带、盘堆兜底坐标）当前只对 `Sushi_1_4_2P` 标注，换关卡可能不准。
- 熟料在盘内的 token 不透明，多熟料关卡区分受限。
- 打分权重为经验初值，未调优。

## 更多文档

- `AGENTS.md` —— 面向开发者的完整说明（架构、约定、命令、坑）
- `docs/` —— 设计 spec、各 Plan 交付总结、实机浸泡报告
