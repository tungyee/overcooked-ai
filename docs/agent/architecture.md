# 技术栈、目录结构与核心架构

本文是 `AGENTS.md` 的补充，概述技术栈、目录结构与核心架构。代码注释里的「spec §N」指 `docs/superpowers/specs/2026-10-04-ai-behavior-architecture-design.md`。

## 1. 技术栈与运行架构

- **语言**：Python 3.9+，`ai/` 是纯库（无打包、无 `__main__`），`pytest.ini` 设 `pythonpath = .` 直接 import。
- **依赖**：运行时除标准库无第三方依赖；开发依赖仅 pytest；语音链路额外依赖见 `requirements-voice.txt`（启用语音时才加载）。
- **数据链路**：C# BepInEx 插件（`plugin/Oc2AIPlugin.cs`）5 Hz 推 NDJSON → `ai/state/world.py` 的 `StateClient` 按 `\n` 分帧 → `World` 解析快照。
- **控制链路**：叶子原语经 `ctx["exec"]` 发 move/tap/hold 等命令 → `ExecClient`（`ai/exec/client.py`）写 27412 → 插件逐帧驱动 InControl 虚拟手柄控制 P2（无需 macOS「辅助功能」）。离线/测试用 `NullExec`（命令即时回 ok，不连 27412）。
- **频率**：插件推 **5 Hz**，主循环 **20 Hz**（`TICK_INTERVAL = 0.05`）。「拍」= 50 ms。主循环用 `read_latest()` 非阻塞读、中间拍复用最新帧，不能用阻塞 `read()`（否则被拖成 5 Hz）。

## 2. 目录结构

`ai/` 各模块职责（根级入口 `agent.py`，对外只认 `from ai import tasks as T`）：

| 子目录 | 模块 |
|---|---|
| 根级 | `agent.py` 装配 · `primitives/` 原语 · `tasks/` task |
| `core/` 主循环 | `arbiter.py` 卡住检测 · `planner.py` 候选→可行性→分层→打分→承诺惯性 · `planner_layers.py` 分层过滤 · `executor.py` task 树 |
| `state/` 状态 | `blackboard.py` 唯一可变状态 · `bb_factory.py` 构造 · `world.py` 只读快照 · `reconcile.py` 认领对账 · `p1model.py` P1 事实与站位 |
| `query/` 查询 | `predicates.py` 纯谓词 · `recipes.py` 配方 · `recipe_graph.py` 配方 DAG + 候选发射 · `plate_tokens.py` 盘/锅 token · `stations.py` 站点 · `placement.py` 落点约束 |
| `space/` 空间 | `navigation.py` 距离场 · `occupancy.py` 可走性 · `geometry.py` 坐标 · `seams.py`/`shuttle.py`/`channels.py`/`portals.py`/`lanes.py` 接缝/渡运/跨区/传送门/半格走廊 |
| `revenue/` 收益 | `economy.py` 小费连击 · `eta.py` 订单 ETA · `cook_supply.py` 熟料供需 |
| `output/` 输出 | `trace.py` 决策日志 · `state_dump.py` 快照序列化 |
| `exec/` 执行 | `client.py` ExecClient/NullExec · `commands.py` 命令 · `bt.py` 行为树 · `record.py`/`replay.py` 录帧回放 |
| `dialog/` 对话 | `schema.py`/`policy.py`/`worker.py`/`audio.py`/`ptt.py`/`asr.py`/`tts.py`/`llm.py`/`env.py`（可选） |

## 3. 核心架构

决策执行分两步——Planner 选 task、Executor tick 成按键——共享状态放 Blackboard：

```
World 快照 → 认领对账 → 更新 P1Model → 卡住观测
  → Planner 选 task → Executor tick 成按键 → 输出 trace
```

- **Planner**（`planner.py`）：纯函数步进器，每拍重新生成候选（枚举订单）→ 可行性过滤 → 分层过滤（取最高层）→ 打分 → 承诺惯性。无状态重算，执行顺序从可行性涌现，不建 DAG；高层动作不可达时层自然掉落。火情由 `L_FIRE` 顶层（`Extinguish` 候选）统一处理。
- **Executor**（`executor.py`）：把 task 换成一棵小的行为树，每拍 tick 产出按键。task 身份（四元组）不变就复用同一棵树（进度保留），变了就 halt 重建；每棵树包 Timeout 兜底。
- **Blackboard**（`blackboard.py`）：唯一可变状态容器，写权受控：Planner 写 `claims`/`commitment`，P1Model 写 `p1_model`，Executor 只写 `steps_done`/`retries` 与 `scratch`。
- **World**（`world.py`）：一帧快照的只读视图，构造时只解析 + 派生数据；可走性/寻路委托 `occupancy`/`navigation`。
- **Trace**（`trace.py`）：每拍写一条 JSONL（模式/候选/打分/选中 task/执行状态/按键），落盘 `runs/trace-<时间戳>.jsonl`（已 gitignore），用于复盘「为什么选这个动作」。

### 3.1 收益模型

`revenue/` 三个模块回答「为什么会 miss、怎么多赚分」，常数全部来自真实 trace 回归（改前看模块注释的实测表）。核心结论：**不丢单 >> 按序送 >> 送得新鲜**；combo 的杠杆在「选哪种菜」而非「选哪一单」，所以 combo 偏好要传导到备菜（`COMBO_PREP_DISCOUNT`）。

### 3.2 对话层与分工

对话层线程边界、语音 PTT、分工（`set_division`）、动态复核（`division_review`/`layout_review`）、攻略（`cookbook`）的完整约定见 `AGENTS.md`，此处不重复。
