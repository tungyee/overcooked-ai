# AGENTS.md — Overcooked! 2 AI 陪玩（oc2）

面向 AI 编码代理的项目速览。**详细内容在 `docs/agent/` 下**：架构细节见 `architecture.md`，task/原语/认领见 `tasks-primitives.md`，技术债务与待办见 `status-backlog.md`。代码注释里大量引用「spec §N」，所指是 `docs/superpowers/specs/2026-10-04-ai-behavior-architecture-design.md`。

## 1. 项目概览

给合作烹饪游戏 **Overcooked! 2** 写的 **AI 陪玩（控制分屏 2P）**：C# BepInEx 插件把整局状态以 NDJSON 全量快照推到 `127.0.0.1:27411`（5 Hz），Python 进程读快照做决策，再通过 macOS CGEvent 键盘注入控制 P2，与人类 P1 协作做菜、出餐。

- C# 插件源码 `plugin/Oc2AIPlugin.cs`，NDJSON 字段是两侧共用的契约，改字段要同步。
- 主关卡 `Sushi_1_4_2P`，部分关卡数据仍对该关硬编码。

## 2. 关键约定（务必遵守）

- **`ai/` 目录结构**：根级只有入口 `agent.py` 和 `primitives/`、`tasks/` 两个子包（对外只认 `from ai import tasks as T` / `from ai import primitives as PR`）。其余模块按职责分到子目录：`core/`（arbiter/planner/planner_layers/executor）、`state/`（blackboard/bb_factory/world/reconcile/p1model）、`query/`（predicates/recipes/recipe_graph/plate_tokens/stations/placement）、`space/`（navigation/occupancy/geometry）、`revenue/`（economy/eta/cook_supply）、`output/`（hands/key_scheduler/controller/trace/state_dump）、`exec/`（bt/record/replay）。跨目录 import 用 `from ai.<子目录> import X` 或 `from ai.<子目录>.<模块> import ...`。
- **频率**：插件推帧 **5 Hz**，Python 主循环 **20 Hz**。代码里「拍」（tick）= Python 循环拍 = **50 ms**，按拍计的阈值都按 20 Hz 理解。主循环用 `StateClient.read_latest()` **非阻塞读**，中间 4 拍复用最新帧重 tick（不能用阻塞 `read()`，否则被拖成 5 Hz）。
- **task 契约**：`ai/tasks/base.py` 定义 `Task` 基类。task = 四元身份 `(TYPE, order_id, bound_resources, bound_item)`，不可变值对象。四个契约 `claims()`/`feasible(ctx)`/`done(ctx)`/`build()`。对外入口只有 `from ai import tasks as T`（不要 import 子模块），`T.TASK_TYPES` 登记全部 19 个类型。
- **资源 id 与认领**：灶/板/洗碗池 → `(网格名, 站点实例名)`；拼装格 → 网格坐标 `(gx, gz)`；盘 → 实例名 `n`。认领是 **`(资源, 动作)` 动作级**粒度，owner 常量 `OWNER = "P2"`。
- **代码风格**：注释/文档中文、标识符英文；类型注解用 `# type: (...) -> ...` 注释形式；谓词一律无副作用、只读入参不依赖 `World` 类；controller 依赖注入（`Hands(ctrl)`）。
- **测试**：pytest 7.4.4，配置见 `pytest.ini`。写新功能先加测试；真实帧 fixture 以 `.ndjson.gz` 入库（`tests/fixtures/`）。

## 3. 常用命令

Python 侧无构建步骤；C# 插件需单独编译。命令在仓库根执行。

```bash
G="$HOME/Library/Application Support/Steam/steamapps/common/Overcooked! 2"

# 可选前置：仅改了 plugin/Oc2AIPlugin.cs 时才需要（之后必须重启游戏才生效）
./plugin/build.sh && cp plugin/build/Oc2AIPlugin.dll "$G/BepInEx/plugins/"

"$G/run_game.sh"        # ① 起游戏（必须用 run_game.sh，Steam 直接启动不加载插件）
python3 run_agent.py    # ② 起 Agent（--dry-run 只看决策、不注入按键）
```

- Agent 连不上 27411 直接退出（退出码 1）；收到 `level` 帧才真正决策。真实注入需 macOS「辅助功能」权限。
- **退出顺序：先停 Agent 再退游戏**（否则残留按住的方向键）。

```bash
python3 -m pytest tests/                # 全部测试
python3 -m pytest tests/test_eta.py -q  # 单文件 / 单用例（::test_xxx）
python3 tools/tree_viz.py <帧文件>      # 离线冒烟：建全部 task 树、走分支、不按键
python3 tools/record_frames.py          # 录制实时帧到 NDJSON
python3 tools/dll_probe.py <dll> <TypeName>...  # .NET 元数据探针
```

## 4. 安全

- 真实运行会向游戏窗口注入全局键盘事件：需 macOS「辅助功能」权限；`Controller` 构造时先 `release_all()` 并把前台切到 Overcooked2。离线验证用 `--dry-run` 或 `tools/tree_viz.py`（不注入按键）。
- 状态端口仅本机，无凭据/密钥；`.gitignore` 已排除运行时产物（`trace-*.jsonl`、`frames-*.jsonl`、未压缩 fixture 帧、`*.log`、`__pycache__`、`.venv`）。

## 5. 参考文档

- `docs/agent/architecture.md` —— 技术栈、目录结构、核心架构（三层+黑板）、收益模型与产能。
- `docs/agent/tasks-primitives.md` —— task 契约与类型表、执行原语表、资源 id/认领、代码风格。
- `docs/agent/status-backlog.md` —— 已知限制/技术债务、当前状态、待办、改权重前必读。
- `docs/map/` —— 各关卡网格/站点/配方数据；`docs/notes/` —— 一次性调试/修复/复盘记录（历史存档）。
- `docs/superpowers/specs/2026-10-04-ai-behavior-architecture-design.md` —— 架构设计（「spec §N」出处）。
- `docs/superpowers/specs/2026-10-06-e2e-testing-design.md` —— 端到端测试框架设计。
- `docs/superpowers/plans/` —— 各 Plan 实施计划；`.superpowers/sdd/` 下是对应 task brief / report / review。
