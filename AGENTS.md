# AGENTS.md — Overcooked! 2 AI 陪玩（oc2）

## 1-4 台面盘失败循环修复（2026-10-19，最新）

报告 `docs/notes/sushi14-stall-2026-10-19.md`。新实机轨迹 trace-20261019-173236 的 1-4 段 1576 拍有 704 failure：台面盘无网格字段被 Face 误作地面物跳过；Align 排除唯一站位后，Planner 不读 bad_pairs，NO_ABANDON 盛饭每拍继续选同盘。resolve 现在为同位置台面上的物品添加 interaction_surface/grid/gx/gz（不改原物品身份/世界位置），Face/Align/Tap 共享该元数据；reachability.can_interact 读取 bad_pairs，选择别的盘或救锅。新增 12 项真实帧/端到端回归，全量 1316 passed / 85 skipped。旧 AI 已停止，修改后只做离线重放，没有新实控；Python 修改，无 DLL 更新。备份 .runtime/source-before-sushi14-174154.zip。离线重放日志在 .runtime/sushi14-offline-agent-replay.jsonl，不能当实机出餐证据。

## 目标附近卡住修订（2026-10-19，当前增量）

见 `docs/notes/target-approach-recovery-2026-10-19.md` 与对应 plans。最新 1-2 轨迹有多格出餐口朝向追错锚点、对齐后未重新转向、正确句柄仍等绝对静止和计时归零后继续规划。修订路径回溯共用 edge_blocked、近目标折返精确控制/滑出纠偏、Align 两个新观测无进展且句柄正确的受保护交互、复合交互最多两次换位与新帧门槛、失位重走 Approach、EnsureHandsFree 刷新移动站点。bad_pairs 仅排除交互落点，不全局封路；按键后 Confirm 不被位姿/目标消失打断，手变但条件未满足不可重按。新增 interaction_recovery.py、trace 交互阶段和地图上下文。Python 修改，未改 DLL；本轮未实机再跑，不得声称通过实机通关。修改前 ai 备份 .runtime/source-before-target-approach-20261019-172026.zip。

## 本地修复记录（2026-10-19，优先于历史说明）

后续 Windows 集成及 150 秒 1-5 实测已完成，报告 `docs/notes/windows-live-validation-2026-10-19.md`。本目录 .venv、start/stop_windows 启动器可用；新版插件 0.19.0 / protocol 4 已部署，旧 DLL 在本目录 .runtime/backups/20261019-165850-395。Windows 禁用 dash 事件、不用右 Alt，方向键/右 Shift/右 Ctrl。本轮 AI 已到时退出：任务终态成功 11、失败 19；全队新增出餐 4，其中最后 1 份有 P2 持盘面对出餐口→空手与分数增加证据，不能全归因 AI。仍有导航/交互重试与插件约 47ms 的采样开销，不得声称稳定通关或性能问题已解决。实测后追加 P.holds_plate，按 held_by 修复同模型盘误归属；持盘详情加入 trace，trace 路径修回仓库根；追加修复仅离线回归，尚未再次实控。真实持盘 fixture 放 tests/fixtures/ownership，不能放根 fixtures（根的旧 parity 测试固定 1102 帧与单网格假设）。

基线 b7a1f82，分支 fix/bt-stall-recovery。复现与修改见 `docs/notes/bt-stall-recovery-2026-10-19.md`，计划见 `docs/plans/2026-10-19-bt-stall-recovery.md`。新增 core/recovery.py 的操作失败历史与动作冷却，space/reachability.py 的当前帧可达性与复用；修复原地挡火、认领残留、切关遗留和局部重算丢平台限制。无任务 trace 发布 waiting 原因。保留 NO_ABANDON 与旧显式 abandoned 资源表，不能混淆 owner="P2" 和厨师名="Player 2"。

本轮在独立目录修复最新上游，未覆盖旧 Windows 本地项目或游戏 DLL；未启动游戏控制。上游仍为 macOS 输入与新版 terrain/grids 协议，实机集成须另行核对。验证结果见修复报告。

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
