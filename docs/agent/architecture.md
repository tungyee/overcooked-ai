# 技术栈、目录结构与核心架构

本文是 `AGENTS.md` 的详细补充，面向需要了解实现细节的 AI 编码代理。`ai/` 内的注释大量引用「spec §N」，所指是 `docs/superpowers/specs/2026-10-04-ai-behavior-architecture-design.md`。

## 1. 技术栈与运行架构

- **语言/运行时**：Python 3.9.6（macOS 系统 Python）。无打包、无构建步骤，`ai/` 是纯库，通过 `pytest.ini` 的 `pythonpath = .` 直接 import。
- **依赖**：运行时除标准库外只有 **`Quartz`（pyobjc）**，且仅在 `ai/output/controller.py`（真实按键注入）用到；决策逻辑完全不依赖第三方库。开发依赖仅 `pytest==7.4.4`（见 `requirements-dev.txt`）。没有 `pyproject.toml` / `setup.py` / `package.json`。
- **数据链路**：C# BepInEx 插件（`plugin/Oc2AIPlugin.cs`）5 Hz 推 NDJSON → `ai/state/world.py` 的 `StateClient` 按 `\n` 分帧 → `World` 解析快照。
- **控制链路**：`ai/output/hands.py`（按键状态层）→ 注入的 controller。正式运行时是 `ai/output/controller.py`（CGEvent，需 macOS「辅助功能」权限）；测试/离线时用 `NullController`（`ai/agent.py` 内定义）或测试里注入的假 controller。
- **频率约定（关键，容易混淆）**：插件推送 **5 Hz**；Python 主循环 **20 Hz**（`TICK_INTERVAL = 0.05`）。代码里「拍」（tick）统一指 **Python 循环拍 = 50 ms**，所有按拍计的阈值都按 20 Hz 理解。实现上主循环用 `StateClient.read_latest()` **非阻塞读**、中间 4 拍复用最新帧重 tick——不能用阻塞的 `read()`，否则会被 5 Hz 推帧拖成 5 Hz（曾踩坑）。

## 2. 目录结构

`ai/` 是纯库（无 `__main__`），唯一入口是根级 `run_agent.py`。其余顶层目录：`plugin/`（C# BepInEx 插件 = 数据源）、`tests/`、`tools/`（开发工具，非运行时）、`docs/`、`.superpowers/`（SDD brief/report/review）。

`ai/` 各模块职责（`agent.py` 是根级入口，其余按职责分到子目录）：

| 子目录 | 模块 |
|---|---|
| 根级 | `agent.py` 13 步装配（收帧→对账→推断→仲裁→规划→执行→日志）· `primitives/` 原语 · `tasks/` task（对外只认 `from ai import tasks as T`） |
| `core/` 主循环 | `arbiter.py` 模式仲裁 · `planner.py` 候选→可行性→协作过滤→打分→承诺惯性（最大文件，约 1800 行）· `planner_layers.py` 分层过滤 · `executor.py` task 树生命周期 |
| `state/` 状态 | `blackboard.py` 唯一可变状态容器 · `bb_factory.py` 其构造 · `world.py` StateClient 分帧 + World 只读视图 · `reconcile.py` 每拍认领对账 · `p1model.py` P1 事实（holding）+ 站位软代价 |
| `query/` 查询 | `predicates.py` 纯谓词（无副作用、不依赖 World 类）· `recipes.py` RecipeBook 配方语义 · `recipe_graph.py` 配方 DAG 编译 + 候选发射 · `plate_tokens.py` 盘/锅内容 token 解析 · `stations.py` 站点检索 · `placement.py` 落点约束 |
| `space/` 空间 | `navigation.py` Dijkstra 距离场 · `occupancy.py` 可走性 + 实体去重 · `geometry.py` 网格↔世界坐标 |
| `revenue/` 收益 | `economy.py` 小费/连击 · `eta.py` 订单 ETA/slack/必死 · `cook_supply.py` 熟料供需（见下） |
| `output/` 输出 | `hands.py` 按键唯一出口 · `key_scheduler.py` 按键定时松开 · `controller.py` CGEvent 注入（仅实机）· `trace.py` 决策日志 · `state_dump.py` 快照压成可序列化 dict |
| `exec/` 执行/回放 | `bt.py` 行为树引擎（有记忆的 Sequence/Selector + RepeatUntil/Guard）· `record.py`/`replay.py` 录帧回放 |

## 3. 核心架构

三层 + 黑板（spec §3）：

```
World 快照 → 认领对账 → 更新 P1Model → Arbiter 选模式
  → Normal 则 Planner 选 task，否则取中断 task → Executor tick 成按键 → 输出 trace
```

- **Arbiter**（`arbiter.py`）：每拍先跑，只决定「谁拿控制权」，不执行动作。返回 `(mode, reason)`。优先级：卡住守卫 → 中断 task 连续失败降级 → Firefighting（灶着火且 P1 未在处理）→ Normal。Arbiter 只读黑板，模式写入由调用方（Agent）负责。**卡住守卫（同一格连续 N 拍）目前只记 `stuck detected` 日志、不触发脱困动作**——实际脱困靠两层：`HoldUntil` 的位置自愈 + Agent 连续失败计数写 `scratch.abandoned`。
- **Planner**（`planner.py`）：纯函数步进器。`select_task(world, bb, order, book)` 走：
  生成候选（枚举所有订单，Plan 4 多订单模式）→ 可行性过滤（`claims_available` 前置 +
  `_is_abandoned` 拉黑检查 + 各 task 的 `feasible()`）→ **分层过滤（取最高层，见
  `planner_layers.py`）** → 打分（该层白名单内的项）→ 承诺惯性（防抖 + 穿插）。
  **无状态每拍重算**，执行顺序从可行性涌现，不建 DAG、不拓扑排序。层是派生值：
  高层动作不可达时该候选自然从 feasible 消失、层自己掉下来。
- **Executor**（`executor.py`）：把 task 换成一棵小的 `bt` 行为树，每拍 tick 产出按键。task 身份（四元组，见 `tasks-primitives.md`）不变就复用同一棵树（进度自然保留），变了就 halt 旧树重建；每棵树包一层 `Timeout` 做最后兜底，默认 `MAX_TASK_TICKS=300` 拍，task 可用类属性 `TIMEOUT_TICKS` 声明自己的上限（灭火是两趟差事，用 `FIREFIGHT_TIMEOUT_TICKS=800`）。
- **Blackboard**（`blackboard.py`）：唯一可变状态容器，取代旧 Agent 上散落的约 20 个跨拍字段。写权受控（spec §4.5）：Planner 写 `claims`/`commitment`，P1Model 写 `p1_model`，Executor 只写 `commitment.steps_done/retries` 与 `scratch`（**不可写 claims**）。
- **World**（`world.py`）：一帧快照的**只读**视图，构造时只做解析 + 派生数据装配；可走性与寻路分别委托 `occupancy` 与 `navigation`。
- **Trace**（`trace.py`）：每拍写一条 JSONL 记录（模式/候选/打分/选中 task/执行状态/按键），落盘仓库根 `trace-<时间戳>.jsonl`（已 gitignore），用于复盘「为什么选这个动作」。另有 `revenue` 摘要（分数/连击/剩余时间/订单 + 送达/连击归零/订单过期三类事件），省去手工 diff 全局 state。

### 3.1 收益模型与产能

这三个模块回答「为什么会 miss、怎么多赚分」。常数**全部来自真实 trace 回归**，不是经验值——改之前先看模块注释里的实测表：

- **`economy.py`** —— 收益函数。小费 = 新鲜度档位（8/5/3，按 `remaining/lifetime` 落在 >2/3、1/3~2/3、<1/3）× 连击系数（combo < 3 恒为 1，之后 `combo-1`）；`base` 固定 60/单，与早晚无关。**超时不扣分**（`EXPIRY_POINT_PENALTY = 0`，社区攻略说的 −10 coin 实测不存在），代价只在连击归零 + 沉没工时。由此定出优先级：**不丢单 >> 按序送 >> 送得新鲜**（新鲜度最多差 5 分，相对 base 60 仅 8%）。52 次送达逐条断言。
- **`eta.py`** —— 订单 ETA / slack / 必死判定。单位耗时取实测中位（见模块注释的表，两局「拍/格」6.2 与 5.9 吻合，已含移动损耗故不再乘裕度）。两个关键建模：① **烹饪是关键路径不是求和**（煮饭 12 秒里人自由，取 max）；② **多单共享一双手**，按最早截止排队后累计手上工时，且**判死的单不占队列容量**（否则判死会沿队列雪崩）。校准基准：一份寿司 ≈ 35~40 秒手上工时 → 130s 寿命最多托住 3 单。
- **`cook_supply.py`** —— 熟料供需。`demand` 只数需求、`supply` 数「在煮 + 已熟未端 + 手持 + 已装盘」，**已装盘的那份只能在一侧扣减**（两边都扣会让缺口被重复减去，这个 bug 单测抓不到、只有真实帧能暴露）。

**产能（为什么需要 `FillCooker`）**：`FetchToCooker` 绑订单，只能由 `recipe_graph.emit` 按单发射，`_in_flight_cook_claim` 压掉同单第二个候选，`_match_cookers_to_orders` 又是严格 1:1 —— 结构上「5 单要米」就不可能变成「3 口灶在烧」。实测 trace-20261014-120406 的 1700~3270 窗口 **59% 的拍里三口灶一个都没在煮**，而三单全在这窗口过期。灶空转的机会成本是 12 秒不可压缩的时钟，所以用新 task 换结构，而不是调权重。

**combo 的真实机制**（57 次送达假设检验，两条规律各自无反例）：
1. 送出一盘 dish D，游戏把它记给「同 dish 存活单里 `remaining` 最小」的那个（55/0）。**这不是我们能选的。**
2. combo 递增 ⟺ 被记账的那单是「`id` 数值最小」者（57/0；对比假设「remaining 最小」只有 43/14 —— 两者分歧是因为订单过期后 id 会被复用）。

**推论：AI 的杠杆是「选哪种菜」，不是「选哪一单」。** `DeliverPlate(order#X)` 里的 X 对 combo 毫无影响，所以送餐的紧迫度/新鲜度/完成度统一按 `_credited_order_id` 求值。又因为实机「料齐的盘」几乎从不同时有两张（本局 0 盘 4279 拍、1 盘 298 拍），送餐那一刻没有分叉，combo 偏好必须传导到**备菜**才有用（`COMBO_PREP_DISCOUNT`）。
