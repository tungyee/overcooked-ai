# AGENTS.md — Overcooked! 2 AI 陪玩（oc2）

本文档面向对本项目一无所知的 AI 编码代理，介绍项目是什么、怎么跑、怎么测、代码怎么组织、以及必须遵守的约定。事实均来自当前仓库内容；`ai/` 内的注释大量引用「spec §N」，所指是 `docs/superpowers/specs/2026-10-04-ai-behavior-architecture-design.md` 这份架构设计文档。

## 1. 项目概览

这是一个给合作烹饪游戏 **Overcooked! 2** 写的 **AI 陪玩（P2 自动玩家）**。游戏内通过 BepInEx 安装一个 C# 插件，把整局状态以 **NDJSON 全量快照**推到本机 `127.0.0.1:27411`（5 Hz）；本仓库的 Python 进程读取快照、做出决策，再通过 **macOS CGEvent 键盘注入**控制分屏的 2P（Player 2），与人类玩家（Player 1）协作做菜、出餐。

- C# 插件源码在本仓库 `plugin/Oc2AIPlugin.cs`，可随项目一起修改、编译（`./plugin/build.sh`，见 §5）。socket 传输协议（NDJSON 字段）是插件与 Python 侧共用的契约，改字段时两侧要同步。
- 当前主要关卡是 `Sushi_1_4_2P`；少数关卡相关数据（传送带、盘堆兜底坐标）仍对该关硬编码（见 §8 已知限制）。

## 2. 技术栈与运行架构

- **语言/运行时**：Python 3.9.6（macOS 系统 Python）。无打包、无构建步骤，`ai/` 是纯库，通过 `pytest.ini` 的 `pythonpath = .` 直接 import。
- **依赖**：运行时除标准库外只有 **`Quartz`（pyobjc）**，且仅在 `ai/controller.py`（真实按键注入）用到；决策逻辑完全不依赖第三方库。开发依赖仅 `pytest==7.4.4`（见 `requirements-dev.txt`）。没有 `pyproject.toml` / `setup.py` / `package.json`。
- **数据链路**：C# BepInEx 插件（`plugin/Oc2AIPlugin.cs`）5 Hz 推 NDJSON → `ai/world.py` 的 `StateClient` 按 `\n` 分帧 → `World` 解析快照。
- **控制链路**：`ai/hands.py`（按键状态层）→ 注入的 controller。正式运行时是 `ai/controller.py`（CGEvent，需 macOS「辅助功能」权限）；测试/离线时用 `NullController`（`ai/agent.py` 内定义）或测试里注入的假 controller。
- **频率约定（关键，容易混淆）**：插件推送 **5 Hz**；Python 主循环 **20 Hz**（`TICK_INTERVAL = 0.05`）。代码里「拍」（tick）统一指 **Python 循环拍 = 50 ms**，所有按拍计的阈值都按 20 Hz 理解。实现上主循环用 `StateClient.read_latest()` **非阻塞读**、中间 4 拍复用最新帧重 tick——不能用阻塞的 `read()`，否则会被 5 Hz 推帧拖成 5 Hz（曾踩坑）。

## 3. 目录结构

`ai/` 是纯库（无 `__main__`），唯一入口是根级 `run_agent.py`。其余顶层目录：`plugin/`（C# BepInEx 插件 = 数据源）、`tests/`、`tools/`（开发工具，非运行时）、`docs/`、`.superpowers/`（SDD brief/report/review）。

`ai/` 各模块职责：

| 组 | 模块 |
|---|---|
| **主循环** | `agent.py` 13 步装配（收帧→对账→推断→仲裁→规划→执行→日志）· `arbiter.py` 模式仲裁 · `planner.py` 候选→可行性→协作过滤→打分→承诺惯性（最大文件，约 1800 行）· `executor.py` task 树生命周期 |
| **状态** | `blackboard.py` 唯一可变状态容器 · `bb_factory.py` 其构造 · `world.py` StateClient 分帧 + World 只读视图 · `reconcile.py` 每拍认领对账 · `p1model.py` 人类意图推断 |
| **查询** | `predicates.py` 纯谓词（无副作用、不依赖 World 类）· `recipes.py` RecipeBook 配方语义 · `recipe_graph.py` 配方 DAG 编译 + 候选发射 · `plate_tokens.py` 盘/锅内容 token 解析 · `stations.py` 站点检索 · `placement.py` 落点约束 |
| **空间** | `navigation.py` Dijkstra 距离场 · `occupancy.py` 可走性 + 实体去重 · `geometry.py` 网格↔世界坐标 |
| **收益** | `economy.py` 小费/连击 · `eta.py` 订单 ETA/slack/必死 · `cook_supply.py` 熟料供需（见 §4.1） |
| **输出** | `hands.py` 按键唯一出口 · `key_scheduler.py` 按键定时松开 · `controller.py` CGEvent 注入（仅实机）· `trace.py` 决策日志 · `state_dump.py` 快照压成可序列化 dict |
| **执行** | `bt.py` 行为树引擎（有记忆的 Sequence/Selector + RepeatUntil/Guard）· `primitives/` 原语（见 §6.2）· `tasks/` task（见 §6.1）· `record.py`/`replay.py` 录帧回放 |

## 4. 核心架构

三层 + 黑板（spec §3）：

```
World 快照 → 认领对账 → 更新 P1Model → Arbiter 选模式
  → Normal 则 Planner 选 task，否则取中断 task → Executor tick 成按键 → 输出 trace
```

- **Arbiter**（`arbiter.py`）：每拍先跑，只决定「谁拿控制权」，不执行动作。返回 `(mode, reason)`。优先级：卡住守卫 → 中断 task 连续失败降级 → Firefighting（灶着火且 P1 未在处理）→ Assigned（对话层指派，当前为空壳）→ Normal。Arbiter 只读黑板，模式写入由调用方（Agent）负责。**卡住守卫（同一格连续 N 拍）目前只记 `stuck detected` 日志、不触发脱困动作**——实际脱困靠两层：`HoldUntil` 的位置自愈（见 §6.2）+ Agent 连续失败计数写 `scratch.abandoned`（见 §6.2 `AbandonTarget` 行）。
- **Planner**（`planner.py`）：纯函数步进器。`select_task(world, bb, order, book)` 走：
  生成候选（枚举所有订单，Plan 4 多订单模式）→ 可行性过滤（`claims_available` 前置 +
  `_is_abandoned` 拉黑检查 + 各 task 的 `feasible()`）→ **分层过滤（取最高层，见
  `planner_layers.py`）** → 打分（该层白名单内的项）→ 承诺惯性（防抖 + 穿插）。
  **无状态每拍重算**，执行顺序从可行性涌现，不建 DAG、不拓扑排序。层是派生值：
  高层动作不可达时该候选自然从 feasible 消失、层自己掉下来。
- **Executor**（`executor.py`）：把 task 换成一棵小的 `bt` 行为树，每拍 tick 产出按键。task 身份（四元组，见 §6）不变就复用同一棵树（进度自然保留），变了就 halt 旧树重建；每棵树包一层 `Timeout` 做最后兜底，默认 `MAX_TASK_TICKS=300` 拍，task 可用类属性 `TIMEOUT_TICKS` 声明自己的上限（灭火是两趟差事，用 `FIREFIGHT_TIMEOUT_TICKS=800`）。
- **Blackboard**（`blackboard.py`）：唯一可变状态容器，取代旧 Agent 上散落的约 20 个跨拍字段。写权受控（spec §4.5）：Planner 写 `claims`/`commitment`，P1Model 写 `p1_model`，Executor 只写 `commitment.steps_done/retries` 与 `scratch`（**不可写 claims**）。
- **World**（`world.py`）：一帧快照的**只读**视图，构造时只做解析 + 派生数据装配；可走性与寻路分别委托 `occupancy` 与 `navigation`。
- **Trace**（`trace.py`）：每拍写一条 JSONL 记录（模式/候选/打分/选中 task/执行状态/按键），落盘仓库根 `trace-<时间戳>.jsonl`（已 gitignore），用于复盘「为什么选这个动作」。另有 `revenue` 摘要（分数/连击/剩余时间/订单 + 送达/连击归零/订单过期三类事件），省去手工 diff 全局 state。

### 4.1 收益模型与产能

这三个模块回答「为什么会 miss、怎么多赚分」。常数**全部来自真实 trace 回归**，不是经验值——改之前先看模块注释里的实测表：

- **`economy.py`** —— 收益函数。小费 = 新鲜度档位（8/5/3，按 `remaining/lifetime` 落在 >2/3、1/3~2/3、<1/3）× 连击系数（combo < 3 恒为 1，之后 `combo-1`）；`base` 固定 60/单，与早晚无关。**超时不扣分**（`EXPIRY_POINT_PENALTY = 0`，社区攻略说的 −10 coin 实测不存在），代价只在连击归零 + 沉没工时。由此定出优先级：**不丢单 >> 按序送 >> 送得新鲜**（新鲜度最多差 5 分，相对 base 60 仅 8%）。52 次送达逐条断言。
- **`eta.py`** —— 订单 ETA / slack / 必死判定。单位耗时取实测中位（见模块注释的表，两局「拍/格」6.2 与 5.9 吻合，已含移动损耗故不再乘裕度）。两个关键建模：① **烹饪是关键路径不是求和**（煮饭 12 秒里人自由，取 max）；② **多单共享一双手**，按最早截止排队后累计手上工时，且**判死的单不占队列容量**（否则判死会沿队列雪崩）。校准基准：一份寿司 ≈ 35~40 秒手上工时 → 130s 寿命最多托住 3 单。
- **`cook_supply.py`** —— 熟料供需。`demand` 只数需求、`supply` 数「在煮 + 已熟未端 + 手持 + 已装盘」，**已装盘的那份只能在一侧扣减**（两边都扣会让缺口被重复减去，这个 bug 单测抓不到、只有真实帧能暴露）。

**产能（为什么需要 `FillCooker`）**：`FetchToCooker` 绑订单，只能由 `recipe_graph.emit` 按单发射，`_in_flight_cook_claim` 压掉同单第二个候选，`_match_cookers_to_orders` 又是严格 1:1 —— 结构上「5 单要米」就不可能变成「3 口灶在烧」。实测 trace-20261014-120406 的 1700~3270 窗口 **59% 的拍里三口灶一个都没在煮**，而三单全在这窗口过期。灶空转的机会成本是 12 秒不可压缩的时钟，所以用新 task 换结构，而不是调权重。

**combo 的真实机制**（57 次送达假设检验，两条规律各自无反例）：
1. 送出一盘 dish D，游戏把它记给「同 dish 存活单里 `remaining` 最小」的那个（55/0）。**这不是我们能选的。**
2. combo 递增 ⟺ 被记账的那单是「`id` 数值最小」者（57/0；对比假设「remaining 最小」只有 43/14 —— 两者分歧是因为订单过期后 id 会被复用）。

**推论：AI 的杠杆是「选哪种菜」，不是「选哪一单」。** `DeliverPlate(order#X)` 里的 X 对 combo 毫无影响，所以送餐的紧迫度/新鲜度/完成度统一按 `_credited_order_id` 求值。又因为实机「料齐的盘」几乎从不同时有两张（本局 0 盘 4279 拍、1 盘 298 拍），送餐那一刻没有分叉，combo 偏好必须传导到**备菜**才有用（`COMBO_PREP_DISCOUNT`）。

## 5. 常用命令

Python 侧没有构建步骤；C# 插件需单独编译（`./plugin/build.sh`）。Python 命令在仓库根执行。

### 5.1 实机启停

**只有两步要手动：起游戏 → 起 Agent。** 插件是 BepInEx 插件，随游戏自动加载并在 `Awake()` 里监听 `127.0.0.1:27411`，没有「启动插件」这回事。

```bash
G="$HOME/Library/Application Support/Steam/steamapps/common/Overcooked! 2"

# 可选前置：仅改了 plugin/Oc2AIPlugin.cs 时才需要（之后必须重启游戏才生效）
./plugin/build.sh && cp plugin/build/Oc2AIPlugin.dll "$G/BepInEx/plugins/"

"$G/run_game.sh"        # ① 起游戏，进关卡 Sushi_1_4_2P
python3 run_agent.py    # ② 起 Agent（--dry-run 只看决策、不注入按键）
```

- **必须用 `run_game.sh`**（内部是 `DOORSTOP_ENABLED=1` + `DYLD_INSERT_LIBRARIES=libdoorstop.dylib`）。**从 Steam 直接启动不注入 BepInEx**，插件与 socket 都不加载。
- Agent 启动即连 27411，连不上直接退出（退出码 1）；要等进关卡收到 `level` 帧才真正决策。真实注入需终端已获 macOS「辅助功能」权限。
- **退出顺序相反：先停 Agent 再退游戏。** Agent 收 `Ctrl-C`/`SIGTERM` 会先 `release_all()` 松键，顺序反了会残留按住的方向键。

### 5.2 其他常用命令

```bash
python3 -m pytest tests/                       # 全部测试（当前 967 passed, 78 skipped）
python3 -m pytest tests/test_eta.py -q         # 单文件 / 单用例（::test_xxx）

python3 tools/tree_viz.py <帧文件>              # 离线冒烟：建全部 task 树、走分支、不按键
                                               # （旧 fixture 已删，待 record_frames 重录）
python3 tools/record_frames.py                 # 录制实时帧到 NDJSON
python3 tools/dll_probe.py <dll> <TypeName>... # .NET 元数据探针

./plugin/build.sh   # 编译插件。换机器可用 GAME_DIR / DOTNET / CSC_DLL 覆盖路径
```

## 6. 代码组织与关键约定

### 6.1 task 的契约与身份

`ai/tasks/base.py` 定义 `Task` 基类。**task 实例 = 四元身份 `(TYPE, order_id, bound_resources, bound_item)`**，四项全同才是同一个 task；Task 是不可变值对象（`__slots__` + 禁 `__setattr__`），可作 dict 键/set 元素。每个 task 实现四个契约：

- `claims()` → 需要的 `(resource_id, action)` 认领键
- `feasible(ctx)` → 「此刻我可不可做」（按绑定参数求值）
- `done(ctx)` → 完成判定（即下游 task 的 `feasible`，上下游咬合）
- `build()` → 构建行为树（由 Executor 持有，不进黑板）

`ai/tasks/__init__.py` 是对外唯一入口：一律 `from ai import tasks as T`，**不要直接 import 子模块**。子模块按 spec 的「单文件 ≤ 400 行」硬指标分组（plate / cook / fire / chop / assembly / cleanup / mix / relay）。`T.TASK_TYPES` 登记全部 19 个类型，靠 `__init__.py` 的 `import *` 触发 `@_register` 才能完整；`tests/test_tasks.py::test_task_types_registry_covers_all_subclasses` 用计数钉住完整性。

主线 17 个 task 类型（另两个见表后）：

| 模块 | task |
|---|---|
| `chop.py` | `FetchToBoard`、`Chop` |
| `cook.py` | `FetchToCooker`、`TakeCookedToPlate`、`ReturnPot`、`RescueCooked` |
| `fire.py` | `Extinguish` |
| `plate.py` | `PlaceCleanPlate`、`WashPlate`、`ResetPlate` |
| `assembly.py` | `FetchToPlate`、`DeliverPlate` |
| `cleanup.py` | `Discard`、`RecoverLoosePot`、`StashHeld`、`Prefetch` |
| `mix.py` | `Mix` |
| `relay.py` | `PlaceToRelay` |

另有第 18 号 `FillCooker`（`cook.py`，继承 `FetchToCooker`）：**订单无关**的填灶，
`order_id` 恒为 None，谓词是纯世界谓词（空灶 + 熟料缺口 > 0）。它存在的理由是
结构性的，见 §4「产能」一节。

### 6.2 执行原语（`ai/primitives/`）

树的叶子是带强制前后校验的原语（spec §7.2）。ctx 契约（`primitives/base.py`）为 `{w, chef, hands, bb, field, book, tick}`；target 优先传「可调用」以便每拍重算（树跨拍复用，构造时抓的 dict 第二拍就过期）。halt 只松键、不碰物品（优雅退出由抢占方树首的 `EnsureHandsFree` 负责）。

15 个原语（`primitives.__all__` 另含 `Primitive` 基类与 `resolve` 辅助）：

| 原语 | 作用（括号内是容易踩的坑） |
|---|---|
| `Approach` | 走到目标相邻交互位并落定。长距离巡航按住冲刺、距目标 ≤`DASH_BRAKE_CELLS`(3) 步松冲刺、最后一步短按停稳。（**只保证站位、不保证朝向**，定向交互须另配 `Face`；目标格就是脚下格时直接 SUCCESS） |
| `Face` | 转向 target（以 `interactions.gx/gz` 为准）。（`max_ticks` 只是兜底，不是「转好了」的判据） |
| `Tap` | 单击一次，**边沿触发**（同一激活期内只按一次，否则 toggle 会反复拿起放下） |
| `Confirm` | 后置校验 `pred(ctx)->bool`，不成立先 RUNNING 等刷新。（`retries` 按**新快照**消耗，不是按调用次数） |
| `Settle` | 等 N 拍让状态刷新。（只等状态，不等物理稳定——后者已由 `Approach` 取代） |
| `WaitUntil` | **不按任何键**等到 `pred` 成立（端盘在锅前等饭熟）。（`timeout` 必须显式传） |
| `HoldUntil` | 按住 key 直到 `pred` 成立（切菜/喷灭火器/洗盘）。（传 `target` 则每拍校验 `interact_ready`，被传送带推离时松键 FAILURE 让 Sequence 重走 Approach，防空按卡死） |
| `TapWhileHolding` | 按住反向键的同时单击（传送带顶位专用，防被推走） |
| `EnsureHandsFree` | 手里有东西就放到就近合法落点，**幂等**。落点物料感知：空锅回灶、熟锅/糊锅不上灶；`home_cooker=False` 关掉归位（灭火端下来的锅只去台面/地上，否则会被当空灶放回去继续烧） |
| `InteractWith` | 复合 = Approach + (Face) + Tap + Confirm。（`expect` 必传，漏给即 TypeError） |
| `ThrowTo` | `/` 键投掷手持可投掷物，只校验离手。（落点不可控，米曾落到地图边界行够不到） |
| `Timeout` | task 级超时装饰，超时 halt 子树转 FAILURE 交还 Planner |
| `MarkBlocked` / `Nudge` / `AbandonTarget` | 三级脱困，**全部预留未接线**。实际脱困靠两处：`HoldUntil` 的位置自愈，以及 Agent 的连续失败计数（同一 task 连败 `ABANDON_FAIL_STREAK=3` → 写 `scratch.abandoned`，`filter_feasible` 跳过该候选，TTL 200 拍）。**灭火是例外**：`Extinguish` 声明 `NO_ABANDON`、`plan_firefighting` 传 `ignore_abandon=True`——拉黑按资源 id 记，别的 task 在同一口灶上反复失败会把着火的那口灶也写进去 |

移动控制用 `frame_seq/frame_received_at/now` 区分「新观测」与「循环拍」，短按与观察窗口按单调时钟计时（详见 `docs/movement_control_fix_20261010.md`）。`motion.stall_seconds` 暴露巡航空转，见 §10.1。

### 6.3 资源 id 与认领粒度

资源 id 规则（spec §4.4）：
- 灶 / 板 / 洗碗池 → 站点实例名 `n`（稳定）
- **拼装格 → 网格坐标 `(gx, gz)`（坐标即身份）**
- 盘 → 实例名 `n` 为主

认领是 **`(resource_id, action)` 动作级**粒度（不是按资源整锁）：P1 往 1 号锅放生饭只锁 `(cooker1, "fetch_to")`，`(cooker1, "take_cooked")` 仍开放——取熟饭是最有价值的接力。认领的 owner 常量是 `OWNER = "P2"`（AI 控制的是 P2）。

### 6.4 代码风格

- **注释/文档用中文**，标识符用英文。注释强调「为什么」（尤其大量 `实机复现`/`实测 N 帧` 的踩坑记录与 spec § 引用），不要写解释「做了什么」的噪声注释。
- **类型注解用 `# type: (...) -> ...` 注释形式**（非 PEP 526 内联注解），全项目统一。
- 谓词一律**无副作用**：`predicates.py` 的函数只读入参 dict/List，不依赖 `World` 类（这样单测不必造真游戏帧）；`World` 是只读视图。
- 纯函数优先、controller 依赖注入：`Hands(ctrl)` 通过构造注入 controller，测试传记录调用的假 controller，不碰 Quartz。
- 键位沿袭 legacy 实机验证的 P2 分屏键位（`ai/controller.py` 的 `KEY` 表）：方向键移动、右 Shift 拾取、`/` 切菜/投掷/喷火，**右 Option（keycode 61）冲刺**。

## 7. 测试策略

- **框架**：pytest 7.4.4。配置见 `pytest.ini`：`testpaths = tests`、`pythonpath = .`、`python_files = test_*.py`。**没有** `pytest-cov`/覆盖率配置（文档里出现的 `--cov` 命令是建议，未装依赖）。
- **测试组织**：一个被测模块对应一个 `tests/test_<module>.py`；另有集成/端到端：`test_e2e_smoke.py`（端到端冒烟）、`test_soak.py`（600 拍浸泡）、`test_parity.py`/`test_world_parity.py`（与 legacy 行为对齐）、`test_real_frames.py`/`test_real_frame_planner.py`（真实帧驱动）。
- **fixtures**：`tests/conftest.py` 提供会话级 `frames`（把 `tests/fixtures/*.ndjson.gz` 全部真实帧只解析一次）、`level_frame`、`chef_empty`、`chef_holding`、`cooker`。真实帧 fixture **以 `.ndjson.gz` 压缩格式入库**（离线回归依据），未压缩的 `.ndjson` 只留本地（`.gitignore` 已排除）。**旧帧已于 2026-10-08 删除（缺 `detail`/`pot_contents`），待 `tools/record_frames.py` 重录新帧后恢复真实帧回归。**
- **trace 隔离**：`conftest.py` 的 `trace_to_tmp`（session autouse）把 `trace.TRACE_PATH` 改写到 pytest 临时目录，避免测试在仓库根凭空建 `trace-*.jsonl`。
- 写新功能时，先加测试（项目实践是 TDD + 子代理并行开发 + 每 task 单独提交回归）。

## 8. 已知限制 / 技术债务

（多为 spec §10 的未来工作，或文档已记录的妥协）

- **关卡硬编码**：传送带（`occupancy.BELTS`）、盘堆兜底坐标（`stations.PLATE_STACKS`）目前只对 `Sushi_1_4_2P` 标注，未知关卡退化为空集/兜底，不崩但可能不准。泛化需插件输出。
- **P1 名称硬编码**：`"Player 1"` / `"Player 2"`（`predicates.P1_NAME/P2_NAME`）。真实帧 `chefs[].slot` 全为 null，只能按 name 区分（已核实 2204 条 chef 条目）。
- **熟料 token 不透明（仅旧帧）**：`RecipeBook.want_tokens` 对需烹饪的料同时给 rich token（`cooked:{raw}:{cookware}`）和旧帧 opaque `COOKED_PLATE_TOKEN`。新帧 `detail` 已通过 `plate_tokens` 输出 rich token，`plate_has`/`plate_lacks`/`missing` 据此能区分同一盘多份不同熟料；旧帧（无 detail）仍退化为不透明 token，多熟料关卡在旧帧下无法区分。
- **打分权重部分未标定**：收益相关的几项（`W_COOKER_IDLE` / `W_COMBO_*` / `W_FRESHNESS` / slack 曲线）已按真实 trace 定标，其余 `W_*` 与穿插阈值（`INTERLEAVE_THRESHOLD/PENALTY`）仍是经验初值。改之前读 §10.2。
- 对话协作层（`Intents.assignments`、split mode 的显式分单）数据结构已预留但未实装语言解析。
- 文档 `docs/plan4_delivery_summary.md` 提到的「16 个 task」已过时（现为 19），以代码的 `TASK_TYPES` 为准。

## 9. 安全注意事项

- `run_agent.py` 真实运行会向游戏窗口注入全局键盘事件：需要 macOS「辅助功能」权限；`Controller` 构造时会先 `release_all()` 并尝试 `osascript` 把前台切到 Overcooked2 进程，避免键进错窗口。离线验证请用 `--dry-run` 或 `tools/tree_viz.py`（不注入按键）。
- 状态端口 `127.0.0.1:27411` 仅本机；项目不涉及凭据/密钥，`.gitignore` 已排除运行时产物（`trace-*.jsonl`、`frames-*.jsonl`、未压缩 fixture 帧、`*.log`、`__pycache__`、`.venv`）。

## 10. 当前状态

分支 `feat/bt-opt`，全量测试 **1067 passed, 78 skipped**。

本轮（2026-10-15）把打分从扁平加权和改成**层间词典序 + 层内加权和**
（`ai/planner_layers.py`，设计见 `docs/superpowers/specs/2026-10-15-layered-scoring-design.md`）。
动机：17 项加权和有 O(n²) 对交互，单组测试结构上查不出任何一对——§10.2 记的那次
回退就是这个性质的后果。分层后跨类优先级由控制流隔离保证：有高层候选时低层候选
压根不进打分。

层序（数字小 = 层高）：L0 火情 / L1 不可逆损失（锅快糊）/ L2 救料（刚熟的锅）/
L3 兑现 / L4 解卡（无盘）/ L5 异步产能（填灶）/ L6 备菜 / L7 清理。判据是「有
倒计时的压过没倒计时的，倒计时短的压过长的」。

**承诺惯性一行未改**——分层管「挑哪个」，抢占仍由 `COMMITMENT_MARGIN` 与
`pressure >= 1.0 跳过惯性` 负责。

最近一轮按 `trace-20261014-204822`（2 送达、tips 6、combo 0、收盘挂 5 单）定位并修掉四个 bug，提交 `cf2fc64`→`d644d25`：

- **slack 一个量被当两件事用**（`cf2fc64` 之后的 `53d6a2b`，影响最大）。`eta.slack` 同时承担「优先级」和「可完成性」，而它只适合其中一个。队列累计下队尾单背着全队工时 → slack 最小 → urgency 最高，得出「排得越后越优先」：**94% 的可比候选组（3516/3745）倒挂**。同一个量还让判死退化成排位歧视——order#4 出现 19 拍后即判死，当时 remaining=129/130s（寿命剩 99%），三个鱼单一单未送。现在打分走 `solo_slack`（只看自己的关键路径）、判死走队列 slack 并加 `DOOM_FRESHNESS_FLOOR=0.75`。倒挂 94% → 0%。
- **`time_left == 0` 被当成合法时限**（`ecbbadf`）：插件未开局/已结束都报 0，终局闸在开局就把 20 个候选全判 endgame，白站 147 拍。
- **完成度加分是线性的**（`37a0edc`）：2/3 比 1/3 只多 0.33，正好被 urgency 的组间差 0.35 抵掉。实机 t=1353「差一样黄瓜就能送」的盘输给「往 1/3 的盘加第二样」，这类局面本局 2290 拍。改成 `ratio^2 * W * 3`。
- **总分手写加法**（`cf2fc64`）：16 项重抄一遍，漏掉求和那处的症状是「trace 里有这一项但它不影响选择」而单测全不报警。改 `sum(terms.values())`。

**本轮仍只有离线验证**（`tools/replay_urgency.py` 重放本局 400 帧），实机待下一局。

**改动细节不在本文档维护** —— 问 `git log` 更准。这里只记对下一步工作有约束力的东西。

### 10.1 待办

- **糊锅没人倒**：`Discard.feasible` 要求灶还在烧，所以火灭后的糊锅只能被 `StashHeld` 搁台面，要清空得等同灶再起火，或单独加一个「糊锅进垃圾桶」task。
- **`find_ingredient()` 返回板上的料时缺坐标**：给的是 `workstations[]` 那条，**没有 gx/gz/x/z**，于是 `FetchToPlate`（来源在板上）的 `Approach` 拿不到目标格，立即 FAILURE 并每拍重试。修法：按名字从 `world.stations` 合并坐标。
- **~~移动空转 51%~~ 已不成立，真正的瓶颈是 observe（帧率）**：trace-20261014-204822 的 cruise 2379 拍里 stall ≥0.3s 只有 13 拍（1%），无 ≥30 拍长卡窗口。按 `tools/exec_cost.py` 归因，时间在 `observe` 阶段（占有 task 拍数的 25%，957 拍 / 132 段、中位 7 拍）：每个精确步都要「松键 + 等新鲜帧 + 等停稳」≈ 8 拍 = 0.4 秒，而插件 5 Hz（实测每 3.5 拍才一个新帧）决定了 observe 至少等 4 拍。**杠杆在减少精确步次数或提高插件推送频率，不是调移动参数。** `eta.py` 的 COST_* 常数经实测仍吻合（FetchToPlate 中位 134 vs 常数 125），不用重标。
- **灭火「站位朝向都对却喷不灭」待复核**：`interactions.interactable` 报的是 `m_interactable`（手持物时报手持物），**不是朝向证据**，故未纳入 `interact_ready`。下一局用补全后的按键 trace 再看。
- **全部改动只做过离线验证**（真实帧重放只回答「同样世界状态下会选什么」，不模拟物理执行），实机效果待下一局。
- **判死的新鲜度闸是个下限、不是完整方案**：`DOOM_FRESHNESS_FLOOR` 只保证新单不被排位歧视，但「5 单并发而一双手只够 2~3 单」这个结构性超额仍然存在。更对的做法可能是显式限制并发投入的单数（按 ETA 容量取前 N 单），而不是靠判死被动收口。
- **分层废掉了「提前端盘」的预取信号（已知代价）**：`_time_pressure` 在
  ratio 0.25~0.5 给 0→1.0 的渐变，本意是让 `TakeCookedToPlate` 提前去端盘站位
  （注释记着「等熟了才去拿盘会来不及，实机烧糊」）。分层后这整段落 L7、严格弱于
  任何 L6 备菜候选，到 pressure 1.0 才跳 L2——渐变对层选择零影响。这是词典序的
  固有代价：它表达不了「比备菜略高一点」。`tests/test_layered_selection.py::
  test_prewarm_ramp_lands_in_cleanup_known_tradeoff` 显式钉住了这个行为。
  **真实帧能重放时优先验这个窗口**；若确认烧糊，修法是改 spec §4.2 把
  `TakeCookedToPlate` 纳入 L6，不是调权重。

### 10.2 改权重前必读

**跨层权重不会再互相抵消（分层后），但层内仍会。** 原先的实例：urgency 从
`W_URGENCY/remaining`（中段恒 ~0.1）改成读 slack 后，另一组验证过的七个「灶空转
该填灶」的拍全部回退，测试一个都没报警。那组回退现在由 L5 > L6 结构性排除
（`tests/test_layered_selection.py::test_fill_cooker_outranks_prep_structurally`
钉住）。动层**内**的 `W_*` 时，下面这套流程仍然适用。

动 `W_*` 之后：① 跑 `tests/test_fill_cooker_priority.py` 与 `tests/test_plate_supply_guard.py`（两处量级卡口）；② 用 `tools/replay_urgency.py <trace>` 查 urgency 是否仍单调（倒挂须为 0%）；③ 重放一局真实 trace 比对选择分布。

**打分口径**：urgency 必须读 `ETA.solo_slack_by_order`（独立 slack），**不要**读 `slack_by_order`（队列累计，判死口径）。两者对 remaining 的方向是反的，混用会让「排得越后越优先」，而单组测试查不出来——`tests/test_eta.py::test_queue_slack_is_the_one_that_inverts` 把这个差别钉住了。当前量级关系（从低到高）：备菜加分 < 填灶 `W_COOKER_IDLE=4.8` < 端刚熟的锅 `W_PRESSURE*1.0=5.0`；0 盘时洗盘 `W_PLATE_PRESSURE*2.0=10.0` 压过一切。

**警惕"自欺的修复"**：combo 第一版按 `task.order_id == head` 加分，离线复验看着像修好了（改选了 order#3），但盘里装的菜没变、游戏照样记给同 dish 里 `remaining` 最小的单 —— 实机行为与旧版一字不差，只是 trace 上的 order_id 变好看了。验证改动时要问「实机会不会不一样」，不是「trace 看起来有没有变」。

## 11. 参考文档

- `docs/superpowers/specs/2026-10-04-ai-behavior-architecture-design.md` —— 架构设计（代码注释里的「spec §N」都指它）。
- `docs/superpowers/specs/2026-10-06-e2e-testing-design.md` —— 端到端测试框架设计。
- `docs/superpowers/plans/` —— 各 Plan 的实施计划；`.superpowers/sdd/` 下是对应的 task brief / report / review。
- `docs/baseline-audit.md`、`docs/plan4_delivery_summary.md`、`docs/plan4_soak_report.md` —— 基线核查、Plan 4 交付总结与浸泡报告。
