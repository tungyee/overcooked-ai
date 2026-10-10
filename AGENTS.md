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

```
oc2/
├── ai/                    # 纯库（无 __main__，入口在仓库根 run_agent.py）
│   ├── agent.py           # 主循环：13 步装配（收帧→对账→推断→仲裁→规划→执行→日志）
│   ├── arbiter.py         # 模式仲裁（Normal / Firefighting / Assigned + 卡住守卫）
│   ├── planner.py         # 候选生成→可行性→协作过滤→打分→承诺惯性（约 1300 行，最大文件）
│   ├── executor.py        # task 树生命周期（复用/重建/halt/超时）
│   ├── blackboard.py      # 唯一可变状态容器：Claim/Commitment/P1Model/Intents/scratch
│   ├── p1model.py         # 人类意图推断 + P1 周边 soft cost 地图
│   ├── predicates.py      # 纯查询谓词（无副作用，只读入参 dict）
│   ├── recipes.py         # RecipeBook：配方语义（需求/烹饪/切配/装盘态词汇）
│   ├── world.py           # StateClient（socket 分帧）+ World（快照解析+派生数据）
│   ├── navigation.py      # Dijkstra 全图距离场 + 回溯路径
│   ├── occupancy.py       # 可走性判定 + cookers/items/extinguishers 去重
│   ├── geometry.py        # GridMapping：网格↔世界坐标换算（自动校准原点）
│   ├── stations.py        # 站点检索（按类型/食材/距离）
│   ├── placement.py       # 落点约束（手里东西该放哪、空灶归位）
│   ├── hands.py           # 按键唯一出口：移动 + Tap 去抖/冷却
│   ├── controller.py      # CGEvent 键盘注入（macOS，仅真实运行用）
│   ├── trace.py           # 决策日志（JSONL，落盘仓库根 trace-<时间戳>.jsonl）
│   ├── reconcile.py       # 认领对账（每拍校验并释放失效认领）
│   ├── bb_factory.py      # Blackboard / ResourceView 的构造
│   ├── bt.py              # 行为树引擎（有记忆的 Sequence/Selector）
│   ├── record.py / replay.py  # 帧录制 / 回放
│   ├── primitives/        # 执行原语（行为树叶节点，带强制前后校验）
│   │   ├── base.py        # Primitive 基类 + resolve/target_cell/_blocked_cells 等 target 解析
│   │   ├── move.py        # Approach（走向目标格）、Face（转向目标）
│   │   ├── tap.py         # Tap（按一下）、Confirm（按到目标达成）、Settle（落定）
│   │   ├── hold.py        # HoldUntil（按住直到谓词满足）、TapWhileHolding（持物时按一下）
│   │   ├── interact.py    # InteractWith（走近+按下+后置校验）、EnsureHandsFree（先腾手）
│   │   ├── stuck.py       # MarkBlocked / Nudge / AbandonTarget / Timeout（卡住脱困 + 兜底超时）
│   │   └── __init__.py    # 对外导出（__all__ 列全部 13 个原语）
│   └── tasks/             # 15 个 task（身份/可行性/完成判定/建树）
│       ├── base.py        # Task 基类 + TASK_TYPES 注册 + 跨组共享谓词
│       ├── chop.py        # FetchToBoard、Chop
│       ├── cook.py        # FetchToCooker、TakeCookedToPlate、ReturnPot、RescueCooked
│       ├── plate.py       # PlaceCleanPlate、WashPlate、ResetPlate
│       ├── assembly.py    # FetchToPlate、DeliverPlate + delivery_baseline_from
│       ├── cleanup.py     # Discard、RecoverLoosePot、StashHeld、Prefetch
│       └── __init__.py    # import * 触发 @_register，对外唯一入口
├── plugin/                # C# BepInEx 插件（数据源）：Oc2AIPlugin.cs + build.sh
├── tests/                 # pytest 测试（真实帧 fixture 已删，待重录）
├── tools/                 # 开发工具（非运行时）：tree_viz / record_frames / dll_probe
├── docs/                  # 交付总结、浸泡报告、基线核查、SDD spec 与 plan
├── .superpowers/          # SDD（Subagent Driven Development）任务 brief/report/review
├── run_agent.py           # 唯一入口（根级，ai/ 不放 __main__）
├── pytest.ini             # testpaths=tests, pythonpath=.
└── requirements-dev.txt   # 仅 pytest==7.4.4
```

## 4. 核心架构

三层 + 黑板（spec §3）：

```
World 快照 → 认领对账 → 更新 P1Model → Arbiter 选模式
  → Normal 则 Planner 选 task，否则取中断 task → Executor tick 成按键 → 输出 trace
```

- **Arbiter**（`arbiter.py`）：每拍先跑，只决定「谁拿控制权」，不执行动作。返回 `(mode, reason)`。优先级：卡住守卫 → 中断 task 连续失败降级 → Firefighting（灶着火且 P1 未在处理）→ Assigned（对话层指派，当前为空壳）→ Normal。Arbiter 只读黑板，模式写入由调用方（Agent）负责。**卡住守卫（同一格连续 N 拍）目前只记 `stuck detected` 日志、不触发脱困动作**——实际脱困靠两层：`HoldUntil` 的位置自愈（见 §6.2）+ Agent 连续失败计数写 `scratch.abandoned`（见 §6.2 `AbandonTarget` 行）。
- **Planner**（`planner.py`）：纯函数步进器。`select_task(world, bb, order, book)` 走：生成候选（枚举所有订单，Plan 4 多订单模式）→ 可行性过滤（`claims_available` 前置 + `_is_abandoned` 拉黑检查 + 各 task 的 `feasible()`）→ 协作过滤（relay/split）→ 打分（10 项权重）→ 承诺惯性（防抖 + 穿插）。**无状态每拍重算**，执行顺序从可行性涌现，不建 DAG、不拓扑排序。
- **Executor**（`executor.py`）：把 task 换成一棵小的 `bt` 行为树，每拍 tick 产出按键。task 身份（四元组，见 §6）不变就复用同一棵树（进度自然保留），变了就 halt 旧树重建；每棵树包一层 `Timeout`（`MAX_TASK_TICKS=300` 拍）做最后兜底。
- **Blackboard**（`blackboard.py`）：唯一可变状态容器，取代旧 Agent 上散落的约 20 个跨拍字段。写权受控（spec §4.5）：Planner 写 `claims`/`commitment`，P1Model 写 `p1_model`，Executor 只写 `commitment.steps_done/retries` 与 `scratch`（**不可写 claims**）。
- **World**（`world.py`）：一帧快照的**只读**视图，构造时只做解析 + 派生数据装配；可走性与寻路分别委托 `occupancy` 与 `navigation`。
- **Trace**（`trace.py`）：每拍写一条 JSONL 记录（模式/候选/打分/选中 task/执行状态/按键），落盘仓库根 `trace-<时间戳>.jsonl`（已 gitignore），用于复盘「为什么选这个动作」。

## 5. 常用命令

Python 侧没有构建步骤；C# 插件需单独编译（`./plugin/build.sh`）。Python 命令在仓库根执行。实机的启动与退出顺序见 §5.1、§5.2。

### 5.1 启动顺序（游戏 → Agent）

插件**不需要单独启动**：它是 BepInEx 插件（`[BepInPlugin]` + `BaseUnityPlugin`），游戏经 `run_game.sh` 启动时 BepInEx 自动加载 `BepInEx/plugins/` 下的所有 DLL，插件随即在 `Awake()` 里监听 `127.0.0.1:27411`。真正要手动的只有两步：**起游戏** → **起 Agent**（Agent 启动时连 27411，连不上直接退出：`run_agent.py` 抛 `OSError` → 退出码 1）。

```bash
G="$HOME/Library/Application Support/Steam/steamapps/common/Overcooked! 2"

# 可选前置：仅当 plugin/Oc2AIPlugin.cs 有改动时，重新编译 + 部署（不改插件可跳过）
./plugin/build.sh
cp plugin/build/Oc2AIPlugin.dll "$G/BepInEx/plugins/Oc2AIPlugin.dll"   # 部署后必须重启游戏才生效

# ① 启动游戏 —— 必须走 run_game.sh（顺带注入 BepInEx 并自动加载插件）
"$G/run_game.sh"        # 游戏内进入关卡 Sushi_1_4_2P

# ② 启动 Agent（在仓库根执行）
python3 run_agent.py    # 连 127.0.0.1:27411 并注入 P2 按键
```

- **插件随游戏自动加载，没有单独的「启动插件」命令**；唯一要手动做的是改插件源码后重新编译并部署 DLL（上面的可选前置），且必须重启游戏才生效。`build.sh` 末尾会打印这条 `cp` 命令。
- ① **必须用 `run_game.sh`**（脚本内部等价于 `DOORSTOP_ENABLED=1` + `DYLD_INSERT_LIBRARIES=libdoorstop.dylib` 启动游戏二进制）。**从 Steam 直接启动不会注入 BepInEx**，插件与 socket 都不加载。插件在游戏启动时即监听 27411（早于进关卡），但 Agent 要等进关卡、收到 `level` 帧后才真正决策。
- ② 要求运行终端已获 macOS「辅助功能」权限（CGEvent 注入）；只想看决策、不注入按键时用 `python3 run_agent.py --dry-run`。

### 5.2 退出顺序（Agent → 游戏）

先停 Agent，再退游戏。Agent 收到 `Ctrl-C` / `SIGTERM` 会先 `hands.release_all()` 松开所有按键再退出（防卡键），所以**务必先停 Agent 再退出游戏**，避免残留按住的方向键/动作键。

```bash
# ① 停 Agent：在其终端 Ctrl-C，或 kill -TERM <pid>（会先松键再退出）
# ② 退出游戏：正常退出即可
```

插件是游戏进程内的 DLL，**没有独立进程/服务可停**：它随游戏启动而加载、随游戏退出而卸载。要停用/更新插件就是替换 `$G/BepInEx/plugins/Oc2AIPlugin.dll` 后重启游戏（或直接退出游戏）。

### 5.3 其他常用命令

```bash
# 跑全部测试（当前 708 passed, 2 skipped）
python3 -m pytest tests/

# 跑单个测试文件 / 单个用例
python3 -m pytest tests/test_planner_multi.py
python3 -m pytest tests/test_planner_multi.py::test_xxx -q

# 正式运行：连游戏（插件已在 127.0.0.1:27411 推帧）+ 注入按键
# 前提：macOS，终端已获「辅助功能」权限，游戏在 Sushi_1_4_2P 关卡内
python3 run_agent.py

# 离线决策：不注入按键（NullController），只看决策
python3 run_agent.py --dry-run

# 离线冒烟：从一帧构建全部 task 树、打印 ASCII、走分支、不按键
# （旧帧 fixture 已删，待 tools/record_frames.py 重录后指向新帧）
python3 tools/tree_viz.py <重录后的帧文件>

# 录帧 / .NET 元数据探针
python3 tools/record_frames.py            # 录制实时帧到 NDJSON
python3 tools/dll_probe.py <Assembly-CSharp.dll> <TypeName>...

# 编译 C# 插件（产物 plugin/build/Oc2AIPlugin.dll；拷到游戏 BepInEx/plugins/ 后重启游戏生效）
# 换机器可用环境变量覆盖：GAME_DIR（游戏安装目录）、DOTNET（dotnet 可执行文件）、CSC_DLL（Roslyn csc.dll 路径）
./plugin/build.sh
```

## 6. 代码组织与关键约定

### 6.1 task 的契约与身份

`ai/tasks/base.py` 定义 `Task` 基类。**task 实例 = 四元身份 `(TYPE, order_id, bound_resources, bound_item)`**，四项全同才是同一个 task；Task 是不可变值对象（`__slots__` + 禁 `__setattr__`），可作 dict 键/set 元素。每个 task 实现四个契约：

- `claims()` → 需要的 `(resource_id, action)` 认领键
- `feasible(ctx)` → 「此刻我可不可做」（按绑定参数求值）
- `done(ctx)` → 完成判定（即下游 task 的 `feasible`，上下游咬合）
- `build()` → 构建行为树（由 Executor 持有，不进黑板）

`ai/tasks/__init__.py` 是对外唯一入口：一律 `from ai import tasks as T`，**不要直接 import 子模块**。子模块按 spec 的「单文件 ≤ 400 行」硬指标分组（plate / cook / chop / assembly / cleanup）。`T.TASK_TYPES` 登记全部 15 个类型，靠 `__init__.py` 的 `import *` 触发 `@_register` 才能完整；`tests/test_tasks.py::test_task_types_registry_covers_all_subclasses` 用计数钉住完整性。

15 个 task 类型：

| 模块 | task |
|---|---|
| `chop.py` | `FetchToBoard`、`Chop` |
| `cook.py` | `FetchToCooker`、`TakeCookedToPlate`、`ReturnPot`、`RescueCooked` |
| `plate.py` | `PlaceCleanPlate`、`WashPlate`、`ResetPlate` |
| `assembly.py` | `FetchToPlate`、`DeliverPlate` |
| `cleanup.py` | `Discard`、`RecoverLoosePot`、`StashHeld`、`Prefetch` |

### 6.2 执行原语（`ai/primitives/`）

树的叶子是带强制前后校验的原语（spec §7.2）。ctx 契约（`primitives/base.py`）为 `{w, chef, hands, bb, field, book, tick}`；target 优先传「可调用」以便每拍重算（树跨拍复用，构造时抓的 dict 第二拍就过期）。halt 只松键、不碰物品（优雅退出由抢占方树首的 `EnsureHandsFree` 负责）。

13 个原语的语义：

| 原语 | 作用 |
|---|---|
| `Approach` | 锁定目标相邻交互位，近处 50 ms 短按后等新帧确认停稳；站到目标交互位并落定才成功。**只保证站位、不保证朝向**，定向交互须另配 `Face`。 |
| `Face` | 转向 target（以 `interactions.gx/gz` 为准），短按方向键后等新帧判断朝向；`max_ticks` 仅兜底，不作为「转好了」的判据。 |
| `Tap` | 单击一次，**边沿触发**（同一次激活期内只按一次，避免 toggle 反复拿起放下）；带 target 时按键前先校验 `interact_ready`。 |
| `Confirm` | 后置校验：`pred(ctx)->bool` 成立才算成功，否则先 RUNNING 等状态刷新；正式运行按新快照消耗 `retries`，无新帧也有实际时间上限。离线无收帧元数据时仍按调用次数计。 |
| `Settle` | 等 N 拍让状态刷新（只用于等状态，不等物理稳定——后者已由 `Approach` 的前置校验取代）。 |
| `HoldUntil` | 按住 key 直到 `pred` 成立（切菜/喷灭火器/洗盘）；`timeout` 无默认值必须显式传，halt 时 release 防卡键。可选 `target`/`kind`：传了则每拍校验 `interact_ready`，人被推离目标时松键 FAILURE 让 Sequence 重置重新 Approach（Chop / WashPlate 用，防传送带推走空按卡死）。 |
| `TapWhileHolding` | 按住反向键的同时单击（传送带顶位专用，防被推走）；同样边沿触发。 |
| `EnsureHandsFree` | 手里有东西就放到就近合法落点（**幂等**：手空直接 SUCCESS）；Approach→Face→Tap→Confirm 四阶段，落点物料感知（空锅回灶、熟锅不上灶）。 |
| `InteractWith` | 复合原语 = Approach + (Face) + Tap + Confirm；`expect` 后置校验必传（漏给即 TypeError），替代旧 22 处 goto→tap→confirm 模板。 |
| `MarkBlocked` | L1 脱困（**预留，未接线**）：把格子拉黑写 `scratch.bad_cells`，Approach 重算距离场绕开。 |
| `Nudge` | L2 脱困（**预留，未接线**）：侧向微移 N 拍，脱离贴墙/离格。 |
| `AbandonTarget` | L3 脱困原语（**预留，未接线**）。实际 L3 由 Agent 的连续失败计数实现：同一 task 连续失败满 `ABANDON_FAIL_STREAK=3` 次 → 写 `scratch.abandoned`，Planner 的 `filter_feasible` 读它跳过该候选（TTL `ABANDON_TTL_TICKS=200` 拍）。 |
| `Timeout` | task 级超时装饰（结构性安全网），超时 halt 子树转 FAILURE，交还 Planner。 |

移动控制另使用 `frame_seq/frame_received_at/now` 区分新观测与循环拍，短按和观察
窗口按单调时钟计时。`InteractWith` 记忆执行阶段，`Controller.tap` 非阻塞并定时松键。
Normal 模式的移动无进展还会通过 Agent 写 `scratch.bad_cells/abandoned`，按位置和资源
短暂避让（独立于上表中预留的脱困节点）。详见
`docs/movement_control_fix_20261010.md`（792 passed, 35 skipped；实机手感待下一局复核）。

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
- 键位沿袭 legacy 实机验证的 P2 分屏键位（`ai/controller.py` 的 `KEY` 表）。

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
- **打分权重未调优**：`planner.py` 的 `W_*` 常量为经验初值；穿插阈值（`INTERLEAVE_THRESHOLD/PENALTY`）硬编码。
- 文档（`docs/plan4_delivery_summary.md`）里提到的「16 个 task」与代码实际不符——`TASK_TYPES` 当前登记 **15** 个类型，以代码为准。
- 对话协作层（`Intents.assignments`、split mode 的显式分单）数据结构已预留但未实装语言解析。

## 9. 安全注意事项

- `run_agent.py` 真实运行会向游戏窗口注入全局键盘事件：需要 macOS「辅助功能」权限；`Controller` 构造时会先 `release_all()` 并尝试 `osascript` 把前台切到 Overcooked2 进程，避免键进错窗口。离线验证请用 `--dry-run` 或 `tools/tree_viz.py`（不注入按键）。
- 状态端口 `127.0.0.1:27411` 仅本机；项目不涉及凭据/密钥，`.gitignore` 已排除运行时产物（`trace-*.jsonl`、`frames-*.jsonl`、未压缩 fixture 帧、`*.log`、`__pycache__`、`.venv`）。

## 10. 当前状态

- 分支 `main`；最近一轮是「实机修复」（trace 落盘/心跳、落点物料感知、两段式判据改按锅内容、承诺被抢吞掉成功记账、观察式盘↔单归属、完成度读错字段等），全量测试 **702 passed, 35 skipped**（部分真实帧 fixture 已删，相关回归转为 skip）。
- 本轮把 C# 插件从 `../oc/src/Oc2AIPlugin.cs` 迁入 `plugin/`，解除「不改插件」冻结，并修掉「切关卡不刷新缓存」的 bug（新增 `RefreshLevel()`：检测 `ReadLevel()` 变化时重置 `_level`/`_staticJson`），已 `./plugin/build.sh` 编译通过。
- 本轮还修了「P1 行动打断 P2 → 手里端着盘子卡死」：`FetchToBoard` / `FetchToPlate` / `DeliverPlate` 树首加 `EnsureHandsFree(keep=…)` 并声明 `HANDS="empty"`（spec §6.1(b)/§7.4）；`EnsureHandsFree` 新增 `keep` 谓词（手里已是目标物就跳过放下）；`find_ingredient` 绑定到 P2（P1 手里的料不再误判为 available）；`DeliverPlate` 的 pickup 判据从 `held_is_plate` 收紧为 `held_matches(plate_id)`。全量测试通过。
- 已知未修完的一条：`find_ingredient()` 返回"板上的料"时给的是 `workstations[]` 那条，**没有 gx/gz/x/z**，于是 `FetchToPlate`（来源在板上）的 `Approach` 拿不到目标格，立即 FAILURE 并每拍重试。修法：按名字从 `world.stations` 合并坐标进去。

## 11. 参考文档

- `docs/superpowers/specs/2026-10-04-ai-behavior-architecture-design.md` —— 架构设计（代码注释里的「spec §N」都指它）。
- `docs/superpowers/specs/2026-10-06-e2e-testing-design.md` —— 端到端测试框架设计。
- `docs/superpowers/plans/` —— 各 Plan 的实施计划；`.superpowers/sdd/` 下是对应的 task brief / report / review。
- `docs/baseline-audit.md`、`docs/plan4_delivery_summary.md`、`docs/plan4_soak_report.md` —— 基线核查、Plan 4 交付总结与浸泡报告。
