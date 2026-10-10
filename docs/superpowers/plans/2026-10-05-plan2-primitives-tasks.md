# Plan 2：原语 + task 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付执行层的两块砖——`primitives.py`（11 原语 + `InteractWith` + `Timeout`）与 `tasks.py`（16 个 task 的身份、可行性、完成判定、树构建），以及它们依赖的配方语义与落点约束，使 `tree_viz.py` 能离线构建任意 task 的树、用假状态走分支，且**一个按键都不注入**。

**Architecture:** 按键输出收口到一个注入式的 `Hands` 包装层（持住跨拍按住的方向、Tap 去抖与冷却），原语只调 `ctx["hands"]`，所以离线测试注入一个记录调用的假 controller 就能断言键序列。`tasks.py` 里每个 task 是**不可变值对象**（`(TYPE, order_id, resources, item)` 四元身份，spec §6.0），自带 `claims()` / `feasible(ctx)` / `done(ctx)` / `build()` 四个方法；可行性与完成判定都是**按绑定参数求值的纯谓词**，上游 task 的 `done` 即下游 task 的 `feasible`（spec §6.1 上下游咬合）。Planner 不在本计划内——本计划交付的是它要挑选的东西和它们各自声明的可做条件。

**Tech Stack:** Python 3.9.6、pytest。无第三方依赖（`Quartz` 只在真实 `controller.py` 里，本计划的测试一律注入假 controller，不导入它）。

**Spec:** `docs/superpowers/specs/2026-10-04-ai-behavior-architecture-design.md`（§6.0 task 身份、§6.1 候选 task 全集、§7 Executor 全章、§9 锅具识别、§11.2 阶段 3、§11.2.1 计划映射与"已知缝隙"表）

## Global Constraints

- **Python 3.9.6**：不可用 `match`、不可用 `list[int]` / `dict[str, int]` 式内置泛型下标。类型标注一律用注释形式 `# type: (...) -> ...` 并从 `typing` 导入 `Dict` / `List` / `Set` / `Tuple` / `Optional` / `Callable` / `Any`。
- **不改 `oc/`**：旧实现是行为对照组，只读参考，一个字节都不许动。
- **不改 C# 插件**：帧格式按现状适配（spec §4 行 4）。
- **不注入按键**：本计划交付的代码不得在测试里导入 `Quartz` 或真实 `Controller`。`primitives.py` 只通过 `ctx["hands"]` 输出，`Hands` 构造时注入 controller。
- **不接运行时决策路径**：`oc/ai/agent.py` 照旧能跑，`oc2/ai/` 下不出现主循环（归 Plan 3）。
- **注释用中文**，与 `oc2/ai/` 既有模块一致。每个新模块头部 docstring 说明"与 `oc/ai/` 旧实现的差异及其理由"——这是该目录已建立的惯例。
- **无单个文件 > 400 行**（spec §11.5 唯一的代码硬指标之一）。16 个 task 的实现合计 1100 行上下，故 `ai/tasks` 是**包而非单文件**：`base.py` 放身份与基类，另五个子模块各放一组 task，`__init__.py` 导入全部并 re-export。调用方一律 `from ai import tasks as T`，不直接 import 子模块——子模块边界是实现细节，`TASK_TYPES` 的完整性由 `__init__.py` 的导入保证（见 Task 12）。
- **测试命令**：`cd /Users/dongyi/Documents/coding/oc2 && python3 -m pytest <path> -v`
- **分支**：本计划在 `plan2-primitives-tasks` 分支上做，不直接提交 main。

## Review Focus

spec 没点明但会咬到人的七类输入，每条都已挂到对应任务的测试里。

**第 0 条是写计划时在实测帧上发现的，它推翻了 spec §6.1 的一处字面表述**，故列在最前：

0. **盘里的料名与订单需求名是两套词汇**。801 帧实测：订单 `composition` 写 `['Seaweed','BoiledSushiRice','SushiFish']`，而做完的盘 `contents` 是 `['CookedCompositeAssembledNode','SushiFish','Seaweed']`——熟料变成不透明 token，需切的料回落到生料名（不是 `chop_target`）。spec §6.1 的"盘 `contents` 含该熟料"按字面实现必然死循环：`plate_lacks(plate,"BoiledSushiRice")` 恒为真 → `TakeCookedToPlate.done` 永不成立 → AI 把饭倒进盘后以为没倒，无限重倒。修法是 `RecipeBook.plate_token` 这一层装盘态映射 → Task 5 Step 1（`test_missing_complete_plate_uses_real_frame_vocabulary` / `test_plate_token_rules` / 真实帧全扫 `test_plate_has_on_real_frames`），消费方在 Task 13/14/16 各有断言。

1. **`Tap` 被同一棵树反复 tick**。`bt.Sequence` 每拍从第 0 个子节点重新 tick（`oc/ai/bt.py:38`），已 SUCCESS 的子节点下一拍照旧被 tick 一遍。而拾取是 toggle（`oc/ai/agent.py:921` 注释），连点等于"拿起又放下"。`Tap` 必须边沿触发：一次激活期内只按一次 → Task 8 Step 1（`test_tap_does_not_repress_on_retick`）。
2. **`HoldUntil` 被抢占时没松键**。切菜/喷灭火器要按住多拍，中断（火情、脱困）来时 `halt()` 若不 release，按键会卡死到进程退出——这是本项目已踩过的坑（`AI_REVIEW.md` Bug#2 加 `halt()` 的由来）→ Task 9 Step 1（`test_hold_until_halt_releases_key`）与 Step 5（`grep -n "def halt"` 卡口）。
3. **完成判定没绑定具体资源**。两单都缺黄瓜时，P1 把黄瓜放进盘 B，若 `FetchToPlate.done` 不绑定盘，针对盘 A 的 task 会误报 SUCCESS，盘 A 其实还空着（spec §6.0 原话）→ Task 16 Step 1（`test_fetch_to_plate_done_binds_specific_plate`）。
4. **切配产物不以 `Chopped` 开头 / 需切多轮**。`Chop.done` 必须读 `crate.chop`（实测 `{"ing":"Cucumber","chop":"ChoppedCucumber","chop_stages":8}`）而非字符串前缀匹配，否则换关卡静默误判"已切好"→ Task 5 Step 5 + Task 15 Step 1（`test_chop_done_with_non_chopped_prefix_product`）与 Step 5（grep 卡口）。
5. **目标在动作中途消失**。锅被 P1 端走、盘被端走、灭火器被捡走——原语的 target 解析必须退化为 FAILURE 交还 Planner（spec §7.6 纪律 1），不能 `NoneType has no attribute 'get'` 崩掉主循环 → Task 7 Step 1（`test_approach_target_gone_is_failure` / `test_face_target_gone_is_failure`）。
6. **手里的东西被意外换掉**。spec §7.5 列了这一类（"误放下 / 误拾 → 手部连续性不变式"）但没说不变式放哪，初稿漏掉了它。被 P1 塞过来、传送带冲来一块料、误拾——若不检出，`InteractWith` 会把这个意外物品放进盘里。落点是 `Primitive.hands_changed`，在 `InteractWith` 按键**前**查（按键后手里本该变，那是 `expect` 的职责）→ Task 10 Step 1（`test_interact_with_fails_when_hands_change_unexpectedly` 及其两条边界：按键后容忍、首拍不误判）。

---

## 文件结构

| 文件 | 职责 | 产出任务 | 预估行数 |
|---|---|---|---|
| `ai/bt.py` | BT 引擎：`Sequence` / `Selector` / `Action` / `Condition` / `halt`（**删除未接入的 `Parallel`**，spec §7.7） | Task 1 | ~110 |
| `ai/hands.py` | 按键输出包装：跨拍按住的移动方向、`Tap` 去抖与冷却、`release_all`。controller 注入 | Task 2 | ~110 |
| `ai/navigation.py` | **修改**：加 `trace_back` / `first_step`（spec §11.2.1 记为 Plan 2 的"已知缝隙"） | Task 3 | +60 |
| `ai/predicates.py` | **修改**：加 `interact_ready`（交互前置校验）、`held_is_cookware`（§9 锅具识别数据驱动） | Task 4 | +90 |
| `ai/recipes.py` | 配方语义：`order_requirements` / `cooked_source` / `chop_target` / `find_ingredient` | Task 5 | ~140 |
| `ai/placement.py` | 落点约束（spec §6.1）：空台面优先、地板兜底 | Task 6 | ~110 |
| `ai/primitives.py` | 11 原语 + `InteractWith` + `Timeout` | Task 7-11 | ~380 |
| `ai/tasks/__init__.py` | 包门面：导入全部子模块并 re-export `Task` / `TASK_TYPES` / 共享谓词。调用方一律 `from ai import tasks as T` | Task 12 | ~40 |
| `ai/tasks/base.py` | `Task` 基类与四元身份、`claims_available`、`_register` / `TASK_TYPES`、跨组共享谓词 | Task 12 | ~200 |
| `ai/tasks/plate.py` | `PlaceCleanPlate` / `WashPlate` / `ResetPlate` + 盘相关谓词 | Task 13 | ~270 |
| `ai/tasks/cook.py` | `FetchToCooker` / `TakeCookedToPlate` / `ReturnPot` / `RescueCooked` | Task 14 | ~300 |
| `ai/tasks/chop.py` | `FetchToBoard` / `Chop` + 板相关谓词 | Task 15 | ~130 |
| `ai/tasks/assembly.py` | `FetchToPlate` / `DeliverPlate` | Task 16 | ~170 |
| `ai/tasks/cleanup.py` | `Discard` / `RecoverLoosePot` / `StashHeld` / `Prefetch` | Task 17 | ~210 |
| `tools/tree_viz.py` | 离线冒烟：打印树、假状态走分支、零按键注入 | Task 18 | ~140 |
| `tests/test_bt.py` | BT 语义（含重复 tick 与 halt 传播） | Task 1 | |
| `tests/test_hands.py` | 按键包装层单测 | Task 2 | |
| `tests/test_navigation.py` | **修改**：`trace_back` 的非对称代价回溯 | Task 3 | |
| `tests/test_predicates.py` | **修改**：`interact_ready` / `held_is_cookware` | Task 4 | |
| `tests/test_recipes.py` | 配方语义单测 | Task 5 | |
| `tests/test_placement.py` | 落点约束单测 | Task 6 | |
| `tests/test_primitives.py` | 原语单测（假 controller，零按键注入） | Task 7-11 | |
| `tests/test_tasks.py` | task 身份 / 可行性 / 完成判定 / 树结构。**不随实现拆分**——单一测试文件按 task 组分节，共享 `FakeWorld` / `make_ctx` 等脚手架 | Task 12-17 | |
| `tests/test_tree_viz.py` | 冒烟脚本可跑且不碰 `Quartz` | Task 18 | |

依赖顺序：Task 1（BT）→ 2（Hands）→ 3（寻路回溯）→ 4（谓词）→ 5（配方）→ 6（落点）→ 7-11（原语，用 1-6）→ 12（Task 基类）→ 13-17（16 个 task，用 7-11）→ 18（冒烟，用全部）。

---

### Task 1: BT 引擎移植（删 `Parallel`）

**Files:**
- Create: `ai/bt.py`
- Test: `tests/test_bt.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `Status.SUCCESS` / `Status.FAILURE` / `Status.RUNNING`（字符串常量 `"success"` / `"failure"` / `"running"`）
  - `Node`（基类，`tick(ctx)` 抛 `NotImplementedError`、`halt()` 空实现）
  - `Sequence(children)` / `Selector(children)` / `Action(fn)` / `Condition(fn)`

从 `oc/ai/bt.py` 整体移植，删掉 `Parallel`（spec §7.7：从未接入，`AI_REVIEW.md` 优化项 1 自承"多厨师预留，未接入"）。语义一字不改——它是本项目唯一被实机验证过的控制流引擎。

- [ ] **Step 1: 写失败测试**

创建 `tests/test_bt.py`：

```python
"""BT 引擎语义。

重点钉两件事：
  1. Sequence 每拍从头 tick，已 SUCCESS 的子节点会被重复 tick
     （这是 Tap 必须边沿触发的根因，见 Review Focus 1）。
  2. halt 沿 RUNNING 链向下传播（按键释放依赖它）。
"""
from ai import bt
from ai.bt import Status


class Rec(bt.Node):
    """记录 tick / halt 调用次数的探针节点。"""

    def __init__(self, statuses):
        # statuses: 每次 tick 依次返回的状态列表，耗尽后重复最后一个
        self.statuses = list(statuses)
        self.ticks = 0
        self.halts = 0

    def tick(self, ctx):
        i = min(self.ticks, len(self.statuses) - 1)
        self.ticks += 1
        return self.statuses[i]

    def halt(self):
        self.halts += 1


def test_sequence_all_success():
    a, b = Rec([Status.SUCCESS]), Rec([Status.SUCCESS])
    assert bt.Sequence([a, b]).tick({}) == Status.SUCCESS
    assert (a.ticks, b.ticks) == (1, 1)


def test_sequence_stops_at_failure():
    a, b = Rec([Status.FAILURE]), Rec([Status.SUCCESS])
    assert bt.Sequence([a, b]).tick({}) == Status.FAILURE
    assert b.ticks == 0, "FAILURE 后不该继续 tick 右边的兄弟"


def test_sequence_stops_at_running():
    a, b = Rec([Status.RUNNING]), Rec([Status.SUCCESS])
    assert bt.Sequence([a, b]).tick({}) == Status.RUNNING
    assert b.ticks == 0


def test_sequence_reticks_completed_children_next_tick():
    """Review Focus 1 的根因：Sequence 不记"做到第几个"。

    第一拍 a 返回 SUCCESS、b 返回 RUNNING；第二拍 a 会被再 tick 一次。
    若 a 是一个会按键的 Tap，这就是"拿起又放下"。所以 Tap 必须自己
    边沿触发，不能指望引擎记住。
    """
    a = Rec([Status.SUCCESS])
    b = Rec([Status.RUNNING])
    seq = bt.Sequence([a, b])
    seq.tick({})
    seq.tick({})
    assert a.ticks == 2
    assert b.ticks == 2


def test_sequence_halt_propagates_to_running_child():
    a = Rec([Status.RUNNING])
    seq = bt.Sequence([a])
    seq.tick({})
    seq.halt()
    assert a.halts == 1


def test_sequence_halts_previous_running_child_on_switch():
    """RUNNING 从 a 转到 b 时，a 必须被 halt（否则 a 的按键留着）。"""
    a = Rec([Status.RUNNING, Status.SUCCESS])
    b = Rec([Status.RUNNING])
    seq = bt.Sequence([a, b])
    seq.tick({})     # a RUNNING
    seq.tick({})     # a SUCCESS -> b RUNNING
    assert b.ticks == 1
    seq.halt()
    assert b.halts == 1


def test_sequence_halts_running_child_when_later_sibling_fails():
    a = Rec([Status.RUNNING, Status.SUCCESS])
    b = Rec([Status.FAILURE])
    seq = bt.Sequence([a, b])
    seq.tick({})
    assert seq.tick({}) == Status.FAILURE
    assert a.halts == 1, "后续兄弟 FAILURE 时，之前 RUNNING 的子节点要收尾"


def test_selector_returns_first_non_failure():
    a, b = Rec([Status.FAILURE]), Rec([Status.SUCCESS])
    assert bt.Selector([a, b]).tick({}) == Status.SUCCESS
    assert (a.ticks, b.ticks) == (1, 1)


def test_selector_all_failure():
    a, b = Rec([Status.FAILURE]), Rec([Status.FAILURE])
    assert bt.Selector([a, b]).tick({}) == Status.FAILURE


def test_selector_halt_propagates():
    a = Rec([Status.RUNNING])
    sel = bt.Selector([a])
    sel.tick({})
    sel.halt()
    assert a.halts == 1


def test_action_and_condition():
    assert bt.Action(lambda ctx: Status.SUCCESS).tick({}) == Status.SUCCESS
    assert bt.Condition(lambda ctx: True).tick({}) == Status.SUCCESS
    assert bt.Condition(lambda ctx: False).tick({}) == Status.FAILURE


def test_condition_receives_ctx():
    seen = []
    bt.Condition(lambda ctx: seen.append(ctx) or True).tick({"w": 1})
    assert seen == [{"w": 1}]


def test_parallel_is_gone():
    """spec §7.7：Parallel 从未接入，精简掉。

    留着一个没人用、没测试覆盖的并行节点，日后会有人以为它能用。
    """
    assert not hasattr(bt, "Parallel")
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `python3 -m pytest tests/test_bt.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'ai.bt'`

- [ ] **Step 3: 实现 `ai/bt.py`**

```python
"""行为树引擎：Node / Sequence / Selector / Action / Condition，支持 RUNNING 与 halt。

从 oc/ai/bt.py 移植，语义一字不改——它是本项目唯一被实机验证过的控制流
引擎。唯一改动：**删除 Parallel**（spec §7.7）。Parallel 从未被接入过
（AI_REVIEW.md 优化项 1 自承"多厨师预留，未接入"），而新架构里树只负责
单个 task 的 2-4 步线性顺序，更不会用到并行。

语义：
- Sequence：从左到右依次 tick；子节点返回 FAILURE 或 RUNNING 时立即停止并原样返回。
- Selector：从左到右依次 tick；子节点返回 SUCCESS 或 RUNNING 时立即停止并原样返回。
- Action 执行多拍时返回 RUNNING，验证完成返回 SUCCESS，无法继续返回 FAILURE。
- Condition 只返回 SUCCESS / FAILURE，不阻塞。
- halt：RUNNING 子节点被抢占/放弃时，引擎调用其 halt() 释放按键。

⚠️ Sequence **不记录"做到第几个"**：每拍都从第 0 个子节点重新 tick，已
SUCCESS 的子节点下一拍照旧被 tick 一遍。而游戏里拾取是 toggle，连点等于
"拿起又放下"。所以按键原语必须自己边沿触发（见 primitives.Tap），不能
指望引擎记住进度。
"""


class Status:
    SUCCESS = "success"
    FAILURE = "failure"
    RUNNING = "running"


class Node(object):
    def tick(self, ctx):
        raise NotImplementedError

    def halt(self):
        pass


class Sequence(Node):
    def __init__(self, children):
        self.children = children
        self._running = None

    def tick(self, ctx):
        for c in self.children:
            s = c.tick(ctx)
            if s == Status.RUNNING:
                if self._running is not None and self._running is not c:
                    self._running.halt()
                self._running = c
                return Status.RUNNING
            if self._running is c:
                self._running = None
            if s != Status.SUCCESS:
                if self._running is not None:
                    self._running.halt()
                    self._running = None
                return s
        if self._running is not None:
            self._running.halt()
            self._running = None
        return Status.SUCCESS

    def halt(self):
        if self._running is not None:
            self._running.halt()
            self._running = None


class Selector(Node):
    def __init__(self, children):
        self.children = children
        self._running = None

    def tick(self, ctx):
        for c in self.children:
            s = c.tick(ctx)
            if s == Status.RUNNING:
                if self._running is not None and self._running is not c:
                    self._running.halt()
                self._running = c
                return Status.RUNNING
            if self._running is c:
                self._running = None
            if s != Status.FAILURE:
                if self._running is not None:
                    self._running.halt()
                    self._running = None
                return s
        if self._running is not None:
            self._running.halt()
            self._running = None
        return Status.FAILURE

    def halt(self):
        if self._running is not None:
            self._running.halt()
            self._running = None


class Action(Node):
    def __init__(self, fn):
        self.fn = fn

    def tick(self, ctx):
        return self.fn(ctx)


class Condition(Node):
    def __init__(self, fn):
        self.fn = fn

    def tick(self, ctx):
        return Status.SUCCESS if self.fn(ctx) else Status.FAILURE
```

- [ ] **Step 4: 运行测试，确认通过**

Run: `python3 -m pytest tests/test_bt.py -v`
Expected: PASS，13 个测试全绿。

- [ ] **Step 5: 运行全量测试**

Run: `python3 -m pytest -v`
Expected: 全绿（Plan 1/1.5 的既有测试不受影响）。

- [ ] **Step 6: 提交**

```bash
git add ai/bt.py tests/test_bt.py
git commit -m "feat(bt): BT 引擎移植，删除未接入的 Parallel

语义一字不改——oc/ai/bt.py 是本项目唯一被实机验证过的控制流引擎。
删 Parallel 的依据是 spec §7.7：它从未被接入（AI_REVIEW.md 优化项 1
自承"多厨师预留，未接入"），而新架构里树只负责单个 task 的 2-4 步
线性顺序。留着一个没人用、无测试覆盖的并行节点，日后会有人以为它能用。

test_sequence_reticks_completed_children_next_tick 钉住一条会咬人的
语义：Sequence 不记"做到第几个"，已 SUCCESS 的子节点下一拍照旧被
tick。游戏里拾取是 toggle，这就是 Tap 必须边沿触发的根因。

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: `Hands` 按键输出层

**Files:**
- Create: `ai/hands.py`
- Test: `tests/test_hands.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `DIR2D` = `{"left": (-1, 0), "right": (1, 0), "up": (0, 1), "down": (0, -1)}`
  - `OPPOSITE` = `{"left": "right", "right": "left", "up": "down", "down": "up"}`
  - `TAP_COOLDOWN_TICKS = 3`
  - `Hands(ctrl)`，方法：
    - `set_move(d, extra=None)`：切换跨拍按住的移动方向，`d=None` 松开
    - `tap(key)` → `bool`：冷却未到返回 `False` 且不按键，否则按键返回 `True`
    - `hold(key)` / `release(key)`：按住/松开（`HoldUntil` 用）
    - `release_all()`：松全部键并清空内部状态
    - `tick()`：拍号 +1（冷却计时推进），由主循环每拍调一次
    - `counter_dir(belt_dir, cell)` → `Optional[str]`：站在传送带上时要顶住的反向键
    - 只读属性 `move_dir` / `move_extra` / `held_keys`（`Set[str]`）

**为什么要这一层**（spec §7.2 的 `Tap` 去抖、§7.6 的"halt 只松键"）：按键是**跨拍状态**（按住的方向、上次 tap 的拍号），而原语是每拍重建的树节点。把这块状态塞进原语，每拍新建的节点就记不住"我刚按过"。旧实现把它放在 `Agent` 上（`_set_move` / `move_dir` / `move_extra`，`oc/ai/agent.py:1163`），那是 God Object 的一部分。这里收成一个独立对象，controller 注入——于是离线测试注入一个记录调用的假 controller，就能断言键序列而不碰 `Quartz`。

- [ ] **Step 1: 写失败测试**

创建 `tests/test_hands.py`：

```python
"""Hands 按键输出层单测。

全程假 controller，零真实按键注入——Plan 2 的交付标准之一。
"""
from ai.hands import Hands, DIR2D, OPPOSITE, TAP_COOLDOWN_TICKS


class FakeCtrl(object):
    """记录调用序列的假 controller。接口与 oc/ai/controller.py 对齐。"""

    def __init__(self):
        self.calls = []

    def tap(self, name, hold=0.12):
        self.calls.append(("tap", name))

    def press(self, name):
        self.calls.append(("press", name))

    def release(self, name):
        self.calls.append(("release", name))

    def release_all(self):
        self.calls.append(("release_all",))


def test_set_move_presses_once_and_is_idempotent():
    """同方向重复 set_move 不重按——按住是持续态，重按会打断移动。"""
    c = FakeCtrl()
    h = Hands(c)
    h.set_move("left")
    h.set_move("left")
    assert c.calls == [("press", "left")]
    assert h.move_dir == "left"


def test_set_move_switch_releases_old():
    c = FakeCtrl()
    h = Hands(c)
    h.set_move("left")
    h.set_move("up")
    assert c.calls == [("press", "left"), ("release", "left"), ("press", "up")]


def test_set_move_none_releases():
    c = FakeCtrl()
    h = Hands(c)
    h.set_move("left")
    h.set_move(None)
    assert c.calls[-1] == ("release", "left")
    assert h.move_dir is None


def test_set_move_extra_for_belt():
    """传送带上垂直穿越：主方向 + 顶带方向同时按住。"""
    c = FakeCtrl()
    h = Hands(c)
    h.set_move("up", "left")
    assert ("press", "up") in c.calls and ("press", "left") in c.calls
    assert h.move_extra == "left"
    h.set_move("up", None)
    assert ("release", "left") in c.calls
    assert h.move_extra is None


def test_tap_edge_triggered_within_cooldown():
    """Review Focus 1：冷却内的第二次 tap 不按键，且返回 False。

    拾取是 toggle（oc/ai/agent.py:921），连点 = 拿起又放下。
    """
    c = FakeCtrl()
    h = Hands(c)
    assert h.tap("pickup") is True
    assert h.tap("pickup") is False
    assert c.calls == [("tap", "pickup")]


def test_tap_allowed_after_cooldown():
    c = FakeCtrl()
    h = Hands(c)
    h.tap("pickup")
    for _ in range(TAP_COOLDOWN_TICKS):
        h.tick()
    assert h.tap("pickup") is True
    assert c.calls.count(("tap", "pickup")) == 2


def test_tap_cooldown_is_per_key():
    """不同键各自冷却：按了 pickup 不该挡住 chop。"""
    c = FakeCtrl()
    h = Hands(c)
    assert h.tap("pickup") is True
    assert h.tap("chop") is True


def test_hold_and_release_tracked():
    c = FakeCtrl()
    h = Hands(c)
    h.hold("chop")
    assert "chop" in h.held_keys
    h.release("chop")
    assert "chop" not in h.held_keys
    assert c.calls == [("press", "chop"), ("release", "chop")]


def test_hold_is_idempotent():
    """已按住再 hold 不重按——重按会让游戏侧看到一次抬起。"""
    c = FakeCtrl()
    h = Hands(c)
    h.hold("chop")
    h.hold("chop")
    assert c.calls == [("press", "chop")]


def test_release_unheld_key_is_noop():
    c = FakeCtrl()
    h = Hands(c)
    h.release("chop")
    assert c.calls == []


def test_release_all_clears_state():
    c = FakeCtrl()
    h = Hands(c)
    h.set_move("left", "up")
    h.hold("chop")
    h.release_all()
    assert ("release_all",) in c.calls
    assert h.move_dir is None and h.move_extra is None
    assert h.held_keys == set()


def test_release_all_lets_next_tap_through():
    """release_all 是"全部收手"，不该让后续 tap 被旧冷却挡住。

    中断（火情/脱困）会调 release_all，此后抢占方的第一个 tap 必须生效，
    否则抢占方的首个交互要白等几拍。
    """
    c = FakeCtrl()
    h = Hands(c)
    h.tap("pickup")
    h.release_all()
    assert h.tap("pickup") is True


def test_counter_dir():
    """站在往右推的带上 → 顶左键。"""
    h = Hands(FakeCtrl())
    belt = {(0, 0): (1, 0), (1, 0): (0, -1)}
    assert h.counter_dir(belt, (0, 0)) == "left"
    assert h.counter_dir(belt, (1, 0)) == "up"
    assert h.counter_dir(belt, (5, 5)) is None
    assert h.counter_dir({}, (0, 0)) is None


def test_dir_tables_are_consistent():
    for d, opp in OPPOSITE.items():
        assert d in DIR2D and opp in DIR2D
        v, ov = DIR2D[d], DIR2D[opp]
        assert (v[0] + ov[0], v[1] + ov[1]) == (0, 0)
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `python3 -m pytest tests/test_hands.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'ai.hands'`

- [ ] **Step 3: 实现 `ai/hands.py`**

```python
"""按键输出：跨拍按住的移动方向 + Tap 去抖与冷却。

为什么单列一层：按键是**跨拍状态**（按住哪个方向、上次 tap 在哪一拍），
而原语是每拍重建的树节点，塞进原语就记不住"我刚按过"。旧实现把它放在
Agent 上（oc/ai/agent.py:1163 的 _set_move / move_dir / move_extra），
那是 §2.2 批判的 God Object 的一部分。

controller 注入（不自己 import）：离线测试传一个记录调用的假 controller
就能断言键序列，不碰 Quartz。Plan 2 的交付标准之一是"不注入按键"。

Tap 去抖的由来（spec §7.2）：游戏里拾取是 toggle（oc/ai/agent.py:921
注释"拾取是 toggle，连点会拿起又放下"），而 bt.Sequence 每拍从头 tick、
已 SUCCESS 的子节点照旧被 tick。两者相乘就是"反复拿起放下"。
"""
from typing import Dict, Optional, Set, Tuple

DIR2D = {"left": (-1, 0), "right": (1, 0), "up": (0, 1), "down": (0, -1)}
OPPOSITE = {"left": "right", "right": "left", "up": "down", "down": "up"}

# 同一按键两次 tap 的最小间隔（拍）。20 Hz 下 3 拍 = 150 ms，
# 够插件推一帧新状态过来（5 Hz = 200 ms）判断上次 tap 有没有生效。
TAP_COOLDOWN_TICKS = 3


class Hands(object):
    """按键输出的唯一出口。原语通过 ctx["hands"] 调它。"""

    def __init__(self, ctrl):
        # type: (object) -> None
        self.ctrl = ctrl
        self.move_dir = None     # type: Optional[str]
        self.move_extra = None   # type: Optional[str]
        self.held_keys = set()   # type: Set[str]
        self._tick = 0
        self._last_tap = {}      # type: Dict[str, int]

    # ---------- 拍推进 ----------
    def tick(self):
        # type: () -> None
        """拍号 +1。主循环每拍调一次，冷却靠它推进。"""
        self._tick += 1

    # ---------- 移动 ----------
    def set_move(self, d, extra=None):
        # type: (Optional[str], Optional[str]) -> None
        """切换持续按住的移动方向；d=None 松开。extra 为顶带用的第二方向。

        同方向重复调用直接返回——按住是持续态，重按会让游戏侧看到一次
        抬起，表现为移动一顿一顿。
        """
        if d == self.move_dir and extra == self.move_extra:
            return
        if self.move_dir:
            self.ctrl.release(self.move_dir)
        if self.move_extra:
            self.ctrl.release(self.move_extra)
        if d:
            self.ctrl.press(d)
        if extra:
            self.ctrl.press(extra)
        self.move_dir = d
        self.move_extra = extra

    def counter_dir(self, belt_dir, cell):
        # type: (Dict[Tuple[int, int], Tuple[int, int]], Tuple[int, int]) -> Optional[str]
        """站在传送带上要顶住的反向键；不在带上则 None。"""
        bd = (belt_dir or {}).get(cell)
        if not bd:
            return None
        for name, vec in DIR2D.items():
            if vec[0] == -bd[0] and vec[1] == -bd[1]:
                return name
        return None

    # ---------- 单击 ----------
    def tap(self, key):
        # type: (str) -> bool
        """单击一次。冷却未到则不按键并返回 False。

        冷却按**键**计，不是全局：按了 pickup 不该挡住 chop。
        """
        last = self._last_tap.get(key)
        if last is not None and self._tick - last < TAP_COOLDOWN_TICKS:
            return False
        self.ctrl.tap(key)
        self._last_tap[key] = self._tick
        return True

    # ---------- 按住 ----------
    def hold(self, key):
        # type: (str) -> None
        """按下不放。已按住则直接返回（重按会让游戏看到一次抬起）。"""
        if key in self.held_keys:
            return
        self.ctrl.press(key)
        self.held_keys.add(key)

    def release(self, key):
        # type: (str) -> None
        """松开。未按住则空操作——halt 会无条件调它。"""
        if key not in self.held_keys:
            return
        self.ctrl.release(key)
        self.held_keys.discard(key)

    def release_all(self):
        # type: () -> None
        """全部收手并清空状态，含 tap 冷却。

        清冷却的理由：release_all 的调用方是中断（火情/脱困）与生命周期
        守卫，语义是"上一段执行作废"。若留着旧冷却，抢占方的第一个交互
        要白等几拍。
        """
        self.ctrl.release_all()
        self.move_dir = None
        self.move_extra = None
        self.held_keys.clear()
        self._last_tap.clear()
```

- [ ] **Step 4: 运行测试，确认通过**

Run: `python3 -m pytest tests/test_hands.py -v`
Expected: PASS，14 个测试全绿。

- [ ] **Step 5: 确认没碰 Quartz**

Run: `grep -rn "Quartz\|import controller" ai/hands.py tests/test_hands.py`
Expected: 无输出。

- [ ] **Step 6: 提交**

```bash
git add ai/hands.py tests/test_hands.py
git commit -m "feat(hands): 按键输出层，Tap 去抖与跨拍移动状态

按键是跨拍状态（按住哪个方向、上次 tap 在哪一拍），而原语是每拍重建
的树节点——塞进原语就记不住"我刚按过"。旧实现放在 Agent 上
(_set_move/move_dir/move_extra)，是 spec §2.2 批判的 God Object 一部分。

controller 注入而非自己 import：测试传假 controller 断言键序列，
不碰 Quartz。这是 Plan 2"不注入按键"交付标准的落点。

Tap 去抖（spec §7.2）：拾取是 toggle，而 bt.Sequence 每拍从头 tick、
已 SUCCESS 的子节点照旧被 tick，两者相乘就是反复拿起放下。冷却按键计
而非全局，否则 pickup 会挡住 chop。release_all 顺带清冷却——它的语义
是"上一段执行作废"，留着旧冷却会让抢占方的首个交互白等几拍。

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---
### Task 3: 寻路回溯 `trace_back` / `first_step`

**Files:**
- Modify: `ai/navigation.py`（追加两个函数）
- Modify: `tests/test_navigation.py`（追加测试）

**Interfaces:**
- Consumes: `navigation.distance_field(start, is_walkable, belt_dir, blocked, max_cells)` → `Dict[Tuple[int,int], int]`、`navigation.step_cost(belt_dir, cur, nxt)` → `int`（Plan 1 已交付）
- Produces:
  - `trace_back(field, goal, belt_dir)` → `Optional[List[Tuple[int,int]]]`，从 `start` 到 `goal` 的格子序列（含两端）；`goal` 不在 `field` 里则 `None`
  - `first_step(field, goal, belt_dir)` → `Optional[Tuple[int,int]]`，从 `start` 出发的下一格；`goal` 就是 `start` 时返回 `None`（已到达）

**背景**（spec §11.2.1 "已知缝隙"表最后一行，明确归 Plan 2）：`distance_field` 只返回 `{cell: cost}`，没有前驱信息。而原语要选移动方向键，必须知道"下一步往哪走"。

**本任务顺带把真实帧 fixture 搬到 `conftest.py`**：`frames` / `level_frame` 现在是 `tests/test_real_frames.py:39-54` 的**模块级** fixture，本计划后面有四个测试文件要用真实帧。模块级 fixture 跨文件不可见，不搬就得复制四份。

**不能用梯度下降**：`step_cost` 按**离开**的格子计代价而非进入的格子，是非对称的——`step_cost(belt={(0,0):(1,0)}, (0,0)→(-1,0)) == 8`，而反方向 `step_cost(..., (-1,0)→(0,0)) == 1`。同一条边两个方向代价不同，所以"走到代价最小的相邻格"会选出非法前驱。spec 实测在真实帧 60 个可达格里有 2 个（均在传送带排）选错。

**正确谓词**：从 `cur` 的邻居 `n` 中找满足 `field[n] + step_cost(belt_dir, n, cur) == field[cur]` 的 `n`，而不是找 `field` 值最小的邻居。

- [ ] **Step 1a: 把真实帧 fixture 搬到 `conftest.py`**

把 `tests/test_real_frames.py:20-54` 的 `FIXTURE_DIR` / `_fixture_files` / `frames` / `level_frame` 整段**剪切**到 `tests/conftest.py`（追加在现有 fixture 之后），`scope` 从 `"module"` 改为 `"session"`——跨文件共用时按 session 缓存，1102 帧只解析一次。

追加到 `tests/conftest.py`：

```python
import glob
import os

from ai.replay import load_frames

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


def fixture_files():
    # type: () -> list
    """入库为 .ndjson.gz；本地未压缩 .ndjson 若与某 .gz 同名则不重复加载。"""
    gz = glob.glob(os.path.join(FIXTURE_DIR, "*.ndjson.gz"))
    stems = set(p[: -len(".gz")] for p in gz)
    plain = [
        p
        for p in glob.glob(os.path.join(FIXTURE_DIR, "*.ndjson"))
        if p not in stems
    ]
    return sorted(gz + plain)


@pytest.fixture(scope="session")
def frames():
    """全部真实帧。scope=session：多个测试文件共用，1102 帧只解析一次。"""
    fs = []
    for path in fixture_files():
        fs.extend(load_frames(path))
    assert fs, "样本帧文件为空"
    return fs


@pytest.fixture(scope="session")
def level_frame(frames):
    """取第一帧 phase==level 的（关卡内）帧。"""
    for f in frames:
        if f.get("phase") == "level" and f.get("stations"):
            return f
    pytest.skip("样本里没有关卡内的帧")
```

`tests/test_real_frames.py` 相应改动：删掉 `:20-54` 的那四段，`pytestmark` 改为从 conftest 导入的函数判断：

```python
from conftest import fixture_files

pytestmark = pytest.mark.skipif(
    not fixture_files(),
    reason="缺 tests/fixtures/*.ndjson，先运行 tools/record_frames.py",
)
```

同时删掉 `test_real_frames.py` 里已无用的 `glob` / `os` / `load_frames` / `FIXTURE_DIR` 导入（`grep -n "glob\|os\.\|load_frames\|FIXTURE_DIR" tests/test_real_frames.py` 确认无残留引用后再删）。

- [ ] **Step 1b: 运行测试，确认搬迁未改变结果**

Run: `python3 -m pytest tests/test_real_frames.py -v`
Expected: 与搬迁前相同的测试全部通过，个数不变（搬 fixture 不改断言）。

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_navigation.py` 末尾：

```python
def test_trace_back_straight_line():
    """一条直走廊：回溯出的路径应是连续四邻接格。"""
    walk = lambda gx, gz: gz == 0 and 0 <= gx <= 4
    field = distance_field((0, 0), walk, {})
    path = N.trace_back(field, (4, 0), {})
    assert path == [(0, 0), (1, 0), (2, 0), (3, 0), (4, 0)]


def test_trace_back_start_itself():
    walk = lambda gx, gz: True
    field = distance_field((0, 0), walk, {}, max_cells=50)
    assert N.trace_back(field, (0, 0), {}) == [(0, 0)]


def test_trace_back_unreachable_returns_none():
    walk = lambda gx, gz: gz == 0 and 0 <= gx <= 2
    field = distance_field((0, 0), walk, {})
    assert N.trace_back(field, (9, 9), {}) is None


def test_trace_back_path_is_contiguous_and_walkable():
    """L 形走廊：每一步必须是四邻接，且每格可走。"""
    def walk(gx, gz):
        return (gz == 0 and 0 <= gx <= 3) or (gx == 3 and 0 <= gz <= 3)
    field = distance_field((0, 0), walk, {})
    path = N.trace_back(field, (3, 3), {})
    assert path is not None
    assert path[0] == (0, 0) and path[-1] == (3, 3)
    for a, b in zip(path, path[1:]):
        assert abs(a[0] - b[0]) + abs(a[1] - b[1]) == 1
        assert walk(b[0], b[1])


def test_trace_back_respects_asymmetric_belt_cost():
    """核心用例：step_cost 按离开的格子计，是非对称的。

    一条 5 格走廊，(2,0) 是往右推的传送带。从 (0,0) 出发往右走是顺带
    （离开 (2,0) 向右，代价 1）；若用"选 field 最小的邻居"做回溯，在
    传送带排上会选出代价账算不平的前驱。这里断言回溯出的路径每一步的
    代价账都对得上 field 的差值。
    """
    walk = lambda gx, gz: gz == 0 and 0 <= gx <= 4
    belt = {(2, 0): (1, 0)}
    field = distance_field((0, 0), walk, belt)
    path = N.trace_back(field, (4, 0), belt)
    assert path is not None
    for a, b in zip(path, path[1:]):
        assert field[a] + N.step_cost(belt, a, b) == field[b], \
            "回溯出的边必须是距离场里真正用过的那条边"


def test_trace_back_rejects_gradient_descent_trap():
    """反向走传送带时，最小邻居不是合法前驱。

    布局：(0,0) 起点，(1,0) 是往**左**推的带。从 (0,0) 走到 (2,0) 必须
    穿过 (1,0)，而离开 (1,0) 往右是逆带（代价 8）。
    field: (0,0)=0, (1,0)=1, (2,0)=9。
    回溯 (2,0) 时邻居只有 (1,0)，但必须验账 1 + 8 == 9 才认它——
    若只按"field 最小"选，遇到多邻居场景会选出 1 + 1 != 9 的非法边。
    """
    walk = lambda gx, gz: gz == 0 and 0 <= gx <= 2
    belt = {(1, 0): (-1, 0)}
    field = distance_field((0, 0), walk, belt)
    assert field[(2, 0)] == 9
    assert N.trace_back(field, (2, 0), belt) == [(0, 0), (1, 0), (2, 0)]


def test_first_step():
    walk = lambda gx, gz: gz == 0 and 0 <= gx <= 4
    field = distance_field((0, 0), walk, {})
    assert N.first_step(field, (4, 0), {}) == (1, 0)


def test_first_step_at_goal_returns_none():
    """已经站在目标格上：没有"下一步"，返回 None 而不是自己。"""
    walk = lambda gx, gz: True
    field = distance_field((0, 0), walk, {}, max_cells=50)
    assert N.first_step(field, (0, 0), {}) is None


def test_first_step_unreachable_returns_none():
    walk = lambda gx, gz: gz == 0 and 0 <= gx <= 2
    field = distance_field((0, 0), walk, {})
    assert N.first_step(field, (9, 9), {}) is None


def test_trace_back_on_real_frame_all_reachable_cells(level_frame):
    """真实帧全覆盖：每个可达格都能回溯出一条合法路径。

    spec 记梯度下降在真实帧 60 个可达格里有 2 个选出非法前驱
    （均在传送带排）。这个测试就是那条断言的反面——正确谓词应 0 失败。
    """
    from ai.world import World
    w = World(level_frame)
    chef = None
    for c in w.chefs:
        if c.get("x") is not None:
            chef = c
            break
    if chef is None:
        pytest.skip("样本里没有带世界坐标的厨师")
    start = w.to_grid(chef["x"], chef["z"])
    field = distance_field(start, w.walkable, w.belt_dir)
    assert len(field) > 10, "距离场太小，这个测试没在真实数据上跑"

    for cell in field:
        path = N.trace_back(field, cell, w.belt_dir)
        assert path is not None, "可达格却回溯不出路径：%r" % (cell,)
        assert path[0] == start and path[-1] == cell
        for a, b in zip(path, path[1:]):
            assert abs(a[0] - b[0]) + abs(a[1] - b[1]) == 1
            assert w.walkable(b[0], b[1])
            assert field[a] + N.step_cost(w.belt_dir, a, b) == field[b]
```

`tests/test_navigation.py` 现在只 `from ai.navigation import (...)` 具体名字（`:1`），新测试用 `N.trace_back` 形式，故在文件头补两行：

```python
import pytest

from ai import navigation as N
```

`level_frame` 由 Step 1a 搬进 conftest 后自动可用，无需本文件声明。

- [ ] **Step 2: 运行测试，确认失败**

Run: `python3 -m pytest tests/test_navigation.py -v -k "trace_back or first_step"`
Expected: FAIL，`AttributeError: module 'ai.navigation' has no attribute 'trace_back'`

- [ ] **Step 3: 实现两个函数**

追加到 `ai/navigation.py` 末尾：

```python
def _predecessor(field, cur, belt_dir):
    # type: (Dict, Tuple[int, int], Dict) -> Optional[Tuple[int, int]]
    """cur 在距离场里的合法前驱。

    判据是**代价账对得上**：field[n] + step_cost(n → cur) == field[cur]，
    而不是"field 值最小的邻居"。

    为什么不能用梯度下降：step_cost 按**离开**的格子计代价，是非对称的——
    step_cost(belt={(0,0):(1,0)}, (0,0)→(-1,0)) == 8，而反方向
    step_cost(..., (-1,0)→(0,0)) == 1。同一条边两个方向代价不同，所以
    "最小邻居"会选出距离场里根本没走过的边。spec §11.2.1 实测真实帧
    60 个可达格里有 2 个（均在传送带排）被梯度下降选错。
    """
    want = field.get(cur)
    if want is None:
        return None
    for dx, dz in _NEIGHBORS:
        n = (cur[0] + dx, cur[1] + dz)
        c = field.get(n)
        if c is None:
            continue
        if c + step_cost(belt_dir, n, cur) == want:
            return n
    return None


def trace_back(field, goal, belt_dir):
    # type: (Dict, Tuple[int, int], Dict) -> Optional[List[Tuple[int, int]]]
    """从距离场回溯 start → goal 的格子序列（含两端）。

    goal 不在 field 里（不可达）返回 None。start 自身返回 [start]。
    """
    if goal not in field:
        return None
    path = [goal]
    cur = goal
    # 上限 len(field)：每格最多进路径一次，断链/环都在这里止住而不是死循环
    for _ in range(len(field)):
        if field[cur] == 0:
            path.reverse()
            return path
        prev = _predecessor(field, cur, belt_dir)
        if prev is None:
            return None          # 距离场被破坏，交还调用方（原语会转 FAILURE）
        path.append(prev)
        cur = prev
    return None


def first_step(field, goal, belt_dir):
    # type: (Dict, Tuple[int, int], Dict) -> Optional[Tuple[int, int]]
    """从 start 出发朝 goal 的下一格。

    已站在 goal 上，或 goal 不可达，都返回 None——调用方据此区分
    "到了"（配合自己的到位判定）与"去不了"。
    """
    path = trace_back(field, goal, belt_dir)
    if not path or len(path) < 2:
        return None
    return path[1]
```

- [ ] **Step 4: 运行测试，确认通过**

Run: `python3 -m pytest tests/test_navigation.py -v`
Expected: PASS，含 Plan 1 既有测试与新增 10 个。

- [ ] **Step 5: 运行全量测试**

Run: `python3 -m pytest -v`
Expected: 全绿。

- [ ] **Step 6: 提交**

```bash
git add ai/navigation.py tests/test_navigation.py tests/conftest.py tests/test_real_frames.py
git commit -m "feat(navigation): trace_back / first_step，补上 Plan 1 的已知缝隙

distance_field 只返回 {cell: cost}，没有前驱信息，而原语选移动方向键
必须知道"下一步往哪走"。spec §11.2.1 把这条缝隙明确归给 Plan 2。

不用梯度下降：step_cost 按**离开**的格子计代价，是非对称的——
step_cost(belt={(0,0):(1,0)}, (0,0)->(-1,0))==8 而反方向==1，同一条边
两个方向代价不同，"走到 field 最小的邻居"会选出距离场里没走过的边。
spec 实测真实帧 60 个可达格里有 2 个（均在传送带排）被它选错。

正确谓词是验账：field[n] + step_cost(n->cur) == field[cur]。
test_trace_back_on_real_frame_all_reachable_cells 在真实帧上对每个可达
格全覆盖断言，正是那 2 个失败格的反面。

顺带把 frames / level_frame 两个 fixture 从 test_real_frames.py 的模块
作用域搬到 conftest.py 的 session 作用域：本计划后面有四个测试文件要用
真实帧，模块级 fixture 跨文件不可见，不搬就得复制四份。

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4: 交互前置校验与锅具识别

**Files:**
- Modify: `ai/predicates.py`（追加）
- Modify: `tests/test_predicates.py`（追加）

**Interfaces:**
- Consumes: `predicates.station_name_matches(a, b)` → `bool`、`predicates.held_name(chef)` → `str`（Plan 1 已交付）
- Produces:
  - `COOKWARE_KEYWORDS`（元组）
  - `cookware_names(recipes)` → `Set[str]`：从配方的 `cook` 字段取全部锅具类型名（小写）
  - `held_is_cookware(chef, recipes=None)` → `bool`
  - `interact_ready(chef, target, kind=None, to_grid=None)` → `bool`：交互前置校验
  - `chef_cell(chef, to_grid)` → `Optional[Tuple[int,int]]`
  - `facing_cell(chef)` → `Optional[Tuple[int,int]]`：朝向的那一格（由 `interactions.gx/gz` 给出，不从 yaw 推）

**两件事，为什么合成一个任务**：都是"给原语提供判据的纯谓词"，都改同一个文件，一个 reviewer 一次看完。

**`interact_ready` 是 `Approach` / `InteractWith` 的前置校验核心**（spec §7.5）。从 `oc/ai/agent.py:1092` 移植，两处改动：
- 不再接 `World`，`to_grid` 作为可选回调注入（纯函数化，单测不造 World）。
- `station_name_matches` 替掉旧的"互为子串"兜底——旧写法让 `"workstation_cooker_01" in "workstation_cooker_01 (3)"` 为真，查 1 号灶拿到 3 号灶的状态（Plan 1 已在真实帧复现过这个 bug）。

**`held_is_cookware` 是 spec §9 点名必须修的**：`predicates.held_is_pot` 现在按关键词 `("pot","pan","fryer","tray","tin","steamer")` 匹配，而 `AI_REVIEW.md:78` 自标 ⚠️「名字清单未在游戏实测」。实测帧里手持锅的名字是 `utensil_pot_01`，关键词能中；但 `DeepFatFryer` / `OvenTray` / `OvenCakeTin` / `Steamer` 四种灶具的**持有物**名字未实测。spec §6.1 第 5、12 号 task 用"手持空锅"作纯观测可行性来替代 `pot_cooked` / `dropped_cooked_pot`，这个替代的正确性完全依赖锅具识别可靠。故改为**配方数据驱动 + 关键词兜底**：`recipes[].cook` 列出了本关实际会用到的锅具类型（实测含全部 6 种），优先用它。

- [ ] **Step 1: 写失败测试——锅具识别**

追加到 `tests/test_predicates.py` 末尾：

```python
# ---------- 锅具识别（spec §9）----------

_RECIPES_SUSHI = [
    {"name": "BoiledSushiRice", "cook": "Pot", "comp": ["SushiRice"]},
    {"name": "Cucumber", "cook": "null", "comp": []},
]
_RECIPES_MULTI = [
    {"name": "FriedChips", "cook": "DeepFatFryer", "comp": ["Potato"]},
    {"name": "Cake_Plain", "cook": "OvenCakeTin", "comp": ["MixedFlourEggHoneycomb"]},
    {"name": "SteamedSpecial_Fish", "cook": "Steamer", "comp": ["UncookedFish"]},
    {"name": "CheesePizza", "cook": "OvenTray", "comp": ["Dough", "Cheese"]},
]


def test_cookware_names_from_recipes():
    assert P.cookware_names(_RECIPES_SUSHI) == {"pot"}
    assert P.cookware_names(_RECIPES_MULTI) == {
        "deepfatfryer", "ovencaketin", "steamer", "oventray"}


def test_cookware_names_skips_null_and_missing():
    """cook 字段是字符串 "null"（不是 None），这是插件的实际输出。"""
    assert P.cookware_names([{"name": "X", "cook": "null"},
                             {"name": "Y"},
                             {"name": "Z", "cook": None}]) == set()
    assert P.cookware_names(None) == set()


def test_held_is_cookware_real_held_name():
    """实测帧里手持锅的名字是 utensil_pot_01（关键词路径）。"""
    assert P.held_is_cookware({"held": "utensil_pot_01"}) is True


def test_held_is_cookware_from_recipe_data():
    """Review Focus：配方里有 DeepFatFryer，手持物名字含它就算锅具。

    关键词表里有 "fryer" 能碰巧中；但 OvenCakeTin 这类必须靠配方数据。
    """
    chef = {"held": "equipment_OvenCakeTin_01"}
    assert P.held_is_cookware(chef, _RECIPES_MULTI) is True
    assert P.held_is_cookware(chef, _RECIPES_SUSHI) is False, \
        "本关配方没有 OvenCakeTin 时，不该认它为锅具"


def test_held_is_cookware_keyword_fallback_without_recipes():
    """不传 recipes 时退化为关键词表，保证旧调用点不变坏。"""
    assert P.held_is_cookware({"held": "utensil_frying_pan_01"}) is True
    assert P.held_is_cookware({"held": "Cucumber"}) is False


def test_held_is_cookware_empty_hands():
    assert P.held_is_cookware({"held": None}) is False
    assert P.held_is_cookware(None) is False


def test_held_is_cookware_does_not_match_plate():
    """盘子不是锅具——误判会让 ReturnPot 去把盘子放灶台上。"""
    assert P.held_is_cookware({"held": "equipment_plate_01"},
                              _RECIPES_MULTI) is False
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `python3 -m pytest tests/test_predicates.py -v -k cookware`
Expected: FAIL，`AttributeError: module 'ai.predicates' has no attribute 'cookware_names'`

- [ ] **Step 3: 实现锅具识别**

追加到 `ai/predicates.py`（放在 `held_is_pot` 之后）：

```python
# 锅具识别兜底关键词。主路径走配方数据（见 cookware_names）——
# spec §9 点名这块必须改成数据驱动：AI_REVIEW.md:78 自标 ⚠️「名字清单
# 未在游戏实测」，而 DeepFatFryer / OvenTray / OvenCakeTin / Steamer
# 四种灶具的名字里既无 pot 也无 pan，换关卡会直接漏。
COOKWARE_KEYWORDS = ("pot", "pan", "fryer", "tray", "tin", "steamer")

# 手持盘绝不是锅具。盘名 equipment_plate_01 不含上述关键词，
# 但配方驱动路径是子串匹配，显式排除一次更稳。
_NOT_COOKWARE = ("plate",)


def cookware_names(recipes):
    # type: (Optional[List[Dict]]) -> Set[str]
    """从配方数据取本关会用到的锅具类型名（小写）。

    recipes[].cook 是锅具类型（"Pot" / "FryingPan" / "DeepFatFryer" /
    "OvenTray" / "OvenCakeTin" / "Steamer"）。插件把"无"写成字符串
    "null" 而不是 JSON null，两种都要拦。
    """
    out = set()  # type: Set[str]
    for r in recipes or ():
        if not isinstance(r, dict):
            continue
        cook = r.get("cook")
        if not cook or str(cook).lower() == "null":
            continue
        out.add(str(cook).lower())
    return out


def held_is_cookware(chef, recipes=None):
    # type: (Optional[Dict], Optional[List[Dict]]) -> bool
    """手里拿的是不是锅具（锅 / 平底锅 / 油炸篮 / 烤盘 / 蛋糕模 / 蒸笼）。

    传 recipes 时以配方数据为准（spec §9 要求的数据驱动）；不传时退化
    为关键词表，保证既有调用点行为不变。

    为什么这个谓词必须可靠：spec §6.1 第 5、12 号 task（ReturnPot /
    RecoverLoosePot）用"手持空锅 / 地上有锅"作为**纯观测可行性**，以此
    替代旧代码的 pot_cooked / dropped_cooked_pot 两个记忆字段。替代的
    正确性完全押在这里。
    """
    h = held_name(chef)
    if not h:
        return False
    if any(k in h for k in _NOT_COOKWARE):
        return False
    names = cookware_names(recipes)
    if names:
        return any(n in h for n in names)
    return any(k in h for k in COOKWARE_KEYWORDS)
```

同时在文件头 `typing` 导入里补 `Set`（若尚未导入）。

- [ ] **Step 4: 运行测试，确认通过**

Run: `python3 -m pytest tests/test_predicates.py -v -k cookware`
Expected: PASS，7 个测试全绿。

- [ ] **Step 5: 写失败测试——`interact_ready`**

追加到 `tests/test_predicates.py` 末尾：

```python
# ---------- 交互前置校验（spec §7.5）----------

def _inter(**kw):
    """构造 interactions 字典，未给的键一律 None（与真实帧一致）。"""
    base = {"interactable": None, "pickup": None, "placement": None,
            "grid": None, "gx": None, "gz": None,
            "pickup_gx": None, "pickup_gz": None,
            "placement_gx": None, "placement_gz": None}
    base.update(kw)
    return base


def test_interact_ready_handle_grid_match():
    """最精确的信号：placement_gx/gz 与目标格一致（AGENTS.md §6）。"""
    chef = {"interactions": _inter(placement_gx=7, placement_gz=-2)}
    target = {"n": "workstation_cooker_01 (3)", "gx": 7, "gz": -2}
    assert P.interact_ready(chef, target, "placement") is True


def test_interact_ready_handle_grid_mismatch():
    """放错格子必须拦住——spec §7.5 第一类故障。"""
    chef = {"interactions": _inter(placement_gx=7, placement_gz=-1)}
    target = {"n": "c", "gx": 7, "gz": -2}
    assert P.interact_ready(chef, target, "placement") is False


def test_interact_ready_generic_grid_when_no_kind():
    chef = {"interactions": _inter(gx=1, gz=3)}
    target = {"n": "board", "gx": 1, "gz": 3}
    assert P.interact_ready(chef, target) is True


def test_interact_ready_rejects_plate_station_placement_handle():
    """放置句柄指向出餐口而目标不是出餐口 → 拒绝。

    否则盘子会被误放到上菜口，订单未完成就被判为交付。
    """
    chef = {"interactions": _inter(placement="plate_station_01",
                                   placement_gx=7, placement_gz=0)}
    target = {"n": "countertop_01_standard_wood (29)", "gx": 7, "gz": 0}
    assert P.interact_ready(chef, target, "placement") is False


def test_interact_ready_allows_plate_station_when_target_is_it():
    chef = {"interactions": _inter(placement="plate_station_01",
                                   placement_gx=9, placement_gz=0)}
    target = {"n": "plate_station_01", "gx": 9, "gz": 0}
    assert P.interact_ready(chef, target, "placement") is True


def test_interact_ready_derives_target_grid_via_to_grid():
    """目标只有世界坐标时用注入的 to_grid 换算（灭火器这类动态物）。"""
    chef = {"interactions": _inter(pickup_gx=-4, pickup_gz=2)}
    target = {"n": "utensil_fire_extinguisher_01", "x": 14.4, "z": -2.4}
    to_grid = lambda x, z: (-4, 2)
    assert P.interact_ready(chef, target, "pickup", to_grid) is True


def test_interact_ready_name_fallback_when_no_grid_signal():
    """无网格信号时退到精确名匹配（动态物、灭火器）。"""
    chef = {"interactions": _inter(pickup="utensil_fire_extinguisher_01")}
    target = {"n": "utensil_fire_extinguisher_01"}
    assert P.interact_ready(chef, target, "pickup") is True


def test_interact_ready_name_fallback_does_not_cross_instances():
    """名匹配不许跨实例：查 1 号灶不能拿到 3 号灶。

    旧实现用"互为子串"兜底，"workstation_cooker_01" in
    "workstation_cooker_01 (3)" 为真，已在真实帧上复现过。
    """
    chef = {"interactions": _inter(pickup="workstation_cooker_01 (3)")}
    target = {"n": "workstation_cooker_01"}
    assert P.interact_ready(chef, target, "pickup") is False


def test_interact_ready_extinguisher_keyword_fallback():
    """灭火器名字带实例后缀时的窄兜底（旧实现的必要行为）。"""
    chef = {"interactions": _inter(pickup="utensil_fire_extinguisher_01 (2)")}
    target = {"n": "utensil_fire_extinguisher_01_Rigidbody"}
    assert P.interact_ready(chef, target, "pickup") is True


def test_interact_ready_no_interactions_is_false():
    """Review Focus 5：帧里 interactions 为 None 时不许崩。"""
    assert P.interact_ready({"interactions": None}, {"n": "x"}) is False
    assert P.interact_ready({}, {"n": "x"}) is False
    assert P.interact_ready(None, {"n": "x"}) is False


def test_interact_ready_target_none_is_false():
    """目标在动作中途消失（被 P1 端走）→ False，不抛异常。"""
    chef = {"interactions": _inter(gx=1, gz=1)}
    assert P.interact_ready(chef, None) is False
    assert P.interact_ready(chef, {}) is False


def test_facing_cell_uses_interaction_grid_not_yaw():
    """朝向格取 interactions.gx/gz，不从 yaw 推。

    实测 801 帧：yaw 与交互格的对应关系不稳定（yaw=180 时交互格
    有 706 次在 (+1,0)、60 次在 (-1,0)），不能用来定朝向。
    而 interactions.gx/gz 是游戏自己算的交互目标，1367/1602 帧非空。
    """
    assert P.facing_cell({"interactions": _inter(gx=3, gz=-1)}) == (3, -1)
    assert P.facing_cell({"interactions": _inter()}) is None
    assert P.facing_cell(None) is None


def test_chef_cell():
    to_grid = lambda x, z: (int(x), int(z))
    assert P.chef_cell({"x": 2.0, "z": -3.0}, to_grid) == (2, -3)
    assert P.chef_cell({"x": None, "z": -3.0}, to_grid) is None
    assert P.chef_cell(None, to_grid) is None
```

- [ ] **Step 6: 运行测试，确认失败**

Run: `python3 -m pytest tests/test_predicates.py -v -k "interact_ready or facing_cell or chef_cell"`
Expected: FAIL，`AttributeError: module 'ai.predicates' has no attribute 'interact_ready'`

- [ ] **Step 7: 实现 `interact_ready` 及两个辅助**

追加到 `ai/predicates.py` 末尾：

```python
# ---------- 交互前置校验（spec §7.5）----------

PLATE_STATION_KEYWORD = "plate_station"
_EXT_KEYWORD = "extinguish"
_HANDLE_KEYS = ("interactable", "pickup", "placement", "grid")


def chef_cell(chef, to_grid):
    # type: (Optional[Dict], Any) -> Optional[Tuple[int, int]]
    """厨师所在格。to_grid 是 (x, z) -> (gx, gz) 的回调（World.to_grid）。"""
    if not chef:
        return None
    x, z = chef.get("x"), chef.get("z")
    if x is None or z is None:
        return None
    return to_grid(x, z)


def facing_cell(chef):
    # type: (Optional[Dict]) -> Optional[Tuple[int, int]]
    """厨师朝向的那一格，由 interactions.gx/gz 给出。

    **不从 yaw 推**：实测 801 帧里 yaw 与交互格的对应关系不稳定
    （yaw=180 时交互格 706 次在 (+1,0)、60 次在 (-1,0)），yaw 是渲染
    朝向而非交互判据。interactions.gx/gz 是游戏自己算出的交互目标，
    1367/1602 帧非空，这才是"面朝哪"的权威信号。
    """
    if not chef:
        return None
    inter = chef.get("interactions")
    if not isinstance(inter, dict):
        return None
    gx, gz = inter.get("gx"), inter.get("gz")
    if gx is None or gz is None:
        return None
    return (int(round(gx)), int(round(gz)))


def _target_cell(target, to_grid):
    # type: (Optional[Dict], Any) -> Tuple[Optional[int], Optional[int]]
    """目标格：优先 gx/gz，缺失时用 to_grid 从世界坐标换算。"""
    if not target:
        return (None, None)
    gx, gz = target.get("gx"), target.get("gz")
    if gx is not None and gz is not None:
        return (int(round(gx)), int(round(gz)))
    if to_grid is None:
        return (None, None)
    x, z = target.get("x"), target.get("z")
    if x is None or z is None:
        return (None, None)
    cell = to_grid(x, z)
    return (cell[0], cell[1])


def interact_ready(chef, target, kind=None, to_grid=None):
    # type: (Optional[Dict], Optional[Dict], Optional[str], Any) -> bool
    """"现在能跟 target 交互吗"——用游戏自己的交互信号判断。

    kind=None 用通用网格 interactions.gx/gz；kind="pickup"/"placement"
    优先用对应句柄的网格（pickup_gx/gz、placement_gx/gz），这是
    AGENTS.md §6 称的"能否交互的最精确信号"。

    从 oc/ai/agent.py:1092 移植，两处改动：
      1. 不接 World，to_grid 作为可选回调注入 → 纯函数，单测不造 World。
      2. 名匹配兜底改走 station_name_matches。旧实现用"互为子串"，
         "workstation_cooker_01" in "workstation_cooker_01 (3)" 为真，
         查 1 号灶会拿到 3 号灶的状态（Plan 1 已在真实帧复现）。

    target 为 None / 空（动作中途被 P1 端走）一律 False，不抛异常——
    spec §7.6 纪律 1：失败交还 Planner，不在执行层写恢复逻辑。
    """
    if not chef or not target:
        return False
    inter = chef.get("interactions")
    if not isinstance(inter, dict):
        return False

    tgx, tgz = _target_cell(target, to_grid)
    tn = str(target.get("n") or "").strip()

    # 放置句柄指向出餐口而目标不是出餐口 → 拒绝（防盘子误放上菜口）
    if kind == "placement":
        pl = str(inter.get("placement") or "").lower()
        if PLATE_STATION_KEYWORD in pl and PLATE_STATION_KEYWORD not in tn.lower():
            return False

    # 句柄专属网格（最精确）
    if kind in ("pickup", "placement"):
        hgx = inter.get("%s_gx" % kind)
        hgz = inter.get("%s_gz" % kind)
        if None not in (hgx, hgz, tgx, tgz):
            return int(round(hgx)) == tgx and int(round(hgz)) == tgz

    # 通用交互网格
    igx, igz = inter.get("gx"), inter.get("gz")
    if None not in (igx, igz, tgx, tgz):
        return int(round(igx)) == tgx and int(round(igz)) == tgz

    # 兜底：名匹配（无网格信号时，如动态物 / 灭火器）
    if not tn:
        return False
    for key in _HANDLE_KEYS:
        if station_name_matches(str(inter.get(key) or "").strip(), tn):
            return True
    # 灭火器名可能带 (n) 后缀或 _Rigidbody 变体，保留一个窄的关键字兜底
    if _EXT_KEYWORD in tn.lower():
        for key in _HANDLE_KEYS:
            if _EXT_KEYWORD in str(inter.get(key) or "").lower():
                return True
    return False
```

同时在文件头 `typing` 导入里确认有 `Any` / `Set` / `Tuple`。

- [ ] **Step 8: 运行测试，确认通过**

Run: `python3 -m pytest tests/test_predicates.py -v`
Expected: PASS，含 Plan 1 既有测试与本任务新增 20 个（7 个 cookware + 13 个 interact_ready/facing_cell/chef_cell）。

- [ ] **Step 9: 运行全量测试**

Run: `python3 -m pytest -v`
Expected: 全绿。

- [ ] **Step 10: 提交**

```bash
git add ai/predicates.py tests/test_predicates.py
git commit -m "feat(predicates): interact_ready 前置校验 + 锅具识别数据驱动

interact_ready 从 oc/ai/agent.py:1092 移植，两处改动：
  1. 不接 World，to_grid 作为可选回调注入，变成纯函数（单测不造 World）。
  2. 名匹配兜底改走 station_name_matches。旧实现用"互为子串"，
     "workstation_cooker_01" in "...(3)" 为真，查 1 号灶拿到 3 号灶的
     状态——Plan 1 已在真实帧复现过。

held_is_cookware 是 spec §9 点名必须修的那块：旧 held_is_pot 按关键词
匹配，而 AI_REVIEW.md:78 自标"名字清单未在游戏实测"，DeepFatFryer /
OvenTray / OvenCakeTin / Steamer 四种灶具名里既无 pot 也无 pan。改为
配方数据驱动（recipes[].cook）+ 关键词兜底。这个谓词的可靠性是
ReturnPot / RecoverLoosePot 用"手持空锅"替代 pot_cooked /
dropped_cooked_pot 两个记忆字段的全部依据。

facing_cell 取 interactions.gx/gz 而非从 yaw 推：实测 801 帧里 yaw=180
时交互格 706 次在 (+1,0)、60 次在 (-1,0)，yaw 是渲染朝向不是交互判据。

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---
### Task 5: 配方语义 `recipes.py`

**Files:**
- Create: `ai/recipes.py`
- Test: `tests/test_recipes.py`

**Interfaces:**
- Consumes: `predicates.plate_contents(plate)` → `Tuple[str, ...]`（Plan 1）
- Produces:
  - `RecipeBook(recipes, stations)`，构造后只读。方法：
    - `requirements(order)` → `Tuple[str, ...]`：订单所需的**顶层成分**（原名，不转小写），即 `order["composition"]`
    - `needs_cooking(ing)` → `bool`：该成分是否要下锅（配方 `cook` 非 null）
    - `raw_of(ing)` → `Optional[str]`：熟料对应的生料（`comp[0]`）
    - `cookware_of(ing)` → `Optional[str]`：该熟料用哪种锅具（`cook`）
    - `chop_target(ing)` → `Optional[str]`：该生料切好后的名字（箱子的 `chop` 字段）；不可切则 `None`
    - `chop_stages(ing)` → `int`：需切几轮（箱子的 `chop_stages`，缺失给 0）
    - `missing(order, plate)` → `Tuple[str, ...]`：盘里还缺哪些顶层成分（spec §6.0 的"反向展开"）
  - `find_ingredient(world, ing)` → `Optional[Dict]`：`{"kind": ..., ...}`，`kind` ∈ `held` / `board` / `plate` / `loose` / `crate`

**为什么要 `RecipeBook` 而不是一堆散函数**（spec §8.2 `task.py` 拆解那行："配方语义查找与 `find_plate_counter` → planner + predicates，返回值形状与 claims 耦合，须与 task 定义同期定"）：配方查找要同时看 `recipes[]`（成分树）与 `stations[]`（箱子的 `chop` / `chop_stages`）。每个 task 的 `feasible` / `done` 每拍都要问好几次，散函数每次重建 `{name: recipe}` 索引是浪费；一次构造、整帧复用。

**`chop_target` 为什么必须从箱子读**（Review Focus 4）：spec §7.3 原话"`Chop` 的完成判定不用字符串前缀匹配。初版写 `item == Chopped*`，但多阶段切配或产物命名不以 `Chopped` 开头时会误判"。实测箱子确有该字段：`{"ing":"Cucumber","chop":"ChoppedCucumber","chop_stages":8}`、`{"ing":"Seaweed","chop":null,"chop_stages":null}`。

- [ ] **Step 1: 写失败测试——订单需求与反向展开**

创建 `tests/test_recipes.py`：

```python
"""配方语义单测。

入参是 recipes / stations 列表而非 World（find_ingredient 例外——它要
扫 chefs / workstations / plates / items 四个数组），单测不造完整快照。
"""
import os

import pytest

from ai import recipes as R

# 实测帧的真实数据（tests/fixtures/real_frame_sushi_1_4.json）
RECIPES = [
    {"name": "Sushi_Fish", "t": "CompositeOrderNode", "comp":
        ["Seaweed", "BoiledSushiRice", "SushiFish"], "cook": "null"},
    {"name": "Sushi_Cucumber", "t": "CompositeOrderNode", "comp":
        ["Seaweed", "BoiledSushiRice", "Cucumber"], "cook": "null"},
    {"name": "BoiledSushiRice", "t": "CookedCompositeOrderNode",
     "comp": ["SushiRice"], "cook": "Pot", "plate": "CompositeSushi"},
    {"name": "Seaweed", "t": "IngredientOrderNode", "comp": [],
     "cook": "null", "plate": "CompositeSushi"},
    {"name": "Cucumber", "t": "IngredientOrderNode", "comp": [],
     "cook": "null", "plate": "CompositeSushi"},
    {"name": "SushiFish", "t": "IngredientOrderNode", "comp": [],
     "cook": "null", "plate": "CompositeSushi"},
    {"name": "SushiRice", "t": "IngredientOrderNode", "comp": [],
     "cook": "null", "plate": "null"},
]

STATIONS = [
    {"t": "crate", "n": "DispenserCrate 3 (1)", "ing": "Seaweed",
     "chop": None, "chop_stages": None, "gx": -5, "gz": 4},
    {"t": "crate", "n": "DispenserCrate 3 (5)", "ing": "SushiFish",
     "chop": "ChoppedSushiFish", "chop_stages": 8, "gx": -1, "gz": 4},
    {"t": "crate", "n": "DispenserCrate 3 (3)", "ing": "SushiRice",
     "chop": None, "chop_stages": None, "gx": -5, "gz": 3},
    {"t": "crate", "n": "DispenserCrate 3 (6)", "ing": "Cucumber",
     "chop": "ChoppedCucumber", "chop_stages": 8, "gx": -1, "gz": 3},
]

ORDER_FISH = {"id": "1", "dish": "Sushi_Fish",
              "composition": ["Seaweed", "BoiledSushiRice", "SushiFish"]}


def book():
    return R.RecipeBook(RECIPES, STATIONS)


def test_requirements_from_order_composition():
    assert book().requirements(ORDER_FISH) == (
        "Seaweed", "BoiledSushiRice", "SushiFish")


def test_requirements_falls_back_to_recipe_comp():
    """订单没带 composition 时从配方的 comp 取。

    实测帧的订单都带 composition，但它不保证永远在——dish 名字查配方
    是一条独立可用的路，缺了它这种帧会让整单无候选、AI 站着不动。
    """
    assert book().requirements({"dish": "Sushi_Cucumber"}) == (
        "Seaweed", "BoiledSushiRice", "Cucumber")


def test_requirements_unknown_order_is_empty():
    assert book().requirements({"dish": "NoSuchDish"}) == ()
    assert book().requirements(None) == ()


def test_needs_cooking():
    b = book()
    assert b.needs_cooking("BoiledSushiRice") is True
    assert b.needs_cooking("Cucumber") is False
    assert b.needs_cooking("NoSuch") is False


def test_raw_and_cookware_of_cooked_item():
    b = book()
    assert b.raw_of("BoiledSushiRice") == "SushiRice"
    assert b.cookware_of("BoiledSushiRice") == "Pot"
    assert b.raw_of("Cucumber") is None
    assert b.cookware_of("Cucumber") is None


def test_missing_reverse_expansion():
    """spec §6.0：从需求反向展开，已满足的不生成候选。"""
    b = book()
    plate = {"n": "p1", "contents": ["Seaweed"]}
    assert b.missing(ORDER_FISH, plate) == ("BoiledSushiRice", "SushiFish")


def test_missing_with_no_plate_is_everything():
    assert book().missing(ORDER_FISH, None) == (
        "Seaweed", "BoiledSushiRice", "SushiFish")


def test_missing_complete_plate_uses_real_frame_vocabulary():
    """核心用例：一张做完的盘长什么样，取自实测帧。

    801 帧实测：做完的 Sushi_Fish 盘是
        ['CookedCompositeAssembledNode', 'SushiFish', 'Seaweed']
    而订单要的是 ['Seaweed', 'BoiledSushiRice', 'SushiFish']。
    若按字面比较，BoiledSushiRice 永远报缺。
    """
    plate = {"contents": ["CookedCompositeAssembledNode", "SushiFish",
                          "Seaweed"]}
    assert book().missing(ORDER_FISH, plate) == ()


def test_missing_cooked_item_matched_by_opaque_token():
    """熟料只靠 token 匹配，字面名永不出现在盘里。"""
    b = book()
    plate = {"contents": ["CookedCompositeAssembledNode"]}
    assert b.plate_has(plate, "BoiledSushiRice") is True
    assert b.missing(ORDER_FISH, plate) == ("Seaweed", "SushiFish")


def test_plate_token_rules():
    b = book()
    # 1. 需烹饪 → 不透明 token
    assert b.plate_token("BoiledSushiRice") == R.COOKED_PLATE_TOKEN
    # 2. 需切的料 → 生料名（不是 chop_target）
    assert b.plate_token("SushiFish") == "sushifish"
    assert b.plate_token("Cucumber") == "cucumber"
    # 3. 其余 → 原名
    assert b.plate_token("Seaweed") == "seaweed"
    assert b.plate_token(None) is None


def test_plate_token_for_chopped_item_is_raw_name_not_chop_target():
    """实测：切好的料进盘后回落到生料名。

    板上是 ChoppedSushiFish，倒进盘后 contents 里是 SushiFish
    （帧 118→146 的 WS/plate 联动已验证）。若用 chop_target 去匹配，
    FetchToPlate.done 永不成立。
    """
    b = book()
    assert b.chop_target("SushiFish") == "ChoppedSushiFish"
    assert b.plate_token("SushiFish") == "sushifish"
    plate = {"contents": ["SushiFish"]}
    assert b.plate_has(plate, "SushiFish") is True


def test_missing_is_case_insensitive():
    """盘里的名字与配方名大小写不保证一致；不容忍会让整单永远"缺料"。"""
    plate = {"contents": ["seaweed", "SUSHIFISH"]}
    assert book().missing(ORDER_FISH, plate) == ("BoiledSushiRice",)


def test_missing_ignores_extra_contents():
    """盘里多放了料不影响"还缺什么"的判断。"""
    plate = {"contents": ["Seaweed", "Lettuce"]}
    assert book().missing(ORDER_FISH, plate) == (
        "BoiledSushiRice", "SushiFish")


@pytest.fixture(scope="module")
def real_frames():
    """真实录制帧。缺夹具时跳过——没录帧的环境仍能跑完其余测试。

    与 tests/test_real_frames.py 的 frames 夹具同源但不共享（那个是
    module-scope，跨文件不可见），所以这里自己加载一份。
    """
    from ai.replay import load_frames
    path = os.path.join(os.path.dirname(__file__), "fixtures",
                        "full_level.ndjson.gz")
    if not os.path.exists(path):
        pytest.skip("缺 tests/fixtures/full_level.ndjson.gz")
    fs = load_frames(path)
    assert fs, "样本帧文件为空"
    return fs


def test_plate_has_on_real_frames(real_frames):
    """真实帧全扫：每张"看起来做完了"的盘，都该被判为某张单料齐。

    这是对装盘态映射的端到端验证——若映射错了，三料盘会被判成还缺料，
    AI 永远不出餐。
    """
    checked = 0
    for f in real_frames:
        orders = f.get("orders") or []
        if not orders:
            continue
        b = R.RecipeBook(f.get("recipes"), f.get("stations"))
        for p in (f.get("plates") or []):
            contents = p.get("contents") or []
            if len(contents) < 3:
                continue
            checked += 1
            assert any(b.missing(o, p) == () for o in orders), \
                "三料盘 %r 没被任何订单判为料齐" % (contents,)
    assert checked > 0, "真实帧里没有三料盘，这个测试没验到东西"
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `python3 -m pytest tests/test_recipes.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'ai.recipes'`

- [ ] **Step 3: 实现 `RecipeBook` 的需求侧**

创建 `ai/recipes.py`：

```python
"""配方语义：订单需要什么、什么要下锅、什么要切、料在哪。

从 oc/ai/task.py 抽取（spec §8.2 "配方语义查找 → planner + predicates"）。
两处结构性改动：

  1. **CHOPPABLE 硬编码表删除**。旧代码有一张 {"SushiFish","Cucumber",
     "SushiCarrot","SushiPrawn"} 的可切食材表（oc/ai/task.py:21）。
     spec §9 记它"已泛化（主路径读 crate 的 chop/chop_stages），该表是
     遗留，随 task.py 拆解删除"。这里不带它。
  2. **一次构造、整帧复用**。旧 find_* 每次调用都重建 {name: recipe}
     索引，而新架构里每个 task 的 feasible/done 每拍要问好几次。

decompose() 不在这里——spec §8.2 明确"decompose() 删除"。它输出的线性
步骤序列正是 §6.2 要换掉的东西：执行顺序从可行性涌现，不预先排序。
"""
from typing import Dict, List, Optional, Set, Tuple

from ai import predicates as P

_NULL = ("null", "none", "")

# 熟料进盘后的不透明 token（实测 801 帧，小写比较）。盘里不记"是哪种
# 熟料"，只记"有一份熟的复合料"。见 RecipeBook.plate_token。
COOKED_PLATE_TOKEN = "cookedcompositeassemblednode"


def _is_null(v):
    # type: (object) -> bool
    """插件把"无"写成字符串 "null" 而不是 JSON null，两种都要拦。"""
    return v is None or str(v).strip().lower() in _NULL


class RecipeBook(object):
    """一帧的配方视图：成分树（recipes）+ 箱子的加工信息（stations）。"""

    def __init__(self, recipes, stations=None):
        # type: (Optional[List[Dict]], Optional[List[Dict]]) -> None
        self._by_name = {}      # type: Dict[str, Dict]
        for r in recipes or ():
            if isinstance(r, dict) and r.get("name"):
                self._by_name[str(r["name"]).lower()] = r
        # 箱子按产出食材索引，取 chop / chop_stages
        self._crates = {}       # type: Dict[str, Dict]
        for st in stations or ():
            if not isinstance(st, dict) or st.get("t") != "crate":
                continue
            ing = st.get("ing")
            if ing:
                self._crates[str(ing).lower()] = st

    def recipe(self, ing):
        # type: (Optional[str]) -> Optional[Dict]
        if not ing:
            return None
        return self._by_name.get(str(ing).lower())

    # ---------- 订单需求 ----------
    def requirements(self, order):
        # type: (Optional[Dict]) -> Tuple[str, ...]
        """订单所需的顶层成分，保留原始大小写（下游要用它查配方/箱子）。

        优先 order["composition"]（实测帧都有）；缺失时按 dish 名查配方
        的 comp。两条路都要有：composition 不保证永远在，只走它的话，
        缺字段的帧会让整单无候选、AI 站着不动。
        """
        if not order:
            return ()
        comp = order.get("composition")
        if comp:
            return tuple(str(c) for c in comp)
        r = self.recipe(order.get("dish"))
        if not r:
            return ()
        return tuple(str(c) for c in (r.get("comp") or ()))

    def missing(self, order, plate):
        # type: (Optional[Dict], Optional[Dict]) -> Tuple[str, ...]
        """盘里还缺哪些顶层成分（spec §6.0 的反向展开）。

        走 plate_token 做装盘态映射，不是字面比较——实测盘里的词汇与
        订单需求的词汇是两套（见 plate_token 的注释）。字面比较会让
        熟料永远报缺，AI 无限重倒。

        盘里多放的料不影响判断——这里只管"还缺什么"。
        """
        return tuple(ing for ing in self.requirements(order)
                     if self.plate_lacks(plate, ing))

    # ---------- 烹饪 ----------
    def needs_cooking(self, ing):
        # type: (Optional[str]) -> bool
        r = self.recipe(ing)
        return bool(r) and not _is_null(r.get("cook"))

    def raw_of(self, ing):
        # type: (Optional[str]) -> Optional[str]
        """熟料对应的生料。多成分熟料（如 Pasta_Marinara）取第一个。"""
        if not self.needs_cooking(ing):
            return None
        comp = (self.recipe(ing) or {}).get("comp") or ()
        return str(comp[0]) if comp else None

    def cookware_of(self, ing):
        # type: (Optional[str]) -> Optional[str]
        if not self.needs_cooking(ing):
            return None
        return str((self.recipe(ing) or {}).get("cook"))

    # ---------- 切配 ----------
    def chop_target(self, ing):
        # type: (Optional[str]) -> Optional[str]
        """该生料切好后的产物名；不可切则 None。

        从**箱子**的 chop 字段读，不做 "Chopped" 前缀匹配（spec §7.3）：
        多阶段切配或产物命名不以 Chopped 开头时前缀匹配会误判"已切好"。
        实测 {"ing":"Cucumber","chop":"ChoppedCucumber","chop_stages":8}。
        """
        if not ing:
            return None
        st = self._crates.get(str(ing).lower())
        if not st:
            return None
        chop = st.get("chop")
        return None if _is_null(chop) else str(chop)

    def chop_stages(self, ing):
        # type: (Optional[str]) -> int
        """需切几轮。缺失给 0（= 不可切）。"""
        if not ing:
            return 0
        st = self._crates.get(str(ing).lower())
        if not st:
            return 0
        n = st.get("chop_stages")
        try:
            return int(n or 0)
        except (TypeError, ValueError):
            return 0

    def needs_chopping(self, ing):
        # type: (Optional[str]) -> bool
        return self.chop_target(ing) is not None

    # ---------- 装盘态词汇（实测发现，见下方长注释）----------
    def plate_token(self, ing):
        # type: (Optional[str]) -> Optional[str]
        """订单成分 ing 进了盘以后，在 plate.contents 里叫什么（小写）。

        **这一层是必需的，不是便利**：实测 801 帧证明盘里的词汇与订单
        需求的词汇是两套。一张做完的 Sushi_Fish 盘是
            ['CookedCompositeAssembledNode', 'SushiFish', 'Seaweed']
        而订单要的是
            ['Seaweed', 'BoiledSushiRice', 'SushiFish']
        三样里只有 Seaweed 与 SushiFish 字面对得上。

        三条规则（全部有实测支撑）：
          1. 需烹饪的料（BoiledSushiRice）→ 不透明 token
             COOKED_PLATE_TOKEN。盘里不记它是什么熟料，只记"有一份熟的
             复合料"。实测 311 个含该 token 的盘帧，无一帧出现两次。
          2. 需切的料（SushiFish，板上是 ChoppedSushiFish）→ **生料名**。
             切好的料进盘后回落到生料名，不是 chop_target。
          3. 其余（Seaweed）→ 原名。

        按字面实现 spec §6.1 的"盘 contents 含该熟料"必然死循环：
        plate_lacks(plate, "BoiledSushiRice") 恒为真 →
        TakeCookedToPlate.done 永不成立 → AI 把饭倒进盘后以为没倒，
        无限重倒。
        """
        if not ing:
            return None
        if self.needs_cooking(ing):
            return COOKED_PLATE_TOKEN
        return str(ing).lower()

    def plate_has(self, plate, ing):
        # type: (Optional[Dict], Optional[str]) -> bool
        """盘里是否已有订单成分 ing（按装盘态词汇判断）。

        ⚠️ 已知局限：熟料的 token 不透明，所以**同一盘需要两份不同熟料
        的菜无法区分**（如 Pasta_Marinara_New 要 BoiledPasta +
        PanFriedPrawns + PanFriedFish）。本关 Sushi_1_4 每道菜只有一份
        熟料，不受影响。多熟料菜要靠 count 计数或插件补字段，记为
        Plan 3 的已知缺口而非本计划的交付项。
        """
        token = self.plate_token(ing)
        if token is None:
            return False
        return token in P.plate_contents(plate)

    def plate_lacks(self, plate, ing):
        # type: (Optional[Dict], Optional[str]) -> bool
        """plate_has 的反面。与 predicates.plate_lacks 的区别是**走装盘态
        词汇**——后者是字面比较，对熟料永远报缺。"""
        return not self.plate_has(plate, ing)
```

- [ ] **Step 4: 运行测试，确认通过**

Run: `python3 -m pytest tests/test_recipes.py -v`
Expected: PASS，13 个测试全绿。

- [ ] **Step 5: 写失败测试——切配与找料**

追加到 `tests/test_recipes.py`：

```python
# ---------- 切配（Review Focus 4）----------

def test_chop_target_from_crate_data():
    b = book()
    assert b.chop_target("Cucumber") == "ChoppedCucumber"
    assert b.chop_target("SushiFish") == "ChoppedSushiFish"
    assert b.chop_target("Seaweed") is None, "chop 为 null 的料不可切"
    assert b.chop_target("NoSuch") is None


def test_chop_target_not_prefix_matching():
    """Review Focus 4：产物名不以 Chopped 开头也必须正确。

    spec §7.3 点名这是初版的误判源。假一个 chop 产物叫 SlicedTuna——
    前缀匹配会认为"板上的 SlicedTuna 不是 Chopped*，还没切好"，于是
    无限切下去。
    """
    b = R.RecipeBook(RECIPES, [
        {"t": "crate", "ing": "Tuna", "chop": "SlicedTuna", "chop_stages": 4},
    ])
    assert b.chop_target("Tuna") == "SlicedTuna"
    assert b.needs_chopping("Tuna") is True


def test_chop_stages():
    b = book()
    assert b.chop_stages("Cucumber") == 8
    assert b.chop_stages("Seaweed") == 0
    assert b.chop_stages("NoSuch") == 0


def test_chop_stages_malformed_is_zero():
    b = R.RecipeBook(RECIPES, [
        {"t": "crate", "ing": "X", "chop": "ChoppedX", "chop_stages": "abc"},
    ])
    assert b.chop_stages("X") == 0


def test_case_insensitive_lookups():
    b = book()
    assert b.chop_target("cucumber") == "ChoppedCucumber"
    assert b.needs_cooking("boiledsushirice") is True


# ---------- 找料 ----------

class FakeWorld(object):
    def __init__(self, chefs=None, workstations=None, plates=None,
                 items=None, stations=None):
        self.chefs = chefs or []
        self.workstations = workstations or []
        self.plates = plates or []
        self.items = items or []
        self.stations = stations if stations is not None else STATIONS


def test_find_ingredient_held():
    w = FakeWorld(chefs=[{"name": "Player 1", "held": "Cucumber"}])
    got = R.find_ingredient(w, "Cucumber")
    assert got == {"kind": "held", "chef": "Player 1"}


def test_find_ingredient_board():
    ws = {"n": "board1", "item": "ChoppedCucumber"}
    w = FakeWorld(workstations=[ws])
    got = R.find_ingredient(w, "ChoppedCucumber")
    assert got["kind"] == "board" and got["st"] is ws


def test_find_ingredient_plate():
    p = {"n": "p1", "contents": ["Cucumber"]}
    w = FakeWorld(plates=[p])
    got = R.find_ingredient(w, "Cucumber")
    assert got["kind"] == "plate" and got["st"] is p


def test_find_ingredient_loose():
    it = {"n": "Cucumber", "x": 1.0, "z": 2.0}
    w = FakeWorld(items=[it])
    got = R.find_ingredient(w, "Cucumber")
    assert got["kind"] == "loose" and got["st"] is it


def test_find_ingredient_crate():
    got = R.find_ingredient(FakeWorld(), "Cucumber")
    assert got["kind"] == "crate"
    assert got["st"]["n"] == "DispenserCrate 3 (6)"


def test_find_ingredient_priority_held_over_crate():
    """手上优先于箱子：已经拿着的东西不该再去箱子取一份。"""
    w = FakeWorld(chefs=[{"name": "Player 2", "held": "Cucumber"}])
    assert R.find_ingredient(w, "Cucumber")["kind"] == "held"


def test_find_ingredient_exact_name_not_substring():
    """精确名匹配：Cucumber 与 ChoppedCucumber 是两种不同的料。

    宽松匹配会让"去拿生黄瓜"的 task 看到板上的切好黄瓜就以为料到手了。
    """
    w = FakeWorld(workstations=[{"n": "b", "item": "ChoppedCucumber"}],
                  stations=[])
    assert R.find_ingredient(w, "Cucumber") is None


def test_find_ingredient_missing_is_none():
    assert R.find_ingredient(FakeWorld(stations=[]), "Lettuce") is None
    assert R.find_ingredient(FakeWorld(), None) is None
    assert R.find_ingredient(FakeWorld(), "") is None
```

- [ ] **Step 6: 运行测试，确认失败**

Run: `python3 -m pytest tests/test_recipes.py -v -k find_ingredient`
Expected: FAIL，`AttributeError: module 'ai.recipes' has no attribute 'find_ingredient'`

- [ ] **Step 7: 实现 `find_ingredient`**

追加到 `ai/recipes.py` 末尾：

```python
def find_ingredient(world, ing):
    # type: (object, Optional[str]) -> Optional[Dict]
    """定位食材当前位置，返回 {"kind": ..., "st"/"chef": ...}。

    kind 优先级：held（被人拿着）→ board（砧板上）→ plate（已装盘）
    → loose（散落）→ crate（箱子里）。优先级就是"离可用最近"的顺序：
    已经拿着的东西不该再去箱子取一份。

    **精确名匹配**，不做子串：Cucumber 与 ChoppedCucumber 是两种不同
    的料，宽松匹配会让"去拿生黄瓜"的 task 看到板上的切好黄瓜就以为
    料到手了。生/熟/切好的区分由调用方传对名字。

    与 oc/ai/task.py:find_ingredient 的差异：kind=="plate" 时**返回盘
    本身**（旧实现只返回 {"kind":"plate"}）。spec §6.0 要求谓词按绑定
    参数求值——两单都缺黄瓜时，必须能区分料在哪只盘里。
    """
    il = str(ing or "").strip().lower()
    if not il:
        return None

    for c in getattr(world, "chefs", None) or ():
        if P.held_name(c) == il:
            return {"kind": "held", "chef": c.get("name")}

    for ws in getattr(world, "workstations", None) or ():
        if str(ws.get("item") or "").lower() == il:
            return {"kind": "board", "st": ws}

    for p in getattr(world, "plates", None) or ():
        if il in P.plate_contents(p):
            return {"kind": "plate", "st": p}

    for it in getattr(world, "items", None) or ():
        if str(it.get("n") or "").lower() == il:
            return {"kind": "loose", "st": it}

    for st in getattr(world, "stations", None) or ():
        if not isinstance(st, dict) or st.get("t") != "crate":
            continue
        if str(st.get("ing") or "").lower() == il:
            return {"kind": "crate", "st": st}
    return None
```

- [ ] **Step 8: 运行测试，确认通过**

Run: `python3 -m pytest tests/test_recipes.py -v`
Expected: PASS，27 个测试全绿。

- [ ] **Step 9: 确认没带进 CHOPPABLE 硬编码表**

Run: `grep -rn "CHOPPABLE" ai/`
Expected: 无输出（spec §9 要求随 `task.py` 拆解删除）。

- [ ] **Step 10: 运行全量测试**

Run: `python3 -m pytest -v`
Expected: 全绿。

- [ ] **Step 11: 提交**

```bash
git add ai/recipes.py tests/test_recipes.py
git commit -m "feat(recipes): 配方语义，切配信息改从箱子数据读

从 oc/ai/task.py 抽取，三处改动：

1. 删掉 CHOPPABLE 硬编码表（{"SushiFish","Cucumber",...}）。spec §9 记
   它"已泛化（主路径读 crate 的 chop/chop_stages），该表是遗留，随
   task.py 拆解删除"。chop_target 从箱子的 chop 字段读——实测
   {"ing":"Cucumber","chop":"ChoppedCucumber","chop_stages":8}。
   spec §7.3 点名前缀匹配 item == Chopped* 是初版误判源：产物名不以
   Chopped 开头时会无限切下去。

2. RecipeBook 一次构造整帧复用。旧 find_* 每次调用重建 {name: recipe}
   索引，而新架构里每个 task 的 feasible/done 每拍要问好几次。

3. find_ingredient 在 kind=="plate" 时返回盘本身（旧实现只返回
   {"kind":"plate"}）。spec §6.0 要求谓词按绑定参数求值——两单都缺
   黄瓜时必须能区分料在哪只盘里。

decompose() 没有移植：spec §8.2 明确"decompose() 删除"，它输出的线性
步骤序列正是 §6.2 要换掉的东西（执行顺序从可行性涌现，不预先排序）。

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 6: 落点约束 `placement.py`

**Files:**
- Create: `ai/placement.py`
- Test: `tests/test_placement.py`

**Interfaces:**
- Consumes: `navigation.distance_field` / `nearest_cost`（Plan 1）、`navigation.interact_cells`（Plan 1）
- Produces:
  - `forbidden_adjacent_cells(stations)` → `Set[Tuple[int,int]]`：不可作落点的格（贴灶台 / 出餐口 / 拼装台的四邻）
  - `find_drop_station(world, field, extra_forbidden=None)` → `Optional[Dict]`：就近的合法空台面站点
  - `find_drop_cell(world, field, extra_forbidden=None)` → `Optional[Tuple[int,int]]`：地板兜底落点

**spec §6.1 原话**："现有 `_find_drop_cell`（`agent.py:483`）的筛选逻辑是**功能性的、不是优化**，必须保留——落点须可落脚、非传送带，且不贴灶台（会继续加热）、不贴出餐口（会被误上菜）、不贴拼装台（会挡盘）。初版只写'清空手'会丢掉这些约束。"

**本任务对旧实现的一处升级**：spec §7.4 要求"放**就近的合法空台面**而非地上（地上虽也能捡，但可能挡路或在传送带上被冲走）"。旧 `_find_drop_cell` 只找地板格。故先找空 `counter` 站点（`find_drop_station`），找不到才退到地板格（`find_drop_cell`）。

**不含 `find_plate_counter` / `PLATE_COUNTERS`**：spec §9 明确归 Plan 3（"替代品是 Planner 的多拼装位候选生成 + 认领，不是一个 find 函数"）。所以这里的"拼装台"禁区只能从**已就位的盘**位置推——盘在哪格，那格就是在用的拼装位。

- [ ] **Step 1: 写失败测试**

创建 `tests/test_placement.py`：

```python
"""落点约束单测（spec §6.1 / §7.4）。

旧 _find_drop_cell 的筛选逻辑是功能性的不是优化：贴灶台会继续加热、
贴出餐口会被误上菜、贴拼装台会挡盘。这些断言就是那三条。
"""
from ai import placement
from ai.navigation import distance_field


class FakeWorld(object):
    """只提供 placement 要用的几个属性。"""

    def __init__(self, stations=None, plates=None, belt_cells=None,
                 belt_dir=None, walkable=None):
        self.stations = stations or []
        self.plates = plates or []
        self.belt_cells = set(belt_cells or ())
        self.belt_dir = belt_dir or {}
        self._walk = walkable or (lambda gx, gz: True)

    def walkable(self, gx, gz):
        return self._walk(gx, gz)


def test_forbidden_includes_cooker_neighbors():
    """贴灶台的格不可落 —— 锅放那儿会继续加热。"""
    sts = [{"t": "cooking", "n": "cooker1", "gx": 5, "gz": 0}]
    bad = placement.forbidden_adjacent_cells(sts)
    assert (4, 0) in bad and (6, 0) in bad
    assert (5, 1) in bad and (5, -1) in bad
    assert (3, 0) not in bad


def test_forbidden_includes_plate_station_neighbors():
    """贴出餐口的格不可落 —— 东西放那儿会被误上菜。"""
    sts = [{"t": "plate_station", "n": "window", "gx": 9, "gz": 0}]
    bad = placement.forbidden_adjacent_cells(sts)
    assert (8, 0) in bad


def test_forbidden_skips_ordinary_counters():
    """普通台面不是禁区 —— 否则整张图都落不下。"""
    sts = [{"t": "counter", "n": "c", "gx": 2, "gz": 2}]
    assert placement.forbidden_adjacent_cells(sts) == set()


def test_forbidden_skips_stations_without_grid():
    """实测 71 个站点里 10 个没有 gx/gz，不许崩。"""
    sts = [{"t": "cooking", "n": "c"},
           {"t": "plate_station", "n": "w", "gx": None, "gz": None}]
    assert placement.forbidden_adjacent_cells(sts) == set()


def test_find_drop_station_prefers_empty_counter():
    """spec §7.4：优先放就近的合法空台面，不是地上。"""
    sts = [
        {"t": "counter", "n": "near", "gx": 1, "gz": 0, "x": 1.2, "z": 0.0},
        {"t": "counter", "n": "far", "gx": 6, "gz": 0, "x": 7.2, "z": 0.0},
    ]
    w = FakeWorld(stations=sts, walkable=lambda gx, gz: gz == 0)
    field = distance_field((0, 0), w.walkable, {})
    got = placement.find_drop_station(w, field)
    assert got is not None and got["n"] == "near"


def test_find_drop_station_skips_occupied_counter():
    """台面上已有盘 → 不能往上放（会把盘顶掉或放不下）。"""
    sts = [
        {"t": "counter", "n": "busy", "gx": 1, "gz": 0, "x": 1.2, "z": 0.0},
        {"t": "counter", "n": "free", "gx": 3, "gz": 0, "x": 3.6, "z": 0.0},
    ]
    plates = [{"n": "p1", "x": 1.2, "z": 0.0, "count": 2,
               "contents": ["Seaweed", "Cucumber"]}]
    w = FakeWorld(stations=sts, plates=plates,
                  walkable=lambda gx, gz: gz == 0)
    field = distance_field((0, 0), w.walkable, {})
    got = placement.find_drop_station(w, field)
    assert got is not None and got["n"] == "free"


def test_find_drop_station_skips_assembly_counter():
    """盘所在的那格就是在用的拼装位 → 它的四邻也是禁区（会挡盘）。

    PLATE_COUNTERS 硬编码表归 Plan 3 删/替（spec §9），所以这里只能从
    已就位的盘反推拼装位。
    """
    sts = [
        {"t": "counter", "n": "beside_plate", "gx": 1, "gz": 0,
         "x": 1.2, "z": 0.0},
        {"t": "counter", "n": "clear", "gx": 4, "gz": 0, "x": 4.8, "z": 0.0},
    ]
    plates = [{"n": "p1", "x": 2.4, "z": 0.0, "count": 1,
               "contents": ["Seaweed"]}]
    w = FakeWorld(stations=sts, plates=plates,
                  walkable=lambda gx, gz: gz == 0)
    field = distance_field((0, 0), w.walkable, {})
    got = placement.find_drop_station(w, field)
    assert got is not None and got["n"] == "clear"


def test_find_drop_station_unreachable_is_none():
    sts = [{"t": "counter", "n": "island", "gx": 50, "gz": 50,
            "x": 60.0, "z": 60.0}]
    w = FakeWorld(stations=sts, walkable=lambda gx, gz: gz == 0 and gx < 5)
    field = distance_field((0, 0), w.walkable, {})
    assert placement.find_drop_station(w, field) is None


def test_find_drop_station_honors_extra_forbidden():
    """调用方可追加禁区（如正在灭的那口灶周边）。"""
    sts = [{"t": "counter", "n": "c", "gx": 1, "gz": 0, "x": 1.2, "z": 0.0}]
    w = FakeWorld(stations=sts, walkable=lambda gx, gz: gz == 0)
    field = distance_field((0, 0), w.walkable, {})
    assert placement.find_drop_station(w, field) is not None
    assert placement.find_drop_station(
        w, field, extra_forbidden={(1, 0)}) is None


def test_find_drop_cell_floor_fallback():
    """没有合法空台面时退到地板格。"""
    w = FakeWorld(walkable=lambda gx, gz: gz == 0 and 0 <= gx <= 3)
    field = distance_field((0, 0), w.walkable, {})
    cell = placement.find_drop_cell(w, field)
    assert cell is not None
    assert w.walkable(cell[0], cell[1])


def test_find_drop_cell_avoids_belt():
    """传送带格不可落 —— 东西会被冲走（spec §7.4）。"""
    w = FakeWorld(belt_cells={(0, 0), (1, 0)},
                  belt_dir={(0, 0): (1, 0), (1, 0): (1, 0)},
                  walkable=lambda gx, gz: gz == 0 and 0 <= gx <= 3)
    field = distance_field((0, 0), w.walkable, {})
    cell = placement.find_drop_cell(w, field)
    assert cell not in ((0, 0), (1, 0))


def test_find_drop_cell_avoids_forbidden_adjacency():
    sts = [{"t": "cooking", "n": "cooker", "gx": 2, "gz": 0}]
    w = FakeWorld(stations=sts,
                  walkable=lambda gx, gz: gz == 0 and 0 <= gx <= 4)
    field = distance_field((0, 0), w.walkable, {})
    cell = placement.find_drop_cell(w, field)
    assert cell not in ((1, 0), (3, 0)), "不该落在贴灶台的格上"


def test_find_drop_cell_none_when_nothing_legal():
    """全图都是禁区 → None。调用方据此转 FAILURE，不是无限找。"""
    sts = [{"t": "cooking", "n": "c", "gx": 1, "gz": 0}]
    w = FakeWorld(stations=sts, walkable=lambda gx, gz: (gx, gz) in
                  ((0, 0), (2, 0)))
    field = distance_field((0, 0), w.walkable, {})
    assert placement.find_drop_cell(w, field) is None
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `python3 -m pytest tests/test_placement.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'ai.placement'`

- [ ] **Step 3: 实现 `ai/placement.py`**

```python
"""落点约束：手里的东西该放哪（spec §6.1 / §7.4）。

从 oc/ai/agent.py:483 的 _find_drop_cell 抽取。spec §6.1 原话：那段
筛选逻辑"是功能性的、不是优化，必须保留"——落点须可落脚、非传送带，
且不贴灶台（会继续加热）、不贴出餐口（会被误上菜）、不贴拼装台（会挡盘）。

一处升级：spec §7.4 要求"放**就近的合法空台面**而非地上（地上虽也能捡，
但可能挡路或在传送带上被冲走）"。旧实现只找地板格，这里先找空 counter
站点（find_drop_station），找不到才退到地板（find_drop_cell）。

不含 find_plate_counter / PLATE_COUNTERS：spec §9 明确归 Plan 3（替代品
是 Planner 的多拼装位候选生成 + 认领，不是一个 find 函数）。所以"拼装台"
禁区只能从**已就位的盘**位置反推——盘在哪格，那格就是在用的拼装位。
"""
from typing import Dict, Optional, Set, Tuple

from ai import navigation
from ai.occupancy import _POS_ROUND

# 这两类站点的四邻不可作落点：灶台会继续加热，出餐口会把东西当成上菜
_HOT_STATION_TYPES = ("cooking", "plate_station")

_NEIGHBORS = ((-1, 0), (1, 0), (0, -1), (0, 1))


def _grid_of(st):
    # type: (Dict) -> Optional[Tuple[int, int]]
    """站点网格坐标。缺失返回 None——实测 71 个站点里 10 个没有 gx/gz。"""
    gx, gz = st.get("gx"), st.get("gz")
    if gx is None or gz is None:
        return None
    return (int(gx), int(gz))


def forbidden_adjacent_cells(stations):
    # type: (Optional[list]) -> Set[Tuple[int, int]]
    """灶台 / 出餐口四邻的禁区格。

    只收这两类：普通台面若也算禁区，整张图就落不下任何东西了。
    """
    bad = set()  # type: Set[Tuple[int, int]]
    for st in stations or ():
        if not isinstance(st, dict) or st.get("t") not in _HOT_STATION_TYPES:
            continue
        cell = _grid_of(st)
        if cell is None:
            continue
        for dx, dz in _NEIGHBORS:
            bad.add((cell[0] + dx, cell[1] + dz))
    return bad


def _plate_cells(world):
    # type: (object) -> Set[Tuple[float, float]]
    """盘所在位置（世界坐标，量化后）。盘在哪就是在用的拼装位。"""
    out = set()  # type: Set[Tuple[float, float]]
    for p in getattr(world, "plates", None) or ():
        if not isinstance(p, dict):
            continue
        x, z = p.get("x"), p.get("z")
        if x is None or z is None:
            continue
        out.add((round(x, _POS_ROUND), round(z, _POS_ROUND)))
    return out


def _assembly_forbidden(world):
    # type: (object) -> Set[Tuple[int, int]]
    """已就位的盘所在格 + 其四邻（会挡盘）。"""
    bad = set()  # type: Set[Tuple[int, int]]
    plates = _plate_cells(world)
    if not plates:
        return bad
    for st in getattr(world, "stations", None) or ():
        if not isinstance(st, dict):
            continue
        x, z = st.get("x"), st.get("z")
        cell = _grid_of(st)
        if cell is None or x is None or z is None:
            continue
        if (round(x, _POS_ROUND), round(z, _POS_ROUND)) not in plates:
            continue
        bad.add(cell)
        for dx, dz in _NEIGHBORS:
            bad.add((cell[0] + dx, cell[1] + dz))
    return bad


def _occupied_station_cells(world):
    # type: (object) -> Set[Tuple[int, int]]
    """台面上已有盘的站点格——往上放会顶掉盘或放不下。"""
    plates = _plate_cells(world)
    out = set()  # type: Set[Tuple[int, int]]
    if not plates:
        return out
    for st in getattr(world, "stations", None) or ():
        if not isinstance(st, dict):
            continue
        cell = _grid_of(st)
        x, z = st.get("x"), st.get("z")
        if cell is None or x is None or z is None:
            continue
        if (round(x, _POS_ROUND), round(z, _POS_ROUND)) in plates:
            out.add(cell)
    return out


def find_drop_station(world, field, extra_forbidden=None):
    # type: (object, Dict, Optional[Set[Tuple[int, int]]]) -> Optional[Dict]
    """就近的合法空台面站点。按距离场代价取最近，不可达的跳过。

    field 是从厨师位置算出的 distance_field，所以"最近"按真实路径代价
    算而不是直线距离——隔着墙的台面不该被选中。
    """
    forbidden = set(extra_forbidden or ())
    forbidden |= forbidden_adjacent_cells(getattr(world, "stations", None))
    forbidden |= _assembly_forbidden(world)
    occupied = _occupied_station_cells(world)
    belt_cells = getattr(world, "belt_cells", None) or set()

    best, best_cost = None, None
    for st in getattr(world, "stations", None) or ():
        if not isinstance(st, dict) or st.get("t") != "counter":
            continue
        cell = _grid_of(st)
        if cell is None or cell in forbidden or cell in occupied:
            continue
        cells = navigation.interact_cells(
            cell[0], cell[1], world.walkable, belt_cells)
        got = navigation.nearest_cost(field, cells)
        if got is None:
            continue
        if best_cost is None or got[1] < best_cost:
            best, best_cost = st, got[1]
    return best


def find_drop_cell(world, field, extra_forbidden=None):
    # type: (object, Dict, Optional[Set[Tuple[int, int]]]) -> Optional[Tuple[int, int]]
    """地板兜底落点：可走、非传送带、不在禁区，按距离场取最近。

    返回 None 表示全图无合法落点。调用方据此转 FAILURE 交还 Planner，
    不在这里无限找（spec §7.6 纪律 1）。
    """
    forbidden = set(extra_forbidden or ())
    forbidden |= forbidden_adjacent_cells(getattr(world, "stations", None))
    forbidden |= _assembly_forbidden(world)
    belt_dir = getattr(world, "belt_dir", None) or {}
    belt_cells = getattr(world, "belt_cells", None) or set()

    best, best_cost = None, None
    for cell, cost in field.items():
        if cell in forbidden or cell in belt_cells or cell in belt_dir:
            continue
        if best_cost is None or cost < best_cost:
            best, best_cost = cell, cost
    return best
```

- [ ] **Step 4: 运行测试，确认通过**

Run: `python3 -m pytest tests/test_placement.py -v`
Expected: PASS，13 个测试全绿。

- [ ] **Step 5: 真实帧上确认能找到落点**

追加到 `tests/test_placement.py`：

```python
def test_find_drop_on_real_frame(level_frame):
    """真实帧：既能找到空台面，也能找到地板落点。

    若两者都 None，说明禁区规则把整张图筛空了——那 EnsureHandsFree
    会永远 FAILURE，所有中断 task 都起不来。
    """
    from ai.world import World
    w = World(level_frame)
    chef = None
    for c in w.chefs:
        if c.get("x") is not None:
            chef = c
            break
    if chef is None:
        pytest.skip("样本里没有带世界坐标的厨师")
    field = distance_field(w.to_grid(chef["x"], chef["z"]),
                           w.walkable, w.belt_dir)

    st = placement.find_drop_station(w, field)
    cell = placement.find_drop_cell(w, field)
    assert st is not None or cell is not None, \
        "真实帧上一个落点都找不到，禁区规则过严"
    if cell is not None:
        assert w.walkable(cell[0], cell[1])
        assert cell not in w.belt_cells
```

文件头补 `import pytest`。

- [ ] **Step 6: 运行测试，确认通过**

Run: `python3 -m pytest tests/test_placement.py -v`
Expected: PASS，14 个测试全绿。

- [ ] **Step 7: 运行全量测试**

Run: `python3 -m pytest -v`
Expected: 全绿。

- [ ] **Step 8: 提交**

```bash
git add ai/placement.py tests/test_placement.py
git commit -m "feat(placement): 落点约束，空台面优先于地板

从 oc/ai/agent.py:483 的 _find_drop_cell 抽取。spec §6.1 原话：那段筛选
逻辑"是功能性的、不是优化，必须保留"——贴灶台会继续加热、贴出餐口会被
误上菜、贴拼装台会挡盘、传送带格上的东西会被冲走。

一处升级：spec §7.4 要求放"就近的合法空台面"而非地上（地上虽也能捡，
但可能挡路或被传送带冲走）。旧实现只找地板格，这里先找空 counter
站点，找不到才退到地板。

"最近"按 distance_field 的路径代价算而非直线距离——隔着墙的台面不该
被选中。

拼装台禁区从已就位的盘位置反推，不用 PLATE_COUNTERS 硬编码表：
spec §9 明确它归 Plan 3（替代品是 Planner 的候选生成 + 认领）。

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---
### Task 7: 原语基类 + `Approach` + `Face`

**Files:**
- Create: `ai/primitives.py`
- Test: `tests/test_primitives.py`

**Interfaces:**
- Consumes: `bt.Node` / `bt.Status`（Task 1）、`hands.Hands` / `hands.DIR2D`（Task 2）、`navigation.first_step`（Task 3）、`predicates.interact_ready` / `facing_cell` / `chef_cell`（Task 4）、`navigation.interact_cells` / `nearest_cost`（Plan 1）
- Produces:
  - **`ctx` 契约**（整个执行层共用）：
    ```
    ctx = {
        "w":     World,          # 本拍快照
        "chef":  Dict,           # P2 的 chefs[] 条目
        "hands": Hands,          # 按键出口
        "bb":    Blackboard,     # 只读 claims，可写 scratch（spec §4.5）
        "field": Dict,           # 从 chef 格算出的 distance_field
        "book":  RecipeBook,     # 配方视图
        "tick":  int,            # 当前拍号
    }
    ```
  - `Primitive(bt.Node)` 基类：`halt()` 默认松开本节点按住的键并清移动方向；`reset()` 清边沿状态
  - `resolve(target, ctx)` → `Optional[Dict]`：target 可以是 dict（静态）或 `ctx -> Optional[Dict]` 的可调用（每拍重解析）
  - `Approach(target, kind=None)`：走到交互位。**只保证站位，不保证朝向**（spec §7.2）
  - `Face(target)`：转向目标

**target 为什么是可调用**（Review Focus 5）：锅被 P1 端走、盘被端走、灭火器被捡走——目标在动作中途消失是常态。树是跨拍复用的对象（spec §7.6 "task 身份与上拍相同 → 复用同一棵树"），构造时抓一个 dict 快照，第二拍它就是过期数据。所以 target 传解析函数，每拍重算；解析不出来 → FAILURE 交还 Planner。

**`Approach` 的到位判据**：`predicates.interact_ready`，即游戏自己的 `placement_gx/gz` / `pickup_gx/gz` 信号（spec §7.5 第一类故障）。spec §7.5 另记一笔："现有 `settle_*` 稳定拍被 `placement_gx/gz` 前置校验取代"——`Approach` 不等物理稳定，直接查交互格。

- [ ] **Step 1: 写失败测试——基类与 target 解析**

创建 `tests/test_primitives.py`：

```python
"""原语单测。

全程假 controller + 假 World，零真实按键注入——Plan 2 的交付标准。
"""
import pytest

from ai import primitives as PR
from ai.bt import Status
from ai.hands import Hands
from ai.navigation import distance_field
from ai.recipes import RecipeBook
from ai.blackboard import Blackboard


class FakeCtrl(object):
    def __init__(self):
        self.calls = []

    def tap(self, name, hold=0.12):
        self.calls.append(("tap", name))

    def press(self, name):
        self.calls.append(("press", name))

    def release(self, name):
        self.calls.append(("release", name))

    def release_all(self):
        self.calls.append(("release_all",))


class FakeWorld(object):
    """只提供原语要用的属性。"""

    def __init__(self, stations=None, plates=None, workstations=None,
                 cookers=None, items=None, chefs=None, recipes=None,
                 belt_cells=None, belt_dir=None, walkable=None, cell=1.2):
        self.stations = stations or []
        self.plates = plates or []
        self.workstations = workstations or []
        self.cookers = cookers or []
        self.items = items or []
        self.chefs = chefs or []
        self.recipes = recipes or []
        self.belt_cells = set(belt_cells or ())
        self.belt_dir = belt_dir or {}
        self._walk = walkable or (lambda gx, gz: True)
        self._cell = cell

    def walkable(self, gx, gz):
        return self._walk(gx, gz)

    def to_grid(self, x, z):
        return (int(round(x / self._cell)), int(round(z / self._cell)))

    def to_world(self, gx, gz):
        return (gx * self._cell, gz * self._cell)


def make_ctx(w=None, chef=None, start=(0, 0), tick=0, bb=None):
    """装配一个 ctx。walkable 默认整行 gz==0 可走。"""
    w = w if w is not None else FakeWorld(
        walkable=lambda gx, gz: gz == 0 and -2 <= gx <= 8)
    chef = chef if chef is not None else {
        "name": "Player 2", "x": start[0] * 1.2, "z": start[1] * 1.2,
        "held": None,
        "interactions": {"interactable": None, "pickup": None,
                         "placement": None, "grid": None,
                         "gx": None, "gz": None,
                         "pickup_gx": None, "pickup_gz": None,
                         "placement_gx": None, "placement_gz": None},
    }
    ctrl = FakeCtrl()
    return {
        "w": w,
        "chef": chef,
        "hands": Hands(ctrl),
        "bb": bb if bb is not None else Blackboard(),
        "field": distance_field(start, w.walkable, w.belt_dir),
        "book": RecipeBook(w.recipes, w.stations),
        "tick": tick,
    }, ctrl


def _inter(**kw):
    base = {"interactable": None, "pickup": None, "placement": None,
            "grid": None, "gx": None, "gz": None,
            "pickup_gx": None, "pickup_gz": None,
            "placement_gx": None, "placement_gz": None}
    base.update(kw)
    return base


# ---------- target 解析 ----------

def test_resolve_static_dict():
    ctx, _ = make_ctx()
    st = {"n": "x", "gx": 1, "gz": 0}
    assert PR.resolve(st, ctx) is st


def test_resolve_callable_is_called_each_tick():
    """Review Focus 5：target 每拍重解析，不缓存构造时的快照。

    树是跨拍复用的对象（spec §7.6），构造时抓的 dict 第二拍就是过期
    数据——锅被端走、盘被端走都会让它失真。
    """
    ctx, _ = make_ctx()
    seen = []

    def resolver(c):
        seen.append(c["tick"])
        return {"n": "x", "gx": 1, "gz": 0}

    PR.resolve(resolver, ctx)
    ctx["tick"] = 1
    PR.resolve(resolver, ctx)
    assert seen == [0, 1]


def test_resolve_none_and_missing():
    ctx, _ = make_ctx()
    assert PR.resolve(None, ctx) is None
    assert PR.resolve(lambda c: None, ctx) is None


# ---------- Approach ----------

def test_approach_already_in_position_is_success():
    """交互信号已指向目标 → 立刻 SUCCESS，不多走一步。"""
    chef = {"name": "Player 2", "x": 0.0, "z": 0.0, "held": None,
            "interactions": _inter(placement_gx=1, placement_gz=0)}
    ctx, ctrl = make_ctx(chef=chef)
    node = PR.Approach({"n": "c", "gx": 1, "gz": 0}, "placement")
    assert node.tick(ctx) == Status.SUCCESS
    assert ("press", "right") not in ctrl.calls


def test_approach_moves_toward_target():
    """未到位 → RUNNING 且按住朝目标的方向键。"""
    ctx, ctrl = make_ctx(start=(0, 0))
    node = PR.Approach({"n": "c", "gx": 5, "gz": 0}, "placement")
    assert node.tick(ctx) == Status.RUNNING
    assert ctx["hands"].move_dir == "right"
    assert ("press", "right") in ctrl.calls


def test_approach_stops_pressing_on_arrival():
    """到位那一拍必须松开移动键，否则会滑过交互位。"""
    ctx, ctrl = make_ctx(start=(0, 0))
    node = PR.Approach({"n": "c", "gx": 5, "gz": 0}, "placement")
    node.tick(ctx)
    assert ctx["hands"].move_dir == "right"
    ctx["chef"]["interactions"] = _inter(placement_gx=5, placement_gz=0)
    assert node.tick(ctx) == Status.SUCCESS
    assert ctx["hands"].move_dir is None


def test_approach_unreachable_is_failure():
    """不可达 → FAILURE（spec §7.2），交还 Planner 重算。"""
    w = FakeWorld(walkable=lambda gx, gz: gz == 0 and 0 <= gx <= 2)
    ctx, _ = make_ctx(w=w)
    node = PR.Approach({"n": "island", "gx": 40, "gz": 40}, "placement")
    assert node.tick(ctx) == Status.FAILURE


def test_approach_target_gone_is_failure():
    """Review Focus 5：目标中途消失 → FAILURE，不抛 AttributeError。"""
    ctx, _ = make_ctx()
    node = PR.Approach(lambda c: None, "pickup")
    assert node.tick(ctx) == Status.FAILURE


def test_approach_target_without_grid_is_failure():
    """站点无 gx/gz 且无世界坐标 → 无法定位，FAILURE。"""
    ctx, _ = make_ctx()
    assert PR.Approach({"n": "nowhere"}, "pickup").tick(ctx) == Status.FAILURE


def test_approach_derives_cell_from_world_coords():
    """动态物（灭火器）只有 x/z：用 World.to_grid 换算。"""
    ctx, _ = make_ctx(start=(0, 0))
    ext = {"n": "utensil_fire_extinguisher_01", "x": 3 * 1.2, "z": 0.0}
    node = PR.Approach(ext, "pickup")
    assert node.tick(ctx) == Status.RUNNING
    assert ctx["hands"].move_dir == "right"


def test_approach_presses_belt_counter_key():
    """站在传送带上横穿时同时顶住反向键，否则被推走（spec §7.2）。"""
    w = FakeWorld(walkable=lambda gx, gz: -2 <= gx <= 8 and -1 <= gz <= 1,
                  belt_cells={(0, 0)}, belt_dir={(0, 0): (1, 0)})
    ctx, ctrl = make_ctx(w=w, start=(0, 0))
    node = PR.Approach({"n": "c", "gx": 0, "gz": 4}, "placement")
    node.tick(ctx)
    assert ctx["hands"].move_dir == "up"
    assert ctx["hands"].move_extra == "left", "往上穿越往右推的带，要顶左"


def test_approach_halt_releases_movement():
    """halt 只松键（spec §7.6 纪律 2）。"""
    ctx, ctrl = make_ctx(start=(0, 0))
    node = PR.Approach({"n": "c", "gx": 5, "gz": 0}, "placement")
    node.tick(ctx)
    node.halt()
    assert ctx["hands"].move_dir is None


def test_approach_respects_blocked_cells_from_scratch():
    """脱困拉黑的格子要绕开（MarkBlocked 写 scratch，Approach 读它）。"""
    from ai.blackboard import StuckScratch
    w = FakeWorld(walkable=lambda gx, gz: -1 <= gz <= 1 and 0 <= gx <= 4)
    bb = Blackboard()
    bb.scratch = StuckScratch(bad_cells={(1, 0): 999})
    ctx, _ = make_ctx(w=w, start=(0, 0), bb=bb)
    node = PR.Approach({"n": "c", "gx": 4, "gz": 0}, "placement")
    node.tick(ctx)
    assert ctx["hands"].move_dir in ("up", "down"), \
        "正前方被拉黑，该绕路而不是直撞"


# ---------- Face ----------

def test_face_already_facing_is_success():
    chef = {"name": "Player 2", "x": 0.0, "z": 0.0, "held": None,
            "interactions": _inter(gx=1, gz=0)}
    ctx, ctrl = make_ctx(chef=chef)
    node = PR.Face({"n": "c", "gx": 1, "gz": 0})
    assert node.tick(ctx) == Status.SUCCESS
    assert ctrl.calls == []


def test_face_presses_toward_target():
    """转向靠压方向键（移动方向 = 面向方向），这是旧 _fire_tick 的
    face_fire 子状态在做的事。"""
    ctx, ctrl = make_ctx(start=(0, 0))
    node = PR.Face({"n": "c", "gx": 1, "gz": 0})
    assert node.tick(ctx) == Status.RUNNING
    assert ctx["hands"].move_dir == "right"


def test_face_succeeds_once_interaction_grid_matches():
    ctx, _ = make_ctx(start=(0, 0))
    node = PR.Face({"n": "c", "gx": 1, "gz": 0})
    node.tick(ctx)
    ctx["chef"]["interactions"] = _inter(gx=1, gz=0)
    assert node.tick(ctx) == Status.SUCCESS
    assert ctx["hands"].move_dir is None


def test_face_target_gone_is_failure():
    ctx, _ = make_ctx()
    assert PR.Face(lambda c: None).tick(ctx) == Status.FAILURE


def test_face_timeout_is_failure():
    """转不过去（被挡住）→ 有限拍后 FAILURE，不无限压键。"""
    ctx, _ = make_ctx(start=(0, 0))
    node = PR.Face({"n": "c", "gx": 1, "gz": 0}, max_ticks=3)
    for _ in range(3):
        assert node.tick(ctx) == Status.RUNNING
    assert node.tick(ctx) == Status.FAILURE
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `python3 -m pytest tests/test_primitives.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'ai.primitives'`

- [ ] **Step 3: 实现基类 + `resolve` + `Approach` + `Face`**

创建 `ai/primitives.py`：

```python
"""执行原语：带强制前后校验的叶子节点（spec §7.2）。

一个复合原语 InteractWith（= Approach + Face + Tap + Confirm）替代今天
约 22 处逐字复制的 "goto → tap → confirm" 模板（实测 oc/ai/actions.py 里
confirm* 子状态分支 22 个、_goto* 调用点 22 个、confirm_wait < 2 重试
样板 17 处）。

与 oc/ai/actions.py 的根本差异（spec §7.1）：旧代码每个动作类内部手搓
一个字符串子状态机（sub = "goto_from" → "confirm_from" → "goto_to"
→ ...），而 bt.Sequence 本来就是干这个的。747 行里大量代码是在手工实现
"按顺序推进 + 记住推到哪"。这里顺序状态交给 BT，原语只管一步。

ctx 契约（整个执行层共用）：
    ctx = {
        "w":     World,          # 本拍快照
        "chef":  Dict,           # P2 的 chefs[] 条目
        "hands": Hands,          # 按键出口（唯一）
        "bb":    Blackboard,     # 只读 claims，可写 scratch（spec §4.5）
        "field": Dict,           # 从 chef 格算出的 distance_field
        "book":  RecipeBook,     # 配方视图
        "tick":  int,            # 当前拍号
    }

target 一律可以是 dict（静态）或 ctx -> Optional[Dict] 的可调用。**优先
传可调用**：树是跨拍复用的对象（spec §7.6"task 身份与上拍相同 → 复用
同一棵树"），构造时抓的 dict 第二拍就是过期数据——锅被 P1 端走、盘被
端走、灭火器被捡走都会让它失真。解析不出来 → FAILURE 交还 Planner
（spec §7.6 纪律 1：Executor 不写任何恢复逻辑）。
"""
from typing import Any, Callable, Dict, Optional, Set, Tuple

from ai import navigation
from ai import predicates as P
from ai.bt import Node, Status
from ai.hands import DIR2D

# Face 的默认上限拍数。旧 _fire_tick 用 face_ticks >= 2 就认为转好了，
# 但那是"压两拍必然转过去"的赌；这里以交互格信号为准、拍数只做兜底。
FACE_MAX_TICKS = 8

# "还没看过手里有什么"的哨兵。不能用 None 或 ""——held_name 对空手正是
# 返回 ""，两者混用会让"首次 tick"与"手本来就空"无法区分。
_UNSET = object()


def resolve(target, ctx):
    # type: (Any, Dict) -> Optional[Dict]
    """把 target 解析成本拍的 dict。可调用的每拍重算，不缓存。"""
    if target is None:
        return None
    if callable(target):
        got = target(ctx)
    else:
        got = target
    return got if isinstance(got, dict) and got else None


def _blocked_cells(ctx):
    # type: (Dict) -> Set[Tuple[int, int]]
    """脱困拉黑的格子（MarkBlocked 写进 scratch，这里读）。

    TTL 按拍计：值是失效拍号，当前拍 >= 它就不再算拉黑。
    """
    bb = ctx.get("bb")
    scratch = getattr(bb, "scratch", None)
    bad = getattr(scratch, "bad_cells", None)
    if not bad:
        return set()
    tick = ctx.get("tick") or 0
    return set(cell for cell, expire in bad.items() if expire > tick)


def target_cell(target, ctx):
    # type: (Optional[Dict], Dict) -> Optional[Tuple[int, int]]
    """目标所在格：优先 gx/gz，缺失时用 World.to_grid 从 x/z 换算。"""
    if not target:
        return None
    gx, gz = target.get("gx"), target.get("gz")
    if gx is not None and gz is not None:
        return (int(round(gx)), int(round(gz)))
    x, z = target.get("x"), target.get("z")
    if x is None or z is None:
        return None
    return ctx["w"].to_grid(x, z)


def _dir_towards(cur, nxt):
    # type: (Tuple[int, int], Tuple[int, int]) -> Optional[str]
    """cur → nxt 的方向键名。两格不相邻则 None。"""
    d = (nxt[0] - cur[0], nxt[1] - cur[1])
    for name, vec in DIR2D.items():
        if vec == d:
            return name
    return None


class Primitive(Node):
    """原语基类：halt 松键、reset 清边沿状态、手部连续性不变式。

    spec §7.6 纪律 2：halt 只松键并清移动方向，**不碰物品**。手里的东西
    交给抢占方树首的 EnsureHandsFree 处理——"优雅退出由抢占方负责前置
    条件，而非被打断方负责清理"（spec §7.4）。
    """

    def __init__(self):
        # type: () -> None
        self._hands = None   # type: Any
        self._ticks = 0
        self._held_seen = _UNSET   # type: Any

    def tick(self, ctx):
        raise NotImplementedError

    def reset(self):
        # type: () -> None
        self._ticks = 0
        self._held_seen = _UNSET

    def halt(self):
        # type: () -> None
        if self._hands is not None:
            self._hands.set_move(None)
        self.reset()

    # 子类在 tick 开头调，记下 hands 以便 halt 能松键
    def _enter(self, ctx):
        # type: (Dict) -> None
        self._hands = ctx.get("hands")
        self._ticks += 1

    def hands_changed(self, ctx):
        # type: (Dict) -> bool
        """手里的东西与上一拍不一致吗（spec §7.5 第三类故障）。

        spec §7.5 原话："**误放下 / 误拾**（没打算放却放了）→ 手部连续性
        不变式 → 每个原语开头确认'手里的东西与我以为的一致'，不一致说明
        中间发生意外 → 停下来让 Planner 按现实重算。"

        为什么是"与上一拍比"而不是"与期望比"：原语不知道自己的期望——
        期望写在 Confirm 的 pred 里，而那是动作**之后**的事。能在动作
        **之前**低成本检出的异常是"我没按键，手里的东西却变了"：被 P1
        换走、传送带冲掉、误拾。

        首次 tick 没有上一拍，一律返回 False（没有"不一致"可言）。
        调用方在确认后用 _remember_hands 记下本拍的值。
        """
        cur = P.held_name(ctx.get("chef"))
        if self._held_seen is _UNSET:
            return False
        return cur != self._held_seen

    def _remember_hands(self, ctx):
        # type: (Dict) -> None
        self._held_seen = P.held_name(ctx.get("chef"))


class Approach(Primitive):
    """走到能跟 target 交互的位置。

    到位判据是 predicates.interact_ready，即游戏自己的 placement_gx/gz
    或 pickup_gx/gz 信号——AGENTS.md §6 称其为"能否交互的最精确信号"。

    spec §7.5 显式记一笔：**现有 settle_* 稳定拍被这个前置校验取代**。
    旧 actions.py:100 的 settle_counter / :314 的 settle_to 是"走到位后
    等一拍再按键"的缓冲，赌的是物理稳定了；查交互格是确认真的能作用到
    目标，比等待可靠。

    **只保证站位，不保证朝向**（spec §7.2）。需要定向交互的地方必须显式
    带 Face——灭火必须面朝火源才喷得到。
    """

    def __init__(self, target, kind=None):
        # type: (Any, Optional[str]) -> None
        Primitive.__init__(self)
        self.target = target
        self.kind = kind

    def tick(self, ctx):
        # type: (Dict) -> str
        self._enter(ctx)
        w, chef, hands = ctx["w"], ctx["chef"], ctx["hands"]
        st = resolve(self.target, ctx)
        if st is None:
            hands.set_move(None)
            return Status.FAILURE

        if P.interact_ready(chef, st, self.kind, w.to_grid):
            hands.set_move(None)      # 到位立刻松键，否则会滑过交互位
            return Status.SUCCESS

        cell = target_cell(st, ctx)
        if cell is None:
            hands.set_move(None)
            return Status.FAILURE

        cur = P.chef_cell(chef, w.to_grid)
        if cur is None:
            hands.set_move(None)
            return Status.FAILURE

        # 每拍重算距离场代价太贵（spec §6.4 要求每拍只 flood-fill 一次），
        # 但拉黑格是脱困期才有的少量格子——有拉黑时才重算一次。
        field = ctx["field"]
        blocked = _blocked_cells(ctx)
        if blocked:
            field = navigation.distance_field(
                cur, w.walkable, w.belt_dir, blocked)

        goals = navigation.interact_cells(
            cell[0], cell[1], w.walkable, w.belt_cells)
        if not goals:
            # 四邻全不可走：站点被围死。也允许直接站上目标格（地面物）
            goals = [cell]
        got = navigation.nearest_cost(field, goals)
        if got is None:
            hands.set_move(None)
            return Status.FAILURE      # 不可达 → 交还 Planner

        nxt = navigation.first_step(field, got[0], w.belt_dir)
        if nxt is None:
            # 已站在交互位上但 interact_ready 还没亮（朝向不对）→ 转向
            d = _dir_towards(cur, cell)
            if d is None:
                hands.set_move(None)
                return Status.FAILURE
            hands.set_move(d)
            return Status.RUNNING

        d = _dir_towards(cur, nxt)
        if d is None:
            hands.set_move(None)
            return Status.FAILURE
        # 站在传送带上横穿时顶住反向键，否则被推偏绕圈
        extra = None
        bd = w.belt_dir.get(cur)
        if bd is not None:
            dv = DIR2D[d]
            if dv[0] * bd[0] + dv[1] * bd[1] == 0:
                extra = hands.counter_dir(w.belt_dir, cur)
        hands.set_move(d, extra)
        return Status.RUNNING


class Face(Primitive):
    """转向 target。朝向以 interactions.gx/gz 为准。

    Approach 只管站位（spec §7.2 定稿），所以所有定向交互都要显式带这个
    节点。旧 _fire_tick 有专门的 face_fire 子状态 + face_ticks 计数，
    证明这是真实需求：灭火必须面朝火源才喷得到。

    转向手段就是压方向键——移动方向 = 面向方向。压到交互格信号指向目标
    为止；max_ticks 只是兜底，不作为"转好了"的判据（旧实现用
    face_ticks >= 2 当判据，那是赌两拍必然转过去）。
    """

    def __init__(self, target, max_ticks=FACE_MAX_TICKS):
        # type: (Any, int) -> None
        Primitive.__init__(self)
        self.target = target
        self.max_ticks = max_ticks

    def tick(self, ctx):
        # type: (Dict) -> str
        self._enter(ctx)
        w, chef, hands = ctx["w"], ctx["chef"], ctx["hands"]
        st = resolve(self.target, ctx)
        if st is None:
            hands.set_move(None)
            return Status.FAILURE

        cell = target_cell(st, ctx)
        if cell is None:
            hands.set_move(None)
            return Status.FAILURE

        if P.facing_cell(chef) == cell:
            hands.set_move(None)
            self.reset()
            return Status.SUCCESS

        if self._ticks > self.max_ticks:
            hands.set_move(None)
            self.reset()
            return Status.FAILURE

        cur = P.chef_cell(chef, w.to_grid)
        if cur is None:
            hands.set_move(None)
            return Status.FAILURE
        d = _dir_towards(cur, cell)
        if d is None:
            # 不相邻：按世界坐标的主轴差压方向（Approach 还在路上时的过渡）
            dx = cell[0] - cur[0]
            dz = cell[1] - cur[1]
            if abs(dx) >= abs(dz):
                d = "right" if dx > 0 else "left"
            else:
                d = "up" if dz > 0 else "down"
        hands.set_move(d)
        return Status.RUNNING
```

- [ ] **Step 4: 运行测试，确认通过**

Run: `python3 -m pytest tests/test_primitives.py -v`
Expected: PASS，18 个测试全绿。

- [ ] **Step 5: 确认测试没碰真实 controller**

Run: `grep -rn "Quartz\|from ai.controller\|import controller" ai/primitives.py tests/test_primitives.py`
Expected: 无输出。

- [ ] **Step 6: 运行全量测试**

Run: `python3 -m pytest -v`
Expected: 全绿。

- [ ] **Step 7: 提交**

```bash
git add ai/primitives.py tests/test_primitives.py
git commit -m "feat(primitives): 原语基类 + Approach + Face

定下整个执行层的 ctx 契约：w / chef / hands / bb / field / book / tick。

target 一律可以是 dict 或 ctx -> Optional[Dict] 的可调用，且优先传可
调用：树是跨拍复用的对象（spec §7.6），构造时抓的 dict 第二拍就是过期
数据——锅被 P1 端走、盘被端走、灭火器被捡走都会让它失真。解析不出来
转 FAILURE 交还 Planner，不在执行层写恢复逻辑（spec §7.6 纪律 1）。

Approach 的到位判据是游戏自己的 placement_gx/gz / pickup_gx/gz 信号。
spec §7.5 显式记一笔：这取代了旧 actions.py 的 settle_* 稳定拍——
等一拍是赌物理稳定了，查交互格是确认真的能作用到目标。

Approach 只管站位、Face 单独管朝向（spec §7.2 定稿，初版两处自相矛盾）。
Face 以交互格信号为"转好了"的判据，max_ticks 只兜底；旧 _fire_tick 用
face_ticks >= 2 当判据，那是赌两拍必然转过去。

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---
### Task 8: `Tap` + `Confirm` + `Settle`

**Files:**
- Modify: `ai/primitives.py`（追加）
- Modify: `tests/test_primitives.py`（追加）

**Interfaces:**
- Consumes: `Primitive` / `resolve`（Task 7）、`Hands.tap`（Task 2）
- Produces:
  - `Tap(key, target=None, kind=None)`：边沿触发的单击。一次激活期内只按一次
  - `Confirm(pred, retries=2)`：后置校验。`pred` 是 `ctx -> bool`；不满足则有限重试后 FAILURE
  - `Settle(ticks)`：等状态刷新

**`Tap` 的边沿触发是 Review Focus 1**。三重原因叠加：
1. `bt.Sequence` 每拍从头 tick，已 SUCCESS 的子节点照旧被 tick（Task 1 已钉住这条语义）。
2. 拾取是 toggle（`oc/ai/agent.py:921` 注释"拾取是 toggle，连点会拿起又放下"）。
3. Python 循环 20 Hz 而插件推 5 Hz，同一帧会被决策 3-4 次（spec §3.4）。

所以 `Tap` 自己记"本次激活期已按过"，按完返回 SUCCESS 并保持该标记；标记由 `reset()`（被 `halt` 或 task 身份变化触发）清除。`Hands` 的 `TAP_COOLDOWN_TICKS` 是第二道闸——两个不同 `Tap` 节点先后按同一键时由它拦。

**`Confirm` 的后置校验必须双条件**（spec §7.5 第二类故障）："手里东西没了 **且** 目标位置真的出现了它。只查'手空了'不够（可能掉地上）。今天 `held_before` 字段名存实亡，是漏洞。" 双条件由调用方写进 `pred` —— `Confirm` 本身只负责"不满足则有限重试"。

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_primitives.py`：

```python
# ---------- Tap（Review Focus 1）----------

def test_tap_presses_once_and_succeeds():
    ctx, ctrl = make_ctx()
    node = PR.Tap("pickup")
    assert node.tick(ctx) == Status.SUCCESS
    assert ctrl.calls == [("tap", "pickup")]


def test_tap_does_not_repress_on_retick():
    """核心用例：Sequence 每拍从头 tick，Tap 第二次 tick 不许再按。

    拾取是 toggle（oc/ai/agent.py:921），再按一次 = 放下刚拿起的东西。
    """
    ctx, ctrl = make_ctx()
    node = PR.Tap("pickup")
    node.tick(ctx)
    ctx["hands"].tick()
    ctx["hands"].tick()
    ctx["hands"].tick()      # 冷却已过，但边沿标记还在
    assert node.tick(ctx) == Status.SUCCESS
    assert ctrl.calls.count(("tap", "pickup")) == 1


def test_tap_presses_again_after_reset():
    """reset（halt / task 身份变化）后允许重按——那是新的一次激活期。"""
    ctx, ctrl = make_ctx()
    node = PR.Tap("pickup")
    node.tick(ctx)
    node.reset()
    for _ in range(3):
        ctx["hands"].tick()
    node.tick(ctx)
    assert ctrl.calls.count(("tap", "pickup")) == 2


def test_tap_blocked_by_hands_cooldown_returns_running():
    """另一个节点刚按过同键 → 本节点这拍按不动，RUNNING 等下拍。

    返回 RUNNING 而不是 SUCCESS：谎称按成功会让 Sequence 直接走到
    Confirm，然后 Confirm 校验失败——多绕一圈且日志误导。
    """
    ctx, ctrl = make_ctx()
    ctx["hands"].tap("pickup")        # 别人先按了
    node = PR.Tap("pickup")
    assert node.tick(ctx) == Status.RUNNING
    assert ctrl.calls.count(("tap", "pickup")) == 1


def test_tap_releases_movement_before_pressing():
    """按键前先停移动——边滑边按会落空或串位（旧 _grab_burst 的做法）。"""
    ctx, _ = make_ctx()
    ctx["hands"].set_move("right")
    PR.Tap("pickup").tick(ctx)
    assert ctx["hands"].move_dir is None


def test_tap_with_target_requires_interact_ready():
    """带 target 的 Tap 在交互信号不对时 FAILURE，不瞎按。

    瞎按的后果是把手里的东西放到错误的格子上。
    """
    ctx, ctrl = make_ctx()
    node = PR.Tap("pickup", {"n": "c", "gx": 5, "gz": 0}, "placement")
    assert node.tick(ctx) == Status.FAILURE
    assert ctrl.calls == []


def test_tap_with_target_ready_presses():
    chef = {"name": "Player 2", "x": 0.0, "z": 0.0, "held": None,
            "interactions": _inter(placement_gx=1, placement_gz=0)}
    ctx, ctrl = make_ctx(chef=chef)
    node = PR.Tap("pickup", {"n": "c", "gx": 1, "gz": 0}, "placement")
    assert node.tick(ctx) == Status.SUCCESS
    assert ("tap", "pickup") in ctrl.calls


def test_tap_halt_clears_edge_flag():
    ctx, ctrl = make_ctx()
    node = PR.Tap("pickup")
    node.tick(ctx)
    node.halt()
    for _ in range(3):
        ctx["hands"].tick()
    node.tick(ctx)
    assert ctrl.calls.count(("tap", "pickup")) == 2


# ---------- Confirm ----------

def test_confirm_pred_true_is_success():
    ctx, _ = make_ctx()
    assert PR.Confirm(lambda c: True).tick(ctx) == Status.SUCCESS


def test_confirm_retries_then_fails():
    """不满足 → 先 RUNNING 等状态刷新，retries 用尽才 FAILURE。

    等待是必需的：插件 5 Hz 推帧，刚按下的那一拍世界还没更新。
    """
    ctx, _ = make_ctx()
    node = PR.Confirm(lambda c: False, retries=2)
    assert node.tick(ctx) == Status.RUNNING
    assert node.tick(ctx) == Status.RUNNING
    assert node.tick(ctx) == Status.FAILURE


def test_confirm_succeeds_mid_retry():
    ctx, _ = make_ctx()
    flips = [False, False, True]
    node = PR.Confirm(lambda c: flips.pop(0), retries=3)
    assert node.tick(ctx) == Status.RUNNING
    assert node.tick(ctx) == Status.RUNNING
    assert node.tick(ctx) == Status.SUCCESS


def test_confirm_reset_restarts_budget():
    ctx, _ = make_ctx()
    node = PR.Confirm(lambda c: False, retries=1)
    node.tick(ctx)
    assert node.tick(ctx) == Status.FAILURE
    node.reset()
    assert node.tick(ctx) == Status.RUNNING


def test_confirm_double_condition_catches_dropped_item():
    """spec §7.5 第二类故障：只查"手空了"不够，东西可能掉地上。

    双条件由调用方写进 pred；这个测试钉住"写对了就能拦住"。
    """
    plate = {"n": "p1", "x": 1.2, "z": 0.0, "contents": []}
    w = FakeWorld(plates=[plate])
    chef = {"name": "Player 2", "x": 0.0, "z": 0.0, "held": None,
            "interactions": _inter()}
    ctx, _ = make_ctx(w=w, chef=chef)

    def placed_in_plate(c):
        from ai import predicates as Pr
        return (Pr.hands_free(c["chef"])
                and "cucumber" in Pr.plate_contents(plate))

    assert PR.Confirm(placed_in_plate, retries=0).tick(ctx) == Status.FAILURE
    plate["contents"] = ["Cucumber"]
    assert PR.Confirm(placed_in_plate, retries=0).tick(ctx) == Status.SUCCESS


# ---------- Settle ----------

def test_settle_counts_down_then_succeeds():
    ctx, _ = make_ctx()
    node = PR.Settle(2)
    assert node.tick(ctx) == Status.RUNNING
    assert node.tick(ctx) == Status.RUNNING
    assert node.tick(ctx) == Status.SUCCESS


def test_settle_zero_is_immediate_success():
    ctx, _ = make_ctx()
    assert PR.Settle(0).tick(ctx) == Status.SUCCESS


def test_settle_reset_restarts():
    ctx, _ = make_ctx()
    node = PR.Settle(1)
    node.tick(ctx)
    assert node.tick(ctx) == Status.SUCCESS
    node.reset()
    assert node.tick(ctx) == Status.RUNNING


def test_settle_releases_movement():
    """等状态刷新期间不该还按着方向键。"""
    ctx, _ = make_ctx()
    ctx["hands"].set_move("right")
    PR.Settle(1).tick(ctx)
    assert ctx["hands"].move_dir is None
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `python3 -m pytest tests/test_primitives.py -v -k "tap or confirm or settle"`
Expected: FAIL，`AttributeError: module 'ai.primitives' has no attribute 'Tap'`

- [ ] **Step 3: 实现三个原语**

追加到 `ai/primitives.py`：

```python
class Tap(Primitive):
    """单击一次，边沿触发。

    为什么必须边沿触发（spec §7.2，三重原因叠加）：
      1. bt.Sequence 每拍从第 0 个子节点重新 tick，已 SUCCESS 的子节点
         照旧被 tick（见 tests/test_bt.py 的 retick 用例）。
      2. 游戏里拾取是 toggle——oc/ai/agent.py:921 注释原话"拾取是
         toggle，连点会拿起又放下"。
      3. Python 循环 20 Hz 而插件推 5 Hz，同一帧会被决策 3-4 次
         （spec §3.4），相邻拍输入完全相同。

    三者相乘的表现就是"反复拿起放下"。所以本节点自己记"这次激活期已
    按过"，标记由 reset() 清除（halt 或 task 身份变化时）。

    重试不靠反复 tick 自然重按，由 Confirm(retries) 显式控制
    （spec §7.2 规则 2）。

    带 target 时按键前做一次 interact_ready 校验：信号不对就 FAILURE，
    不瞎按——瞎按会把手里的东西放到错误的格子上。
    """

    def __init__(self, key, target=None, kind=None):
        # type: (str, Any, Optional[str]) -> None
        Primitive.__init__(self)
        self.key = key
        self.target = target
        self.kind = kind
        self._fired = False

    def reset(self):
        # type: () -> None
        Primitive.reset(self)
        self._fired = False

    def tick(self, ctx):
        # type: (Dict) -> str
        self._enter(ctx)
        hands = ctx["hands"]
        if self._fired:
            return Status.SUCCESS

        if self.target is not None:
            st = resolve(self.target, ctx)
            if st is None:
                return Status.FAILURE
            if not P.interact_ready(ctx["chef"], st, self.kind,
                                    ctx["w"].to_grid):
                return Status.FAILURE

        # 先停移动：边滑边按会落空或串位（旧 _grab_burst 同样先停）
        hands.set_move(None)
        if not hands.tap(self.key):
            # 冷却未到（另一个节点刚按过同键）。返回 RUNNING 而不是
            # SUCCESS——谎称按成功会让 Sequence 直接走到 Confirm，
            # 然后校验失败，多绕一圈且日志误导。
            return Status.RUNNING
        self._fired = True
        return Status.SUCCESS


class Confirm(Primitive):
    """后置校验：确认世界真的变了（spec §7.2 / §7.5）。

    pred 是 ctx -> bool。不满足时先 RUNNING 等状态刷新（插件 5 Hz 推帧，
    刚按下那拍世界还没更新），retries 用尽才 FAILURE。

    **双条件由调用方写进 pred**。spec §7.5 第二类故障原话："只查'手空了'
    不够（可能掉地上）。今天 held_before 字段名存实亡，是漏洞。"正确的
    pred 形如 `hands_free(chef) and item in plate_contents(plate)`。
    """

    def __init__(self, pred, retries=2):
        # type: (Callable[[Dict], bool], int) -> None
        Primitive.__init__(self)
        self.pred = pred
        self.retries = retries
        self._tries = 0

    def reset(self):
        # type: () -> None
        Primitive.reset(self)
        self._tries = 0

    def tick(self, ctx):
        # type: (Dict) -> str
        self._enter(ctx)
        if self.pred(ctx):
            self.reset()
            return Status.SUCCESS
        if self._tries >= self.retries:
            self.reset()
            return Status.FAILURE
        self._tries += 1
        return Status.RUNNING


class Settle(Primitive):
    """等 ticks 拍，让状态刷新。

    spec §7.5 定界：**只用于等状态刷新**（如倒料后等 contents 更新），
    不再用于等物理稳定——后者已被 Approach 的 placement_gx/gz 前置校验
    取代（查交互格比等一拍可靠）。
    """

    def __init__(self, ticks):
        # type: (int) -> None
        Primitive.__init__(self)
        self.ticks = ticks
        self._left = ticks

    def reset(self):
        # type: () -> None
        Primitive.reset(self)
        self._left = self.ticks

    def tick(self, ctx):
        # type: (Dict) -> str
        self._enter(ctx)
        ctx["hands"].set_move(None)
        if self._left <= 0:
            self.reset()
            return Status.SUCCESS
        self._left -= 1
        return Status.RUNNING
```

- [ ] **Step 4: 运行测试，确认通过**

Run: `python3 -m pytest tests/test_primitives.py -v`
Expected: PASS，35 个测试全绿。

- [ ] **Step 5: 运行全量测试**

Run: `python3 -m pytest -v`
Expected: 全绿。

- [ ] **Step 6: 提交**

```bash
git add ai/primitives.py tests/test_primitives.py
git commit -m "feat(primitives): Tap 边沿触发 + Confirm + Settle

Tap 的边沿触发是三重原因叠加的结果（spec §7.2）：
  1. bt.Sequence 每拍从头 tick，已 SUCCESS 的子节点照旧被 tick。
  2. 游戏里拾取是 toggle（oc/ai/agent.py:921 注释"连点会拿起又放下"）。
  3. Python 循环 20 Hz 而插件推 5 Hz，同一帧被决策 3-4 次（spec §3.4）。
三者相乘就是"反复拿起放下"。节点自记"本次激活期已按过"，reset 清除。

冷却被别的节点占住时返回 RUNNING 而非 SUCCESS：谎称按成功会让
Sequence 直接走到 Confirm 然后校验失败，多绕一圈且日志误导。

Confirm 不满足时先 RUNNING 等状态刷新（插件 5 Hz，刚按下那拍世界还
没更新），retries 用尽才 FAILURE。双条件由调用方写进 pred——spec §7.5
原话"只查'手空了'不够（可能掉地上），今天 held_before 字段名存实亡"。

Settle 只用于等状态刷新，不再等物理稳定：后者已被 Approach 的
placement_gx/gz 前置校验取代（spec §7.5）。

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 9: `HoldUntil` + `TapWhileHolding`

**Files:**
- Modify: `ai/primitives.py`（追加）
- Modify: `tests/test_primitives.py`（追加）

**Interfaces:**
- Consumes: `Primitive`（Task 7）、`Hands.hold` / `Hands.release` / `Hands.tap` / `Hands.counter_dir`（Task 2）
- Produces:
  - `HoldUntil(key, pred, timeout)`：按住到条件成立。`timeout` **无默认值，必须显式传**（spec §7.2）
  - `TapWhileHolding(tap_key, hold_key, target=None, kind=None)`：按住 `hold_key` 的同时单击 `tap_key`

**`HoldUntil` 的 `halt` 必须 release 是 Review Focus 2**：切菜/喷灭火器要按住多拍，中断（火情、脱困）来时若不松键，按键会卡死到进程退出。这是本项目已踩过的坑（`AI_REVIEW.md` Bug#2 加 `halt()` 的由来，spec §11.5 把"零残留按键"列为浸泡测试硬标准）。

**`TapWhileHolding` 是传送带的必需能力**（spec §7.2 原话"初版完全遗漏"）：在传送带格上拾取时必须同时按住反向键顶住，否则角色被传送带推走。现有 `_grab_burst`（`oc/ai/agent.py:920`）+ `_belt_counter`（`:947`）就是干这个的，而本关 `Sushi_1_4_2P` 有传送带——实跑踩出来的坑。

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_primitives.py`：

```python
# ---------- HoldUntil ----------

def test_hold_until_presses_and_runs():
    ctx, ctrl = make_ctx()
    node = PR.HoldUntil("chop", lambda c: False, timeout=5)
    assert node.tick(ctx) == Status.RUNNING
    assert ("press", "chop") in ctrl.calls
    assert "chop" in ctx["hands"].held_keys


def test_hold_until_releases_on_success():
    """条件成立 → 松键 + SUCCESS。不松键就是卡键。"""
    ctx, ctrl = make_ctx()
    flips = [False, True]
    node = PR.HoldUntil("chop", lambda c: flips.pop(0), timeout=5)
    node.tick(ctx)
    assert node.tick(ctx) == Status.SUCCESS
    assert ("release", "chop") in ctrl.calls
    assert ctx["hands"].held_keys == set()


def test_hold_until_immediate_success_does_not_press():
    """条件一开始就成立 → 不按键。切好的菜不该再切一刀。"""
    ctx, ctrl = make_ctx()
    assert PR.HoldUntil("chop", lambda c: True, timeout=5).tick(ctx) \
        == Status.SUCCESS
    assert ctrl.calls == []


def test_hold_until_timeout_releases_and_fails():
    """超时 → 松键 + FAILURE（spec §7.2）。"""
    ctx, ctrl = make_ctx()
    node = PR.HoldUntil("chop", lambda c: False, timeout=2)
    assert node.tick(ctx) == Status.RUNNING
    assert node.tick(ctx) == Status.RUNNING
    assert node.tick(ctx) == Status.FAILURE
    assert ("release", "chop") in ctrl.calls
    assert ctx["hands"].held_keys == set()


def test_hold_until_halt_releases_key():
    """Review Focus 2：被中断抢占时必须松键，否则按键卡死到进程退出。

    这是本项目已踩过的坑（AI_REVIEW.md Bug#2 加 halt() 的由来），
    spec §11.5 把"零残留按键"列为浸泡测试硬标准。
    """
    ctx, ctrl = make_ctx()
    node = PR.HoldUntil("chop", lambda c: False, timeout=50)
    node.tick(ctx)
    assert "chop" in ctx["hands"].held_keys
    node.halt()
    assert ("release", "chop") in ctrl.calls
    assert ctx["hands"].held_keys == set()


def test_hold_until_halt_then_retick_presses_again():
    """halt 后重新 tick 是新的一次按住，预算也重置。"""
    ctx, ctrl = make_ctx()
    node = PR.HoldUntil("chop", lambda c: False, timeout=2)
    node.tick(ctx)
    node.tick(ctx)
    node.halt()
    assert node.tick(ctx) == Status.RUNNING
    assert ctrl.calls.count(("press", "chop")) == 2


def test_hold_until_timeout_is_required():
    """spec §7.2："timeout 无默认值，必须显式传"。

    给默认值等于把"这个动作该等多久"的领域知识藏进原语里，
    切菜和喷灭火器的合理时长差一个量级。
    """
    with pytest.raises(TypeError):
        PR.HoldUntil("chop", lambda c: True)


def test_hold_until_releases_movement():
    ctx, _ = make_ctx()
    ctx["hands"].set_move("right")
    PR.HoldUntil("chop", lambda c: False, timeout=5).tick(ctx)
    assert ctx["hands"].move_dir is None


# ---------- TapWhileHolding（spec §7.2，传送带必需）----------

def test_tap_while_holding_presses_hold_then_taps():
    """顶住反向键的同时单击：hold 在 tap 之前，顺序不能反。"""
    ctx, ctrl = make_ctx()
    node = PR.TapWhileHolding("pickup", "left")
    assert node.tick(ctx) == Status.SUCCESS
    assert ctrl.calls.index(("press", "left")) < \
        ctrl.calls.index(("tap", "pickup"))


def test_tap_while_holding_releases_hold_after_tap():
    """单击完松开顶带键——继续按着会让角色往反方向走。"""
    ctx, ctrl = make_ctx()
    PR.TapWhileHolding("pickup", "left").tick(ctx)
    assert ("release", "left") in ctrl.calls
    assert ctx["hands"].held_keys == set()


def test_tap_while_holding_is_edge_triggered():
    ctx, ctrl = make_ctx()
    node = PR.TapWhileHolding("pickup", "left")
    node.tick(ctx)
    for _ in range(3):
        ctx["hands"].tick()
    assert node.tick(ctx) == Status.SUCCESS
    assert ctrl.calls.count(("tap", "pickup")) == 1


def test_tap_while_holding_derives_hold_key_from_belt():
    """hold_key 传可调用：站在往右推的带上 → 顶左。"""
    w = FakeWorld(belt_cells={(0, 0)}, belt_dir={(0, 0): (1, 0)},
                  walkable=lambda gx, gz: True)
    ctx, ctrl = make_ctx(w=w, start=(0, 0))
    node = PR.TapWhileHolding(
        "pickup",
        lambda c: c["hands"].counter_dir(
            c["w"].belt_dir, (0, 0)))
    node.tick(ctx)
    assert ("press", "left") in ctrl.calls


def test_tap_while_holding_without_hold_key_still_taps():
    """不在传送带上（hold_key 解析为 None）→ 退化为普通单击。"""
    ctx, ctrl = make_ctx()
    node = PR.TapWhileHolding("pickup", lambda c: None)
    assert node.tick(ctx) == Status.SUCCESS
    assert ("tap", "pickup") in ctrl.calls
    assert not any(c[0] == "press" for c in ctrl.calls)


def test_tap_while_holding_halt_releases():
    ctx, ctrl = make_ctx()
    node = PR.TapWhileHolding("pickup", "left")
    node.tick(ctx)
    node.halt()
    assert ctx["hands"].held_keys == set()


def test_tap_while_holding_target_not_ready_is_failure():
    ctx, ctrl = make_ctx()
    node = PR.TapWhileHolding("pickup", "left",
                              {"n": "c", "gx": 5, "gz": 0}, "pickup")
    assert node.tick(ctx) == Status.FAILURE
    assert ctrl.calls == []
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `python3 -m pytest tests/test_primitives.py -v -k "hold_until or while_holding"`
Expected: FAIL，`AttributeError: module 'ai.primitives' has no attribute 'HoldUntil'`

- [ ] **Step 3: 实现两个原语**

追加到 `ai/primitives.py`：

```python
class HoldUntil(Primitive):
    """按住 key 到 pred 成立：切菜 / 喷灭火器 / 洗盘。

    timeout **无默认值，必须显式传**（spec §7.2）。给默认值等于把"这个
    动作该等多久"的领域知识藏进原语里，而切菜与喷灭火器的合理时长差一个
    量级。

    halt 必须 release（spec §7.6 纪律 2 + §11.5 "零残留按键"硬标准）：
    中断（火情、脱困）来时若不松键，按键会卡死到进程退出。这是本项目已
    踩过的坑——AI_REVIEW.md Bug#2 加 halt() 就是为了它。
    """

    def __init__(self, key, pred, timeout):
        # type: (str, Callable[[Dict], bool], int) -> None
        Primitive.__init__(self)
        self.key = key
        self.pred = pred
        self.timeout = timeout
        self._held = False

    def reset(self):
        # type: () -> None
        Primitive.reset(self)
        self._held = False

    def _release(self, ctx=None):
        # type: (Optional[Dict]) -> None
        hands = (ctx or {}).get("hands") if ctx else self._hands
        if hands is not None and self._held:
            hands.release(self.key)
        self._held = False

    def tick(self, ctx):
        # type: (Dict) -> str
        self._enter(ctx)
        hands = ctx["hands"]
        if self.pred(ctx):
            self._release(ctx)
            self.reset()
            return Status.SUCCESS
        if self._ticks > self.timeout:
            self._release(ctx)
            self.reset()
            return Status.FAILURE
        hands.set_move(None)      # 按住干活时不该还在移动
        hands.hold(self.key)
        self._held = True
        return Status.RUNNING

    def halt(self):
        # type: () -> None
        self._release()
        Primitive.halt(self)


class TapWhileHolding(Primitive):
    """按住 hold_key 的同时单击 tap_key —— 传送带顶位专用。

    spec §7.2 原话："在传送带格上拾取时，必须**同时按住反向键顶住**，
    否则角色会被传送带推走。现有 _grab_burst（oc/ai/agent.py:920）+
    _belt_counter（:947，计算反向键）就是干这个的，而本关 Sushi_1_4_2P
    有传送带——这是实跑踩出来的坑，不是理论问题。"

    Tap(key, hold) 的两参数表达不了"按住 A 的同时点 B"，故单列一个原语。

    hold_key 可以是字符串或 ctx -> Optional[str] 的可调用（从
    hands.counter_dir 算）。解析为 None 时退化为普通单击——不在传送带上
    就不需要顶。

    与 Tap 一样边沿触发（同样的 toggle 问题），halt 松开顶带键。
    """

    def __init__(self, tap_key, hold_key, target=None, kind=None):
        # type: (str, Any, Any, Optional[str]) -> None
        Primitive.__init__(self)
        self.tap_key = tap_key
        self.hold_key = hold_key
        self.target = target
        self.kind = kind
        self._fired = False
        self._holding = None   # type: Optional[str]

    def reset(self):
        # type: () -> None
        Primitive.reset(self)
        self._fired = False
        self._holding = None

    def _resolve_hold(self, ctx):
        # type: (Dict) -> Optional[str]
        hk = self.hold_key(ctx) if callable(self.hold_key) else self.hold_key
        return hk or None

    def tick(self, ctx):
        # type: (Dict) -> str
        self._enter(ctx)
        hands = ctx["hands"]
        if self._fired:
            return Status.SUCCESS

        if self.target is not None:
            st = resolve(self.target, ctx)
            if st is None:
                return Status.FAILURE
            if not P.interact_ready(ctx["chef"], st, self.kind,
                                    ctx["w"].to_grid):
                return Status.FAILURE

        hands.set_move(None)      # 先停移动，专心顶带 + 拿（同 _grab_burst）
        hk = self._resolve_hold(ctx)
        if hk:
            hands.hold(hk)
            self._holding = hk
        try:
            pressed = hands.tap(self.tap_key)
        finally:
            # 单击完立刻松顶带键：继续按着会让角色往反方向走
            if self._holding:
                hands.release(self._holding)
                self._holding = None
        if not pressed:
            return Status.RUNNING
        self._fired = True
        return Status.SUCCESS

    def halt(self):
        # type: () -> None
        if self._hands is not None and self._holding:
            self._hands.release(self._holding)
            self._holding = None
        Primitive.halt(self)
```

- [ ] **Step 4: 运行测试，确认通过**

Run: `python3 -m pytest tests/test_primitives.py -v`
Expected: PASS，50 个测试全绿。

- [ ] **Step 5: 确认无残留按键的结构保证**

Run: `grep -n "def halt" ai/primitives.py`
Expected: 至少 3 处（`Primitive` / `HoldUntil` / `TapWhileHolding`）——每个会按住键的原语都有自己的 `halt`。

- [ ] **Step 6: 运行全量测试**

Run: `python3 -m pytest -v`
Expected: 全绿。

- [ ] **Step 7: 提交**

```bash
git add ai/primitives.py tests/test_primitives.py
git commit -m "feat(primitives): HoldUntil + TapWhileHolding

HoldUntil 的 timeout 无默认值、必须显式传（spec §7.2）：给默认值等于
把"这个动作该等多久"的领域知识藏进原语里，而切菜与喷灭火器的合理时长
差一个量级。

halt 必须 release：中断（火情、脱困）来时若不松键，按键会卡死到进程
退出。这是本项目已踩过的坑——AI_REVIEW.md Bug#2 加 halt() 就是为了
它，spec §11.5 把"零残留按键"列为浸泡测试硬标准。

TapWhileHolding 是传送带的必需能力（spec §7.2 原话"初版完全遗漏"）：
传送带格上拾取必须同时按住反向键顶住，否则角色被推走。Tap(key, hold)
的两参数表达不了"按住 A 的同时点 B"，故单列一个原语。现有 _grab_burst
+ _belt_counter 就是干这个的，本关 Sushi_1_4_2P 有传送带。

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---
### Task 10: `EnsureHandsFree` + `InteractWith`

**Files:**
- Modify: `ai/primitives.py`（追加）
- Modify: `tests/test_primitives.py`（追加）

**Interfaces:**
- Consumes: `Approach` / `Face` / `Tap` / `Confirm`（Task 7-8）、`placement.find_drop_station` / `find_drop_cell`（Task 6）、`predicates.hands_free`（Plan 1）
- Produces:
  - `EnsureHandsFree()`：手里有东西就放到就近合法台面。**幂等**——手已空直接 SUCCESS
  - `InteractWith(target, kind, expect, face=True, retries=2)`：= `Approach` + （`Face`）+ `Tap` + `Confirm`。`expect` 是 `ctx -> bool`，**构造时必传**

**`InteractWith` 是本计划的核心产出**（spec §7.2 / §7.3）：这一个复合原语替代今天约 22 处逐字复制的 "goto → tap → confirm" 模板。spec §7.3 原话："今天 97 行的 `MoveAction` → 两行声明。且校验**写不掉**：`InteractWith` 的构造要求必须给 `expect`，没有后置校验就建不出节点。**这把'验证优先'从口号变成结构强制。**"

所以 `expect` 是**位置参数**（不是带默认值的关键字参数）—— 构造时漏给就 `TypeError`。

**`EnsureHandsFree` 幂等的理由**（spec §7.2）："手已空直接 SUCCESS，故可无条件置于任何树首。" 它是中断 task（灭火、丢弃）的首节点，承担 spec §7.4 的优雅退出："优雅退出由抢占方负责前置条件，而非被打断方负责清理。"

- [ ] **Step 1: 写失败测试——`EnsureHandsFree`**

追加到 `tests/test_primitives.py`：

```python
# ---------- EnsureHandsFree（spec §7.2 / §7.4）----------

def test_ensure_hands_free_is_idempotent_when_empty():
    """手已空 → 直接 SUCCESS，零按键。

    spec §7.2："幂等——手已空直接 SUCCESS，故可无条件置于任何树首。"
    """
    ctx, ctrl = make_ctx()
    assert PR.EnsureHandsFree().tick(ctx) == Status.SUCCESS
    assert ctrl.calls == []


def test_ensure_hands_free_walks_to_counter_when_holding():
    """手里有东西 → 走向就近合法空台面（不是就地丢地上）。

    spec §7.4："放就近的合法空台面而非地上（地上虽也能捡，但可能挡路
    或在传送带上被冲走）。"
    """
    sts = [{"t": "counter", "n": "free", "gx": 3, "gz": 0,
            "x": 3 * 1.2, "z": 0.0}]
    w = FakeWorld(stations=sts,
                  walkable=lambda gx, gz: gz == 0 and 0 <= gx <= 4)
    chef = {"name": "Player 2", "x": 0.0, "z": 0.0, "held": "Cucumber",
            "interactions": _inter()}
    ctx, ctrl = make_ctx(w=w, chef=chef)
    assert PR.EnsureHandsFree().tick(ctx) == Status.RUNNING
    assert ctx["hands"].move_dir == "right"


def test_ensure_hands_free_succeeds_once_hands_empty():
    """放下成功（held 变空）→ SUCCESS。"""
    sts = [{"t": "counter", "n": "free", "gx": 1, "gz": 0,
            "x": 1.2, "z": 0.0}]
    w = FakeWorld(stations=sts, walkable=lambda gx, gz: gz == 0)
    chef = {"name": "Player 2", "x": 0.0, "z": 0.0, "held": "Cucumber",
            "interactions": _inter(placement_gx=1, placement_gz=0)}
    ctx, ctrl = make_ctx(w=w, chef=chef)
    node = PR.EnsureHandsFree()
    node.tick(ctx)                       # 到位 → tap
    assert ("tap", "pickup") in ctrl.calls
    chef["held"] = None
    assert node.tick(ctx) == Status.SUCCESS


def test_ensure_hands_free_falls_back_to_floor():
    """没有合法空台面 → 退到地板格（spec §6.1 落点约束）。"""
    w = FakeWorld(walkable=lambda gx, gz: gz == 0 and 0 <= gx <= 3)
    chef = {"name": "Player 2", "x": 0.0, "z": 0.0, "held": "Cucumber",
            "interactions": _inter()}
    ctx, _ = make_ctx(w=w, chef=chef)
    # 没有 counter 站点，走地板兜底：应产生移动或就地放下，而非 FAILURE
    assert PR.EnsureHandsFree().tick(ctx) in (
        Status.RUNNING, Status.SUCCESS)


def test_ensure_hands_free_no_legal_spot_is_failure():
    """全图无合法落点 → FAILURE 交还 Planner，不原地死等。

    spec §6.1："只有腾手本身不可能时（如手持物无处可放）才判不可行。"
    """
    sts = [{"t": "cooking", "n": "c", "gx": 1, "gz": 0, "x": 1.2, "z": 0.0}]
    w = FakeWorld(stations=sts,
                  walkable=lambda gx, gz: (gx, gz) in ((0, 0), (2, 0)))
    chef = {"name": "Player 2", "x": 0.0, "z": 0.0, "held": "Cucumber",
            "interactions": _inter()}
    ctx, _ = make_ctx(w=w, chef=chef)
    assert PR.EnsureHandsFree().tick(ctx) == Status.FAILURE


def test_ensure_hands_free_halt_releases():
    sts = [{"t": "counter", "n": "free", "gx": 3, "gz": 0,
            "x": 3 * 1.2, "z": 0.0}]
    w = FakeWorld(stations=sts,
                  walkable=lambda gx, gz: gz == 0 and 0 <= gx <= 4)
    chef = {"name": "Player 2", "x": 0.0, "z": 0.0, "held": "Cucumber",
            "interactions": _inter()}
    ctx, _ = make_ctx(w=w, chef=chef)
    node = PR.EnsureHandsFree()
    node.tick(ctx)
    node.halt()
    assert ctx["hands"].move_dir is None


# ---------- InteractWith（spec §7.3 的核心产出）----------

def test_interact_with_requires_expect():
    """spec §7.3："构造要求必须给 expect，没有后置校验就建不出节点。

    这把"验证优先"从口号变成结构强制。"
    """
    with pytest.raises(TypeError):
        PR.InteractWith({"n": "c", "gx": 1, "gz": 0}, "pickup")


def test_interact_with_full_sequence():
    """approach → face → tap → confirm，四步都走到。"""
    crate = {"t": "crate", "n": "crate1", "ing": "Cucumber",
             "gx": 2, "gz": 0, "x": 2 * 1.2, "z": 0.0}
    w = FakeWorld(stations=[crate],
                  walkable=lambda gx, gz: gz == 0 and 0 <= gx <= 3)
    chef = {"name": "Player 2", "x": 0.0, "z": 0.0, "held": None,
            "interactions": _inter()}
    ctx, ctrl = make_ctx(w=w, chef=chef)
    node = PR.InteractWith(crate, "pickup",
                           lambda c: c["chef"].get("held") == "Cucumber")

    assert node.tick(ctx) == Status.RUNNING          # 走路
    assert ctx["hands"].move_dir == "right"

    chef["interactions"] = _inter(pickup_gx=2, pickup_gz=0, gx=2, gz=0)
    assert node.tick(ctx) == Status.RUNNING          # 到位 → tap，等确认
    assert ("tap", "pickup") in ctrl.calls

    chef["held"] = "Cucumber"
    assert node.tick(ctx) == Status.SUCCESS


def test_interact_with_expect_false_eventually_fails():
    """按了但世界没变 → 重试用尽后 FAILURE，交还 Planner。"""
    crate = {"t": "crate", "n": "c", "gx": 1, "gz": 0, "x": 1.2, "z": 0.0}
    w = FakeWorld(stations=[crate], walkable=lambda gx, gz: gz == 0)
    chef = {"name": "Player 2", "x": 0.0, "z": 0.0, "held": None,
            "interactions": _inter(pickup_gx=1, pickup_gz=0, gx=1, gz=0)}
    ctx, _ = make_ctx(w=w, chef=chef)
    node = PR.InteractWith(crate, "pickup", lambda c: False, retries=1)
    seen = [node.tick(ctx) for _ in range(6)]
    assert Status.FAILURE in seen


def test_interact_with_target_gone_is_failure():
    """Review Focus 5：目标中途消失 → FAILURE，不崩。"""
    ctx, _ = make_ctx()
    node = PR.InteractWith(lambda c: None, "pickup", lambda c: False)
    assert node.tick(ctx) == Status.FAILURE


def test_interact_with_skips_face_when_disabled():
    """face=False：拾取地面物不需要精确朝向（旧 _goto_grab_cell 的场景）。"""
    it = {"n": "utensil_fire_extinguisher_01", "x": 1.2, "z": 0.0}
    w = FakeWorld(items=[it], walkable=lambda gx, gz: gz == 0)
    chef = {"name": "Player 2", "x": 0.0, "z": 0.0, "held": None,
            "interactions": _inter(pickup="utensil_fire_extinguisher_01")}
    ctx, ctrl = make_ctx(w=w, chef=chef)
    node = PR.InteractWith(it, "pickup",
                           lambda c: c["chef"].get("held") is not None,
                           face=False)
    node.tick(ctx)
    assert ("tap", "pickup") in ctrl.calls


def test_interact_with_halt_releases_everything():
    crate = {"t": "crate", "n": "c", "gx": 3, "gz": 0, "x": 3 * 1.2, "z": 0.0}
    w = FakeWorld(stations=[crate],
                  walkable=lambda gx, gz: gz == 0 and 0 <= gx <= 4)
    chef = {"name": "Player 2", "x": 0.0, "z": 0.0, "held": None,
            "interactions": _inter()}
    ctx, _ = make_ctx(w=w, chef=chef)
    node = PR.InteractWith(crate, "pickup", lambda c: False)
    node.tick(ctx)
    node.halt()
    assert ctx["hands"].move_dir is None
    assert ctx["hands"].held_keys == set()


def test_interact_with_fails_when_hands_change_unexpectedly():
    """spec §7.5 第三类故障：手部连续性不变式。

    原话："**误放下 / 误拾**（没打算放却放了）→ 每个原语开头确认'手里的
    东西与我以为的一致'，不一致说明中间发生意外 → 停下来让 Planner 按
    现实重算。"

    场景：走向箱子的途中，手里莫名多了个东西（被 P1 塞过来、传送带冲来
    一块料、误拾）。继续执行会把这个意外物品放进盘里。
    """
    crate = {"t": "crate", "n": "c", "gx": 3, "gz": 0, "x": 3 * 1.2, "z": 0.0}
    w = FakeWorld(stations=[crate],
                  walkable=lambda gx, gz: gz == 0 and 0 <= gx <= 4)
    chef = {"name": "Player 2", "x": 0.0, "z": 0.0, "held": None,
            "interactions": _inter()}
    ctx, _ = make_ctx(w=w, chef=chef)
    node = PR.InteractWith(crate, "pickup", lambda c: False)
    assert node.tick(ctx) == Status.RUNNING      # 第一拍：记下手是空的
    chef["held"] = "Lettuce"                     # 没按键，手里却多了东西
    assert node.tick(ctx) == Status.FAILURE
    assert ctx["hands"].move_dir is None


def test_interact_with_tolerates_hands_change_after_tap():
    """按下之后手里本该变——那是 expect 的职责，不是异常。

    若不区分"按键前/后"，拾取成功的那一拍会被不变式误判成意外，
    每次取料都 FAILURE。
    """
    crate = {"t": "crate", "n": "c", "gx": 1, "gz": 0, "x": 1.2, "z": 0.0}
    w = FakeWorld(stations=[crate], walkable=lambda gx, gz: gz == 0)
    chef = {"name": "Player 2", "x": 0.0, "z": 0.0, "held": None,
            "interactions": _inter(pickup_gx=1, pickup_gz=0, gx=1, gz=0)}
    ctx, ctrl = make_ctx(w=w, chef=chef)
    node = PR.InteractWith(crate, "pickup",
                           lambda c: c["chef"].get("held") == "Cucumber")
    node.tick(ctx)                               # 到位并 tap
    assert ("tap", "pickup") in ctrl.calls
    chef["held"] = "Cucumber"                    # 拾取生效
    assert node.tick(ctx) == Status.SUCCESS


def test_interact_with_first_tick_never_trips_invariant():
    """首次 tick 没有"上一拍"，手里有东西也不该算不一致。

    否则所有"手持某物去放下"的 task 第一拍就 FAILURE。
    """
    counter = {"t": "counter", "n": "c", "gx": 1, "gz": 0,
               "x": 1.2, "z": 0.0}
    w = FakeWorld(stations=[counter], walkable=lambda gx, gz: gz == 0)
    chef = {"name": "Player 2", "x": 0.0, "z": 0.0, "held": "Cucumber",
            "interactions": _inter()}
    ctx, _ = make_ctx(w=w, chef=chef)
    node = PR.InteractWith(counter, "placement", lambda c: False)
    assert node.tick(ctx) != Status.FAILURE


def test_interact_with_does_not_double_tap_across_ticks():
    """Review Focus 1 在复合原语上同样成立：等确认期间不重按。"""
    crate = {"t": "crate", "n": "c", "gx": 1, "gz": 0, "x": 1.2, "z": 0.0}
    w = FakeWorld(stations=[crate], walkable=lambda gx, gz: gz == 0)
    chef = {"name": "Player 2", "x": 0.0, "z": 0.0, "held": None,
            "interactions": _inter(pickup_gx=1, pickup_gz=0, gx=1, gz=0)}
    ctx, ctrl = make_ctx(w=w, chef=chef)
    node = PR.InteractWith(crate, "pickup", lambda c: False, retries=5)
    for _ in range(4):
        node.tick(ctx)
        ctx["hands"].tick()
    assert ctrl.calls.count(("tap", "pickup")) == 1, \
        "等确认期间重按 = 拿起又放下"
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `python3 -m pytest tests/test_primitives.py -v -k "hands_free or interact_with"`
Expected: FAIL，`AttributeError: module 'ai.primitives' has no attribute 'EnsureHandsFree'`

- [ ] **Step 3: 实现两个原语**

追加到 `ai/primitives.py`：

```python
class EnsureHandsFree(Primitive):
    """手里有东西就放到就近的合法空台面。

    **幂等**（spec §7.2）：手已空直接 SUCCESS，故可无条件置于任何树首。

    这个节点承担 spec §7.4 的优雅退出："优雅退出由抢占方负责前置条件，
    而非被打断方负责清理。"被打断的 task 树 halt 只松键然后丢弃；抢占方
    （灭火、丢弃等中断 task）树首放这个节点，把手清空再去拿灭火器。
    理由是职责清晰——"灭火需要空手"是灭火的领域知识，切菜树不需要知道
    灭火的任何事。

    落点约束走 placement（spec §6.1）：先找空台面，没有才退地板格。
    全图无合法落点 → FAILURE，不原地死等——spec §6.1"只有腾手本身不可能
    时才判不可行"。

    放下的东西不需要专门回收（spec §7.4）：火灭后回 Normal，Planner 看
    当前世界"黄瓜在台面上、没切、盘里缺黄瓜"自然重新规划。这是无状态
    重算的红利。
    """

    def __init__(self):
        # type: () -> None
        Primitive.__init__(self)
        self._tap = Tap("pickup")

    def reset(self):
        # type: () -> None
        Primitive.reset(self)
        self._tap.reset()

    def halt(self):
        # type: () -> None
        self._tap.halt()
        Primitive.halt(self)

    def tick(self, ctx):
        # type: (Dict) -> str
        self._enter(ctx)
        w, chef, hands = ctx["w"], ctx["chef"], ctx["hands"]
        if P.hands_free(chef):
            self.reset()
            return Status.SUCCESS

        st = placement.find_drop_station(w, ctx["field"])
        if st is not None:
            if P.interact_ready(chef, st, "placement", w.to_grid):
                return self._tap.tick(ctx)
            return self._walk_to_cell(ctx, target_cell(st, ctx))

        cell = placement.find_drop_cell(w, ctx["field"])
        if cell is None:
            hands.set_move(None)
            return Status.FAILURE      # 无处可放 → 交还 Planner
        cur = P.chef_cell(chef, w.to_grid)
        if cur == cell:
            return self._tap.tick(ctx)   # 已在落点格上，就地放下
        return self._walk_to_cell(ctx, cell)

    def _walk_to_cell(self, ctx, cell):
        # type: (Dict, Optional[Tuple[int, int]]) -> str
        """朝 cell 走一步。走不了 → FAILURE。"""
        w, chef, hands = ctx["w"], ctx["chef"], ctx["hands"]
        if cell is None:
            hands.set_move(None)
            return Status.FAILURE
        cur = P.chef_cell(chef, w.to_grid)
        if cur is None:
            hands.set_move(None)
            return Status.FAILURE
        goals = navigation.interact_cells(
            cell[0], cell[1], w.walkable, w.belt_cells) or [cell]
        got = navigation.nearest_cost(ctx["field"], goals)
        if got is None:
            hands.set_move(None)
            return Status.FAILURE
        nxt = navigation.first_step(ctx["field"], got[0], w.belt_dir)
        if nxt is None:
            d = _dir_towards(cur, cell)
            if d is None:
                hands.set_move(None)
                return Status.FAILURE
            hands.set_move(d)
            return Status.RUNNING
        d = _dir_towards(cur, nxt)
        if d is None:
            hands.set_move(None)
            return Status.FAILURE
        hands.set_move(d)
        return Status.RUNNING


class InteractWith(Primitive):
    """= Approach + (Face) + Tap + Confirm。前置 + 后置齐备。

    这一个复合原语替代今天约 **22 处**逐字复制的 "goto → tap → confirm"
    模板（实测 oc/ai/actions.py：confirm* 子状态分支 22 个、_goto* 调用点
    22 个、confirm_wait < 2 重试样板 17 处）。97 行的 MoveAction → 两行
    声明（spec §7.3）。

    **expect 是位置参数，构造时漏给就 TypeError**。spec §7.3 原话：
    "校验写不掉：InteractWith 的构造要求必须给 expect，没有后置校验就
    建不出节点。这把'验证优先'从口号变成结构强制。"

    face=False 用于不需要精确朝向的场景（拾取地面物，旧
    _goto_grab_cell 的那条路）。
    """

    def __init__(self, target, kind, expect, face=True, retries=2):
        # type: (Any, Optional[str], Callable[[Dict], bool], bool, int) -> None
        Primitive.__init__(self)
        self.target = target
        self.kind = kind
        self.expect = expect
        self._approach = Approach(target, kind)
        self._face = Face(target) if face else None
        self._tap = Tap("pickup", target, kind)
        self._confirm = Confirm(expect, retries)

    def _children(self):
        out = [self._approach]
        if self._face is not None:
            out.append(self._face)
        out.extend([self._tap, self._confirm])
        return out

    def reset(self):
        # type: () -> None
        Primitive.reset(self)
        for c in self._children():
            c.reset()

    def halt(self):
        # type: () -> None
        for c in self._children():
            c.halt()
        Primitive.halt(self)

    def tick(self, ctx):
        # type: (Dict) -> str
        self._enter(ctx)
        # 后置条件已成立 → 这一步本来就做完了（上游 task 的 done 即
        # 下游的 feasible，spec §6.1 上下游咬合，所以这种情况是常态）
        if self.expect(ctx):
            self.reset()
            return Status.SUCCESS

        # 手部连续性不变式（spec §7.5 第三类故障）：我没按键，手里的东西
        # 却变了——被 P1 换走、传送带冲掉、误拾。停下来让 Planner 按现实
        # 重算，不在这里猜发生了什么（spec §7.6 纪律 1）。
        #
        # 只在 Tap 还没按下时查：按下之后手里本来就该变，那是 expect 的
        # 职责。_tap._fired 正好是"本次激活期按过没"的权威标记。
        if not self._tap._fired and self.hands_changed(ctx):
            ctx["hands"].set_move(None)
            self.reset()
            return Status.FAILURE
        self._remember_hands(ctx)

        if resolve(self.target, ctx) is None:
            ctx["hands"].set_move(None)
            return Status.FAILURE

        # 顺序推进，与 bt.Sequence 语义一致（每拍从头，各子节点自带边沿）
        for child in self._children():
            s = child.tick(ctx)
            if s == Status.RUNNING:
                return Status.RUNNING
            if s == Status.FAILURE:
                self.reset()
                return Status.FAILURE
        self.reset()
        return Status.SUCCESS
```

文件头 import 区补 `from ai import placement`。

- [ ] **Step 4: 运行测试，确认通过**

Run: `python3 -m pytest tests/test_primitives.py -v`
Expected: PASS，66 个测试全绿。

- [ ] **Step 5: 运行全量测试**

Run: `python3 -m pytest -v`
Expected: 全绿。

- [ ] **Step 6: 提交**

```bash
git add ai/primitives.py tests/test_primitives.py
git commit -m "feat(primitives): EnsureHandsFree + InteractWith

InteractWith 是本计划的核心产出（spec §7.3）：一个复合原语替代今天约
22 处逐字复制的 goto→tap→confirm 模板（实测 oc/ai/actions.py 里
confirm* 子状态分支 22 个、_goto* 调用点 22 个、confirm_wait<2 重试
样板 17 处）。97 行的 MoveAction 变成两行声明。

expect 是位置参数而非带默认值的关键字参数——spec §7.3 原话"构造要求
必须给 expect，没有后置校验就建不出节点，这把'验证优先'从口号变成
结构强制"。漏给就 TypeError。

EnsureHandsFree 幂等（手已空直接 SUCCESS），故可无条件置于任何树首。
它承担 spec §7.4 的优雅退出：优雅退出由抢占方负责前置条件，而非被打断
方负责清理——"灭火需要空手"是灭火的领域知识，切菜树不需要知道灭火的
任何事。放下的东西不用专门回收，Planner 下拍重新看世界即可。

落点全图无合法位置时 FAILURE 而非死等（spec §6.1"只有腾手本身不可能
时才判不可行"）。

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 11: 脱困三原语 + `Timeout` 装饰

**Files:**
- Modify: `ai/primitives.py`（追加）
- Modify: `tests/test_primitives.py`（追加）

**Interfaces:**
- Consumes: `Primitive`（Task 7）、`blackboard.StuckScratch`（Plan 1）
- Produces:
  - `MarkBlocked(cell, ttl)`：把格子拉黑写入 `bb.scratch.bad_cells`（L1）
  - `Nudge(direction, ticks)`：侧向微移（L2）
  - `AbandonTarget(target, ttl)`：拉黑**目标**并强制 commitment 失效（L3）
  - `Timeout(child, max_ticks)`：整个 task 级超时装饰
  - `BAD_CELL_TTL_TICKS = 80` / `ABANDON_TTL_TICKS = 200`

**三级脱困为什么都要**（spec §7.2 原话）："现有脱困是**分级**的，三级都要保留（初版只覆盖了 L1 / L2）。" 而 **L3 不能用 `Timeout` 替代**："`Timeout` 的触发条件是'耗时过久'，L3 的触发条件是'**同一格反复卡**'（`last_cell` / `same_count` 计数）。一个 task 可能反复卡在同一格但每次都没超时，Timeout 永远不响。两个机制都要有。"

**TTL 单位改为拍**：旧实现用 `time.monotonic() + BAD_CELL_TTL`（秒，`oc/ai/agent.py:977`）。新架构里一切以拍计（spec §3.4"本文档中'拍'统一指 Python 循环拍（50 ms）"），混用两套时基会让测试必须 sleep。`BAD_CELL_TTL = 4.0` 秒 → 20 Hz 下 80 拍。

**`AbandonTarget` 写哪里**：spec §7.2 说它"拉黑目标（非格子）并强制 commitment 失效"。commitment 在 `bb.commitment`，而 Executor 对它只有 `steps_done` / `retries` 的写权（spec §4.5）。所以这里**不直接清 commitment**，而是写 `bb.scratch.abandoned`（目标名 → 失效拍号），由 Planner 在可行性过滤时读它（Plan 3 接）。这是守住 §4.5 写入规则的唯一做法。

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_primitives.py`：

```python
# ---------- 脱困三原语（spec §7.2）----------

def test_mark_blocked_writes_scratch():
    """L1：把卡住的格拉黑，下次 Approach 的距离场绕开它。"""
    from ai.blackboard import StuckScratch
    bb = Blackboard()
    bb.scratch = StuckScratch()
    ctx, _ = make_ctx(bb=bb, tick=100)
    assert PR.MarkBlocked((3, 1), ttl=80).tick(ctx) == Status.SUCCESS
    assert bb.scratch.bad_cells[(3, 1)] == 180


def test_mark_blocked_creates_scratch_if_missing():
    """scratch 为 None 时自建——卡住守卫可能先于任何模式进入。"""
    bb = Blackboard()
    ctx, _ = make_ctx(bb=bb, tick=0)
    PR.MarkBlocked((1, 1), ttl=10).tick(ctx)
    assert bb.scratch is not None
    assert (1, 1) in bb.scratch.bad_cells


def test_mark_blocked_accepts_callable_cell():
    from ai.blackboard import StuckScratch
    bb = Blackboard()
    bb.scratch = StuckScratch()
    ctx, _ = make_ctx(bb=bb, tick=5)
    PR.MarkBlocked(lambda c: (7, 7), ttl=10).tick(ctx).lower()
    assert (7, 7) in bb.scratch.bad_cells


def test_mark_blocked_none_cell_is_failure():
    ctx, _ = make_ctx()
    assert PR.MarkBlocked(lambda c: None, ttl=10).tick(ctx) == Status.FAILURE


def test_nudge_presses_direction_for_n_ticks():
    """L2：侧向微移固定拍数。"""
    ctx, ctrl = make_ctx()
    node = PR.Nudge("up", ticks=2)
    assert node.tick(ctx) == Status.RUNNING
    assert ctx["hands"].move_dir == "up"
    assert node.tick(ctx) == Status.RUNNING
    assert node.tick(ctx) == Status.SUCCESS
    assert ctx["hands"].move_dir is None


def test_nudge_releases_on_halt():
    ctx, _ = make_ctx()
    node = PR.Nudge("up", ticks=5)
    node.tick(ctx)
    node.halt()
    assert ctx["hands"].move_dir is None


def test_nudge_callable_direction():
    ctx, _ = make_ctx()
    PR.Nudge(lambda c: "down", ticks=1).tick(ctx)
    assert ctx["hands"].move_dir == "down"


def test_nudge_none_direction_is_failure():
    ctx, _ = make_ctx()
    assert PR.Nudge(lambda c: None, ticks=1).tick(ctx) == Status.FAILURE


def test_abandon_target_writes_scratch_not_commitment():
    """L3：拉黑目标并记进 scratch。

    不直接清 bb.commitment：spec §4.5 规定 Executor 对 commitment 只有
    steps_done / retries 的写权。Planner 在可行性过滤时读 scratch.abandoned
    （Plan 3 接），这是守住写入规则的唯一做法。
    """
    from ai.blackboard import StuckScratch
    bb = Blackboard()
    bb.scratch = StuckScratch()
    before = bb.commitment.active_task
    ctx, _ = make_ctx(bb=bb, tick=50)
    assert PR.AbandonTarget("cooker1", ttl=200).tick(ctx) == Status.SUCCESS
    assert bb.scratch.abandoned["cooker1"] == 250
    assert bb.commitment.active_task is before, \
        "Executor 不许直接动 commitment.active_task（spec §4.5）"


def test_abandon_target_accepts_station_dict():
    """传站点 dict 时按实例名 n 拉黑（资源 id 规则，spec §4.4）。"""
    from ai.blackboard import StuckScratch
    bb = Blackboard()
    bb.scratch = StuckScratch()
    ctx, _ = make_ctx(bb=bb, tick=0)
    PR.AbandonTarget({"n": "workstation_cooker_01 (3)"}, ttl=10).tick(ctx)
    assert "workstation_cooker_01 (3)" in bb.scratch.abandoned


def test_abandon_target_none_is_failure():
    ctx, _ = make_ctx()
    assert PR.AbandonTarget(lambda c: None, ttl=10).tick(ctx) \
        == Status.FAILURE


# ---------- Timeout 装饰（spec §7.2 结构性安全网）----------

def test_timeout_passes_through_child_status():
    ctx, _ = make_ctx()
    assert PR.Timeout(PR.Settle(0), 10).tick(ctx) == Status.SUCCESS


def test_timeout_fires_after_max_ticks():
    """整个 task 级超时：今天只有单步重试，没有"这个 task 卡了 30 拍"
    的兜底（spec §7.2）。防死循环的最后一道闸。"""
    ctx, _ = make_ctx()
    node = PR.Timeout(PR.Settle(100), max_ticks=3)
    for _ in range(3):
        assert node.tick(ctx) == Status.RUNNING
    assert node.tick(ctx) == Status.FAILURE


def test_timeout_halts_child_when_firing():
    """超时转 FAILURE 时必须 halt 子树，否则 HoldUntil 的键留着。"""
    ctx, ctrl = make_ctx()
    child = PR.HoldUntil("chop", lambda c: False, timeout=100)
    node = PR.Timeout(child, max_ticks=1)
    node.tick(ctx)
    assert "chop" in ctx["hands"].held_keys
    assert node.tick(ctx) == Status.FAILURE
    assert ctx["hands"].held_keys == set()


def test_timeout_budget_resets_after_child_completes():
    """子树 SUCCESS 后预算重置——树是跨拍复用的，不重置会越跑越紧。"""
    ctx, _ = make_ctx()
    child = PR.Settle(1)
    node = PR.Timeout(child, max_ticks=2)
    assert node.tick(ctx) == Status.RUNNING
    assert node.tick(ctx) == Status.SUCCESS
    assert node.tick(ctx) == Status.RUNNING, "预算该重置了"


def test_timeout_halt_propagates():
    ctx, _ = make_ctx()
    child = PR.HoldUntil("chop", lambda c: False, timeout=100)
    node = PR.Timeout(child, max_ticks=50)
    node.tick(ctx)
    node.halt()
    assert ctx["hands"].held_keys == set()
```

- [ ] **Step 2: 给 `StuckScratch` 加 `abandoned` 字段**

修改 `ai/blackboard.py` 的 `StuckScratch`：

```python
@dataclass
class StuckScratch:
    """脱困模式局部状态。模式进入时构造、退出即丢弃。"""
    bad_cells: Dict[Tuple[int, int], int] = field(default_factory=dict)
    nudge_dir: Any = None
    last_cell: Optional[Tuple[int, int]] = None
    same_count: int = 0
    skip_ticks: int = 0
    # L3 放弃的目标 → 失效拍号（spec §7.2 的 AbandonTarget）。
    # 不直接清 bb.commitment：§4.5 规定 Executor 对 commitment 只有
    # steps_done / retries 的写权，Planner 在可行性过滤时读这里。
    abandoned: Dict[Any, int] = field(default_factory=dict)
```

`bad_cells` 的值语义同时从"失效时刻（秒）"改为"失效拍号"——旧实现用 `time.monotonic()`，而 spec §3.4 规定一切以拍计。在该 dataclass 的 docstring 里记一笔。

- [ ] **Step 3: 运行测试，确认失败**

Run: `python3 -m pytest tests/test_primitives.py -v -k "mark_blocked or nudge or abandon or timeout"`
Expected: FAIL，`AttributeError: module 'ai.primitives' has no attribute 'MarkBlocked'`

- [ ] **Step 4: 实现四个节点**

追加到 `ai/primitives.py`：

```python
# 脱困 TTL，单位**拍**。旧实现用 time.monotonic() + 秒数
# （oc/ai/agent.py:977 的 BAD_CELL_TTL = 4.0），而 spec §3.4 规定
# "本文档中'拍'统一指 Python 循环拍（50 ms）"。混用两套时基会让测试
# 必须 sleep。4.0 秒 → 20 Hz 下 80 拍。
BAD_CELL_TTL_TICKS = 80
ABANDON_TTL_TICKS = 200


def _ensure_stuck_scratch(ctx):
    # type: (Dict) -> Any
    """取（必要时新建）StuckScratch。

    卡住守卫是跨模式前置（spec §5.1），可能先于任何模式进入，此时
    scratch 还是 None。
    """
    from ai.blackboard import StuckScratch
    bb = ctx["bb"]
    if not isinstance(getattr(bb, "scratch", None), StuckScratch):
        bb.scratch = StuckScratch()
    return bb.scratch


def _resolve_value(v, ctx):
    # type: (Any, Dict) -> Any
    return v(ctx) if callable(v) else v


class MarkBlocked(Primitive):
    """L1：把格子拉黑写入 scratch.bad_cells（spec §7.2）。

    Approach 读它并重算距离场，于是下次寻路绕开堵死的方向。执行层对
    scratch 有写权（spec §4.5），对 claims 没有。
    """

    def __init__(self, cell, ttl=BAD_CELL_TTL_TICKS):
        # type: (Any, int) -> None
        Primitive.__init__(self)
        self.cell = cell
        self.ttl = ttl

    def tick(self, ctx):
        # type: (Dict) -> str
        self._enter(ctx)
        cell = _resolve_value(self.cell, ctx)
        if cell is None:
            return Status.FAILURE
        scratch = _ensure_stuck_scratch(ctx)
        scratch.bad_cells[tuple(cell)] = (ctx.get("tick") or 0) + self.ttl
        return Status.SUCCESS


class Nudge(Primitive):
    """L2：侧向微移 ticks 拍，脱离贴墙 / 离格（spec §7.2）。"""

    def __init__(self, direction, ticks=3):
        # type: (Any, int) -> None
        Primitive.__init__(self)
        self.direction = direction
        self.ticks = ticks
        self._left = ticks

    def reset(self):
        # type: () -> None
        Primitive.reset(self)
        self._left = self.ticks

    def tick(self, ctx):
        # type: (Dict) -> str
        self._enter(ctx)
        hands = ctx["hands"]
        if self._left <= 0:
            hands.set_move(None)
            self.reset()
            return Status.SUCCESS
        d = _resolve_value(self.direction, ctx)
        if not d or d not in DIR2D:
            hands.set_move(None)
            return Status.FAILURE
        hands.set_move(d)
        self._left -= 1
        return Status.RUNNING


class AbandonTarget(Primitive):
    """L3：放弃该目标并拉黑一段时间，触发重规划（spec §7.2）。

    **L3 不能用 Timeout 替代**（spec §7.2 原话）：Timeout 的触发条件是
    "耗时过久"，L3 的是"同一格反复卡"（last_cell / same_count 计数）。
    一个 task 可能反复卡在同一格但每次都没超时，Timeout 永远不响。

    写 scratch.abandoned 而不直接清 bb.commitment：spec §4.5 规定执行层
    对 commitment 只有 steps_done / retries 的写权。Planner 在可行性过滤
    时读这里（Plan 3 接）。
    """

    def __init__(self, target, ttl=ABANDON_TTL_TICKS):
        # type: (Any, int) -> None
        Primitive.__init__(self)
        self.target = target
        self.ttl = ttl

    def tick(self, ctx):
        # type: (Dict) -> str
        self._enter(ctx)
        t = _resolve_value(self.target, ctx)
        if isinstance(t, dict):
            t = t.get("n")
        if not t:
            return Status.FAILURE
        scratch = _ensure_stuck_scratch(ctx)
        scratch.abandoned[t] = (ctx.get("tick") or 0) + self.ttl
        return Status.SUCCESS


class Timeout(Node):
    """整个 task 级超时装饰（spec §7.2 结构性安全网）。

    今天只有单步重试，没有"这个 task 卡了 30 拍"的兜底。任何 task 异常
    久 → FAILURE → Planner 重算。防死循环的最后一道闸。

    超时转 FAILURE 时 halt 子树：否则 HoldUntil 按住的键会留着。
    子树 SUCCESS / FAILURE 后预算重置——树是跨拍复用的对象，不重置会
    越跑越紧。
    """

    def __init__(self, child, max_ticks):
        # type: (Node, int) -> None
        self.child = child
        self.max_ticks = max_ticks
        self._ticks = 0

    def reset(self):
        # type: () -> None
        self._ticks = 0
        reset = getattr(self.child, "reset", None)
        if callable(reset):
            reset()

    def tick(self, ctx):
        # type: (Dict) -> str
        if self._ticks >= self.max_ticks:
            self.child.halt()
            self._ticks = 0
            return Status.FAILURE
        self._ticks += 1
        s = self.child.tick(ctx)
        if s != Status.RUNNING:
            self._ticks = 0
        return s

    def halt(self):
        # type: () -> None
        self.child.halt()
        self._ticks = 0
```

- [ ] **Step 5: 运行测试，确认通过**

Run: `python3 -m pytest tests/test_primitives.py -v`
Expected: PASS，82 个测试全绿。

- [ ] **Step 6: 确认原语数量齐了**

Run: `grep -c "^class " ai/primitives.py`
Expected: `14`

对账到 spec §7.2 的"11 原语 + 1 复合 + 1 超时装饰"：

| 类 | spec §7.2 的归类 |
|---|---|
| `Primitive` | 基类，不在 spec 的计数里 |
| `Approach` / `Face` / `Tap` / `TapWhileHolding` / `HoldUntil` / `Confirm` / `Settle` / `EnsureHandsFree` | 核心交互 8 个 |
| `MarkBlocked` / `Nudge` / `AbandonTarget` | 脱困 3 个 |
| `InteractWith` | 1 个复合 |
| `Timeout` | 1 个超时装饰 |

8 + 3 = 11 原语 ✓。14 = 11 + 复合 + 装饰 + 基类。

若实际输出不是 14，逐个核对上表，缺的补上、多的说明理由并记为 Ruling。

- [ ] **Step 7: 运行全量测试**

Run: `python3 -m pytest -v`
Expected: 全绿。

- [ ] **Step 8: 提交**

```bash
git add ai/primitives.py ai/blackboard.py tests/test_primitives.py
git commit -m "feat(primitives): 脱困三原语 + Timeout 装饰

三级脱困都要（spec §7.2 原话"三级都要保留，初版只覆盖了 L1/L2"）。
L3 不能用 Timeout 替代：Timeout 的触发条件是"耗时过久"，L3 的是
"同一格反复卡"（last_cell/same_count 计数）——一个 task 可能反复卡在
同一格但每次都没超时，Timeout 永远不响。两个机制都要有。

TTL 单位从秒改为拍：旧实现用 time.monotonic() + BAD_CELL_TTL=4.0
（oc/ai/agent.py:977），而 spec §3.4 规定"拍统一指 Python 循环拍
（50 ms）"。混用两套时基会让测试必须 sleep。4.0 秒 → 80 拍。

AbandonTarget 写 scratch.abandoned 而不直接清 bb.commitment：spec §4.5
规定执行层对 commitment 只有 steps_done/retries 的写权。Planner 在可行性
过滤时读它（Plan 3 接）。这是守住写入规则的唯一做法。

Timeout 超时转 FAILURE 时 halt 子树（否则 HoldUntil 的键留着），
子树完成后预算重置（树是跨拍复用对象，不重置会越跑越紧）。

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---
### Task 12: Task 身份与基类

**Files:**
- Create: `ai/tasks/__init__.py`
- Create: `ai/tasks/base.py`
- Test: `tests/test_tasks.py`

**Interfaces:**
- Consumes: `bt.Sequence` / `bt.Status`（Task 1）、`primitives.*`（Task 7-11）、`blackboard.Blackboard.is_claimed_by_other`（Plan 1）
- Produces:
  - `OWNER = "P2"`
  - `Task`（基类，不可变值对象）。构造签名 `Task(order_id=None, resources=(), item=None)`；属性：
    - `TYPE`（类属性，字符串）
    - `order_id` / `resources`（`Tuple[Any, ...]`）/ `item`
    - `key` → `Tuple`：`(TYPE, order_id, resources, item)`，spec §6.0 的四元身份
  - 子类必须实现：`claims()` → `Tuple[Tuple[Any, str], ...]`、`feasible(ctx)` → `bool`、`done(ctx)` → `bool`、`build()` → `bt.Node`
  - `Task.__eq__` / `__hash__` / `__repr__` 按 `key`
  - `claims_available(task, ctx)` → `bool`：spec §4.2 的全局可行性前置
  - `TASK_TYPES` → `Dict[str, type]`：类型名 → 类，`tree_viz` 用

**spec §6.0 原话**："**task 实例 = `(type, order_id, bound_resources, bound_item)`**。两个 task 只有四项全同才是同一个 task。`FetchToPlate(Cucumber→plate_A, order#1)` 与 `FetchToPlate(Cucumber→plate_B, order#2)` 是**两个不同的 task**。"

**为什么身份必须这样定义**（Review Focus 3）：spec §6.0 给了具体 bug——"两单都缺黄瓜时，若 `FetchToPlate` 的完成判定不绑定具体盘，那么 **P1 把黄瓜放进盘 B，会让 AI 针对盘 A 的 task 误报 SUCCESS**——AI 以为自己干完了，盘 A 其实还空着。"

**`claims_available` 是 spec §4.2 点名的致命缺陷修补**："设计稿初版的致命缺陷：定义了认领表，却**没有任何一条可行性谓词读它**。那么 claims 是一张只写不读的表。" 规则：
```
task 可行  ⟹  对其所需的每个 (resource, action)：
                该键未被他人认领   或   认领已过 TTL
```
这一条**优先于**各 task 自己的谓词。

- [ ] **Step 1: 写失败测试**

创建 `tests/test_tasks.py`：

```python
"""task 身份 / 可行性 / 完成判定 / 树结构单测。

ctx 的装配复用 tests/test_primitives.py 的做法（假 World + 假 controller），
零按键注入。
"""
import pytest

from ai import primitives as PR
from ai import tasks as T
from ai.bt import Status, Sequence
from ai.blackboard import Blackboard
from ai.hands import Hands
from ai.navigation import distance_field
from ai.recipes import RecipeBook


class FakeCtrl(object):
    def __init__(self):
        self.calls = []

    def tap(self, name, hold=0.12):
        self.calls.append(("tap", name))

    def press(self, name):
        self.calls.append(("press", name))

    def release(self, name):
        self.calls.append(("release", name))

    def release_all(self):
        self.calls.append(("release_all",))


class FakeWorld(object):
    def __init__(self, **kw):
        self.stations = kw.get("stations") or []
        self.plates = kw.get("plates") or []
        self.workstations = kw.get("workstations") or []
        self.cookers = kw.get("cookers") or []
        self.items = kw.get("items") or []
        self.chefs = kw.get("chefs") or []
        self.orders = kw.get("orders") or []
        self.recipes = kw.get("recipes") or []
        self.extinguishers = kw.get("extinguishers") or []
        self.level = kw.get("level") or "Sushi_1_4_2P"
        self.belt_cells = set(kw.get("belt_cells") or ())
        self.belt_dir = kw.get("belt_dir") or {}
        self._walk = kw.get("walkable") or (lambda gx, gz: True)
        self._cell = 1.2

    def walkable(self, gx, gz):
        return self._walk(gx, gz)

    def to_grid(self, x, z):
        return (int(round(x / self._cell)), int(round(z / self._cell)))

    def to_world(self, gx, gz):
        return (gx * self._cell, gz * self._cell)


def inter(**kw):
    base = {"interactable": None, "pickup": None, "placement": None,
            "grid": None, "gx": None, "gz": None,
            "pickup_gx": None, "pickup_gz": None,
            "placement_gx": None, "placement_gz": None}
    base.update(kw)
    return base


def make_ctx(w, chef=None, bb=None, tick=0, start=(0, 0)):
    chef = chef if chef is not None else {
        "name": "Player 2", "x": start[0] * 1.2, "z": start[1] * 1.2,
        "held": None, "interactions": inter()}
    ctrl = FakeCtrl()
    cur = w.to_grid(chef["x"], chef["z"])
    return {
        "w": w,
        "chef": chef,
        "hands": Hands(ctrl),
        "bb": bb if bb is not None else Blackboard(),
        "field": distance_field(cur, w.walkable, w.belt_dir),
        "book": RecipeBook(w.recipes, w.stations),
        "tick": tick,
    }, ctrl


# ---------- 身份（spec §6.0 / Review Focus 3）----------

class Dummy(T.Task):
    TYPE = "Dummy"

    def claims(self):
        return ((self.resources[0], "use"),) if self.resources else ()

    def feasible(self, ctx):
        return True

    def done(self, ctx):
        return False

    def build(self):
        return Sequence([])


def test_task_key_is_four_tuple():
    t = Dummy(order_id="1", resources=("plate_A",), item="Cucumber")
    assert t.key == ("Dummy", "1", ("plate_A",), "Cucumber")


def test_tasks_differing_in_resource_are_different():
    """核心：绑定不同盘的同类 task 是两个 task（spec §6.0）。

    否则两单都缺黄瓜时，P1 放入盘 B 会让针对盘 A 的 task 误报成功。
    """
    a = Dummy(order_id="1", resources=("plate_A",), item="Cucumber")
    b = Dummy(order_id="2", resources=("plate_B",), item="Cucumber")
    assert a != b
    assert a.key != b.key
    assert len({a, b}) == 2


def test_tasks_with_same_four_items_are_equal():
    a = Dummy(order_id="1", resources=("p",), item="X")
    b = Dummy(order_id="1", resources=("p",), item="X")
    assert a == b and hash(a) == hash(b)
    assert len({a, b}) == 1


def test_task_is_immutable():
    """不可变值对象：Planner 每拍重算、把它当字典键用，可变会出错。"""
    t = Dummy(order_id="1", resources=("p",), item="X")
    with pytest.raises(AttributeError):
        t.order_id = "2"


def test_task_resources_normalized_to_tuple():
    """传 list 也存成 tuple，否则 hash 会炸。"""
    t = Dummy(resources=["a", "b"])
    assert t.resources == ("a", "b")
    assert hash(t) is not None


def test_task_repr_shows_identity():
    t = Dummy(order_id="1", resources=("plate_A",), item="Cucumber")
    r = repr(t)
    assert "Dummy" in r and "plate_A" in r and "Cucumber" in r


def test_task_base_methods_are_abstract():
    class Bare(T.Task):
        TYPE = "Bare"
    t = Bare()
    for name in ("claims", "feasible", "done", "build"):
        with pytest.raises(NotImplementedError):
            fn = getattr(t, name)
            fn({}) if name in ("feasible", "done") else fn()


# ---------- claims_available（spec §4.2）----------

def test_claims_available_when_unclaimed():
    w = FakeWorld()
    ctx, _ = make_ctx(w)
    t = Dummy(resources=("cooker1",))
    assert T.claims_available(t, ctx) is True


def test_claims_available_false_when_claimed_by_other():
    """spec §4.2："定义了认领表却没有任何可行性谓词读它"是初版致命缺陷。

    这一条优先于各 task 自己的谓词。
    """
    bb = Blackboard()
    bb.claim("cooker1", "use", owner="P1", order_id="9", tick=0)
    ctx, _ = make_ctx(FakeWorld(), bb=bb, tick=1)
    assert T.claims_available(Dummy(resources=("cooker1",)), ctx) is False


def test_claims_available_true_when_claim_expired():
    """TTL 过期的认领不算阻塞（spec §4.1 触发点 4 的兜底）。"""
    bb = Blackboard()
    bb.claim("cooker1", "use", owner="P1", order_id="9", tick=0, ttl=5)
    ctx, _ = make_ctx(FakeWorld(), bb=bb, tick=100)
    assert T.claims_available(Dummy(resources=("cooker1",)), ctx) is True


def test_claims_available_true_for_own_claim():
    """自己认领的资源当然可用——否则第二拍就判自己不可行。"""
    bb = Blackboard()
    bb.claim("cooker1", "use", owner=T.OWNER, order_id="1", tick=0)
    ctx, _ = make_ctx(FakeWorld(), bb=bb, tick=1)
    assert T.claims_available(Dummy(resources=("cooker1",)), ctx) is True


def test_claims_available_action_level_granularity():
    """spec §4.0：键是 (resource, action)，锁一个动作不锁整个资源。

    P1 往 1 号锅放了生饭 → (cooker1,"fetch_to") 被锁，但
    (cooker1,"take_cooked") 依然开放——取熟饭正是最有价值的接力。
    """
    class TakeCooked(T.Task):
        TYPE = "TakeCooked"

        def claims(self):
            return ((self.resources[0], "take_cooked"),)

        def feasible(self, ctx):
            return True

        def done(self, ctx):
            return False

        def build(self):
            return Sequence([])

    bb = Blackboard()
    bb.claim("cooker1", "fetch_to", owner="P1", order_id="9", tick=0)
    ctx, _ = make_ctx(FakeWorld(), bb=bb, tick=1)
    assert T.claims_available(TakeCooked(resources=("cooker1",)), ctx) is True


def test_claims_available_no_claims_is_true():
    ctx, _ = make_ctx(FakeWorld())
    assert T.claims_available(Dummy(), ctx) is True


def test_task_types_registry_covers_all_subclasses():
    """TASK_TYPES 是 tree_viz 的入口，漏登记的 task 就看不到。"""
    assert isinstance(T.TASK_TYPES, dict)
    for name, cls in T.TASK_TYPES.items():
        assert issubclass(cls, T.Task)
        assert cls.TYPE == name


def test_package_facade_exports_base_names():
    """子模块边界是实现细节：外部只认 `from ai import tasks as T`。

    tasks 拆成包是为满足 spec §11.5 的 400 行上限，但这不该泄漏到调用方。
    """
    for name in ("Task", "TASK_TYPES", "OWNER", "claims_available",
                 "ingredient_available", "ingredient_source"):
        assert hasattr(T, name), "包门面漏了 %s" % name
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `python3 -m pytest tests/test_tasks.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'ai.tasks'`

- [ ] **Step 3: 实现基类**

创建 `ai/tasks/base.py`：

```python
"""task 的四元身份、基类与认领前置（spec §6.0 / §6.2 / §4.2）。

task 粒度 = **厨师的一趟差事**（spec §6.1）。边界切在"世界状态可明确
观测"的地方——例如倒完饭后"手持空锅"是状态里直接读得到的事实，所以
放回灶台切成独立 task，中断后看一眼手里就知道该干啥。

**task 实例 = (type, order_id, bound_resources, bound_item)**（spec §6.0）。
两个 task 只有四项全同才是同一个。FetchToPlate(Cucumber→plate_A,
order#1) 与 FetchToPlate(Cucumber→plate_B, order#2) 是两个不同的 task。

为什么身份必须这样定义（spec §6.0 给的具体 bug）：两单都缺黄瓜时，若
FetchToPlate 的完成判定不绑定具体盘，P1 把黄瓜放进盘 B 会让 AI 针对盘 A
的 task 误报 SUCCESS——AI 以为自己干完了，盘 A 其实还空着。所以下面所有
谓词都是**绑定参数上的函数**，不是全局谓词。

**不建显式 DAG、不做拓扑排序**（spec §6.2）：每个 task 自己声明"此刻我
是否可做"，执行顺序从可行性中自然涌现。饭没熟 → TakeCookedToPlate 自动
不在候选里。因此不需要记录"我做到第几步"——无状态重算、中断自愈、用户
抢单自适应全部源于这一点。

**上下游咬合**（spec §6.1）：每个 task 的 done 即下游 task 的 feasible
（1 的完成 = 4/9 的可行；4 的完成 = 5 的可行）。同一谓词写一份两处用，
前后校验口径天然一致。

Idle 不在这里（spec §6.1 最后一条）：初版把 Idle 的可行性写成"无其他可行
task"，那不是世界谓词而是对候选集本身的谓词。它是 Planner 流水线第 ⑤ 步
之后的 fallback，归 Plan 3。

**为什么 tasks 是包而非单文件**：16 个 task 的实现合计 1100 行上下，远超
本项目"无单个文件 > 400 行"的硬指标（spec §11.5）。本模块放身份与基类，
plate / cook / chop / assembly / cleanup 各放一组 task。外部一律
`from ai import tasks as T`——子模块边界是实现细节，TASK_TYPES 的完整性
由 `ai/tasks/__init__.py` 的导入保证。
"""
from typing import Any, Dict, Optional, Tuple

from ai import predicates as P
from ai import recipes as R
from ai.bt import Node

# 认领的所有者标识。AI 控制的是 P2（spec §6.7：AI 自己的意图写在 claims
# 里，不需要推断）。
OWNER = "P2"

# task 级超时的默认上限（拍）。20 Hz 下 200 拍 = 10 秒，够走完全图最长
# 路径再做一次交互；超过说明卡住了，交还 Planner（spec §7.2）。
TASK_TIMEOUT_TICKS = 200


class Task(object):
    """task 基类：不可变值对象。

    不可变的理由：Planner 每拍重算、把 task 当字典键与集合元素用
    （承诺惯性要比较"与上拍是否同一个 task"）。可变对象做键会出错。
    """

    TYPE = "Task"

    __slots__ = ("_order_id", "_resources", "_item")

    def __init__(self, order_id=None, resources=(), item=None):
        # type: (Any, Any, Any) -> None
        object.__setattr__(self, "_order_id", order_id)
        object.__setattr__(self, "_resources", tuple(resources or ()))
        object.__setattr__(self, "_item", item)

    def __setattr__(self, name, value):
        raise AttributeError("Task 是不可变值对象：%s" % name)

    @property
    def order_id(self):
        return self._order_id

    @property
    def resources(self):
        # type: () -> Tuple[Any, ...]
        return self._resources

    @property
    def item(self):
        return self._item

    @property
    def key(self):
        # type: () -> Tuple
        """spec §6.0 的四元身份。"""
        return (self.TYPE, self._order_id, self._resources, self._item)

    def __eq__(self, other):
        return isinstance(other, Task) and self.key == other.key

    def __ne__(self, other):
        return not self.__eq__(other)

    def __hash__(self):
        return hash(self.key)

    def __repr__(self):
        parts = []
        if self._item is not None:
            parts.append(str(self._item))
        parts.extend(str(r) for r in self._resources)
        inner = ", ".join(parts)
        if self._order_id is not None:
            inner = "%s, order#%s" % (inner, self._order_id) if inner \
                else "order#%s" % self._order_id
        return "%s(%s)" % (self.TYPE, inner)

    # ---------- 子类契约 ----------
    def claims(self):
        # type: () -> Tuple[Tuple[Any, str], ...]
        """本 task 需要的 (resource_id, action) 键。"""
        raise NotImplementedError

    def feasible(self, ctx):
        # type: (Dict) -> bool
        """此刻我是否可做（spec §6.2）。按绑定参数求值。"""
        raise NotImplementedError

    def done(self, ctx):
        # type: (Dict) -> bool
        """完成判定。即下游 task 的 feasible（上下游咬合）。"""
        raise NotImplementedError

    def build(self):
        # type: () -> Node
        """构建执行树。Executor 持有它（spec §7.6），不进黑板。"""
        raise NotImplementedError


def claims_available(task, ctx):
    # type: (Task, Dict) -> bool
    """全局可行性前置（spec §4.2）：所需资源均未被他人有效认领。

    spec §4.2 原话："设计稿初版的致命缺陷：定义了认领表，却没有任何一条
    可行性谓词读它。那么 claims 是一张只写不读的表，'多锅/多台资源管理'
    这项能力没有任何落地机制。"

    这一条**优先于各 task 自己的谓词**——Planner 的可行性过滤最前面调它。

    粒度是 (resource, action)（spec §4.0）：P1 往 1 号锅放了生饭只锁
    (cooker1,"fetch_to")，(cooker1,"take_cooked") 依然开放——取熟饭
    恰恰是最有价值的接力。
    """
    bb = ctx["bb"]
    tick = ctx.get("tick") or 0
    for resource_id, action in task.claims():
        if bb.is_claimed_by_other(resource_id, action, OWNER, tick):
            return False
    return True


TASK_TYPES = {}   # type: Dict[str, type]


def _register(cls):
    # type: (type) -> type
    """登记到 TASK_TYPES。tree_viz 以它为入口枚举全部 task 类型。"""
    TASK_TYPES[cls.TYPE] = cls
    return cls


# ---------- 跨组共享谓词 ----------
# 只放**两组以上** task 都要用的。单组专用的谓词留在该组模块里
# （晾干架/脏盘在 plate.py、灶状态在 cook.py、板状态在 chop.py），
# 否则 base.py 会变成又一个什么都装的大文件。
#
# plate_by_id / plate_cell / station_at_cell 在这里的理由：plate 组定位
# 拼装格、cook 组的 TakeCookedToPlate 找目标盘、assembly 组装盘与出餐
# 都要用。放 plate.py 会让 cook.py 反向 import 它，组间依赖就乱了。

def plate_by_id(ctx, plate_id):
    # type: (Dict, Any) -> Optional[Dict]
    """按实例名取盘。取不到 = 盘不在快照里（被端走/消失）。

    资源 id 规则（spec §4.4）：盘用实例名 n 为主。
    """
    for p in ctx["w"].plates:
        if str(p.get("n")) == str(plate_id):
            return p
    return None


def plate_cell(ctx, p):
    # type: (Dict, Optional[Dict]) -> Optional[Tuple[int, int]]
    if not p:
        return None
    x, z = p.get("x"), p.get("z")
    if x is None or z is None:
        return None
    return ctx["w"].to_grid(x, z)


def station_at_cell(ctx, cell, kinds=None):
    # type: (Dict, Optional[Tuple[int, int]], Optional[Tuple[str, ...]]) -> Optional[Dict]
    """取某格上的站点。cell 是 (gx, gz)；kinds 限定类型。

    拼装格的资源 id 就是网格坐标（spec §4.4"坐标即身份"），所以要能从
    坐标反查站点 dict 以喂给原语。
    """
    if cell is None:
        return None
    for st in ctx["w"].stations:
        if st.get("gx") == cell[0] and st.get("gz") == cell[1]:
            if kinds is None or st.get("t") in kinds:
                return st
    # 快照里没有该格的站点条目：合成一个最小 dict（原语只要 n/gx/gz/x/z）
    x, z = ctx["w"].to_world(cell[0], cell[1])
    return {"t": "counter", "n": "", "gx": cell[0], "gz": cell[1],
            "x": x, "z": z}


def ingredient_available(ctx, raw):
    # type: (Dict, Optional[str]) -> bool
    """生料能否取到（手上 / 板上 / 散落 / 箱子，不含已装盘的）。

    排除已装盘的：那是别人的半成品，从盘里抠料出来不是合法操作。
    cook / chop / assembly / cleanup 四组都要用，故放在 base。
    """
    if not raw:
        return False
    loc = R.find_ingredient(ctx["w"], raw)
    return loc is not None and loc.get("kind") != "plate"


def ingredient_source(ctx, raw):
    # type: (Dict, Optional[str]) -> Optional[Dict]
    """生料的取用站点 dict。在手上时返回 None（无需再取）。"""
    loc = R.find_ingredient(ctx["w"], raw)
    if loc is None or loc.get("kind") in ("plate", "held"):
        return None
    return loc.get("st")
```

`base.py` 的 import 补 `from ai import recipes as R`。

- [ ] **Step 3b: 写包门面**

创建 `ai/tasks/__init__.py`：

```python
"""16 个 task 的身份、可行性、完成判定与树构建（spec §6.1 / §7.3）。

本包是 tasks 的唯一对外入口。调用方一律：

    from ai import tasks as T
    T.Chop("9", ("board_01",), "Cucumber")
    T.TASK_TYPES          # 全部 16 个 task 类型

**不要直接 import 子模块**——plate / cook / chop / assembly / cleanup
的分组边界是实现细节（为满足 spec §11.5 的 400 行上限而拆），不是 API。

下面的 `from ... import *` 看着粗糙，但它承担一个具体职责：**导入子模块
即触发其中的 @_register，TASK_TYPES 才完整**。少一行就静默少一组 task，
而 tree_viz 以 TASK_TYPES 为入口枚举——漏掉的 task 不会报错，只是看不到。
test_task_types_registry_is_complete 用计数钉住这一点。
"""
from ai.tasks.base import (OWNER, TASK_TYPES, TASK_TIMEOUT_TICKS, Task,
                           claims_available, ingredient_available,
                           ingredient_source)
from ai.tasks.plate import *        # noqa: F401,F403
from ai.tasks.cook import *         # noqa: F401,F403
from ai.tasks.chop import *         # noqa: F401,F403
from ai.tasks.assembly import *     # noqa: F401,F403
from ai.tasks.cleanup import *      # noqa: F401,F403
```

**Task 12 阶段五个子模块还不存在**，所以此刻 `__init__.py` 只写 `base` 那一行，后续每个 task 组完成时补上自己那行。Task 17 Step 5 有一道 grep 卡口核对五行齐全。

- [ ] **Step 4: 运行测试，确认通过**

Run: `python3 -m pytest tests/test_tasks.py -v`
Expected: PASS，15 个测试全绿（`TASK_TYPES` 此刻为空，registry 测试会空转通过）。

- [ ] **Step 5: 运行全量测试**

Run: `python3 -m pytest -v`
Expected: 全绿。

- [ ] **Step 6: 提交**

```bash
git add ai/tasks/ tests/test_tasks.py
git commit -m "feat(tasks): task 四元身份与基类 + claims 可行性前置

task 实例 = (type, order_id, bound_resources, bound_item)（spec §6.0）。
身份必须这样定义的理由是 spec 给的具体 bug：两单都缺黄瓜时，若
FetchToPlate 的完成判定不绑定具体盘，P1 把黄瓜放进盘 B 会让 AI 针对
盘 A 的 task 误报 SUCCESS——AI 以为干完了，盘 A 其实还空着。

Task 是不可变值对象：Planner 每拍重算、把它当字典键与集合元素用
（承诺惯性要比较"与上拍是否同一个 task"），可变对象做键会出错。

claims_available 补 spec §4.2 点名的致命缺陷："定义了认领表，却没有
任何一条可行性谓词读它——那么 claims 是一张只写不读的表，'多锅/多台
资源管理'这项能力没有任何落地机制。"它优先于各 task 自己的谓词。

粒度是 (resource, action) 而非 resource（spec §4.0）：P1 往 1 号锅放了
生饭只锁 fetch_to，take_cooked 依然开放——取熟饭恰恰是最有价值的接力。

tasks 做成包而非单文件：16 个 task 合计 1100 行上下，远超 spec §11.5 的
400 行上限。base 放身份与基类，后续五个子模块各放一组。子模块边界是实现
细节，调用方一律 from ai import tasks as T。

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---
### Task 13: 盘管理三 task

**Files:**
- Create: `ai/tasks/plate.py`
- Modify: `ai/tasks/__init__.py`（补一行导入）
- Modify: `tests/test_tasks.py`（追加）

**Interfaces:**
- Consumes: `Task` / `_register` / `OWNER`（Task 12）、`stations.find_plate_stack` / `find_station`（Plan 1.5）、`predicates.plate_contents` / `plate_count` / `held_is_plate` / `hands_free`（Plan 1）、`primitives.InteractWith` / `HoldUntil` / `Settle`（Task 7-10）
- Produces（spec §6.1 第 1 / 2 / 14 号）：
  - `PlaceCleanPlate(order_id, resources=(plate_id, counter_cell))`
  - `WashPlate(resources=(sink_name,))`
  - `ResetPlate(order_id, resources=(plate_id, counter_cell))`

| # | Task | 可行性 | 完成判定 | 认领 |
|---|---|---|---|---|
| 1 | `PlaceCleanPlate` | 该单需装配 且 无已就位认领盘 且 有干净盘 | 认领盘 `(gx,gz)` == 拼装格 | 盘 + 拼装格 |
| 2 | `WashPlate` | 无干净盘 且 有脏盘 | 干净盘出现在**晾干架**或盘堆 | 洗碗池 |
| 14 | `ResetPlate` | 盘内容与**任何**现存订单需求都不匹配 | 盘空 或 已移出拼装格 | 盘 + 拼装格 |

**`WashPlate` 的取盘位置**（spec §6.1 第 2 号注）："洗完的盘从**晾干架**出来，不是盘堆。`task.py:112` 已把 `t=="plate_return"` 且名字含 `drying` 的站点算作可取盘位（实测帧确有 `{'t':'plate_return','n':'DryingPart'}`）。初版完成判定只写'出现可用干净盘'没说去哪取，实施时会漏。" Plan 1.5 的 `stations._is_plate_source` 已实现这条。

**`ResetPlate` 是初版遗漏的必要能力**（spec §6.1 第 14 号）：两个场景会楔死拼装格——① 盘里被放错料，对账只"失效认领"，那只错料盘仍停在拼装格上；② 端着料齐的盘走向出餐口时订单过期，手里一只满盘。没有这个 task 上述状态无法恢复。

**拼装格 `counter_cell` 从哪来**：`(gx, gz)` 元组，由 Planner 的候选生成绑定（spec §9：`PLATE_COUNTERS` 的替代品是 Planner 的多拼装位候选生成 + 认领，归 Plan 3）。本计划里它是**构造参数**，task 不自己找。

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_tasks.py`：

```python
# ---------- 盘管理（spec §6.1 第 1/2/14 号）----------

RECIPES = [
    {"name": "Sushi_Cucumber", "comp":
        ["Seaweed", "BoiledSushiRice", "Cucumber"], "cook": "null"},
    {"name": "BoiledSushiRice", "comp": ["SushiRice"], "cook": "Pot"},
    {"name": "Seaweed", "comp": [], "cook": "null"},
    {"name": "Cucumber", "comp": [], "cook": "null"},
    {"name": "SushiRice", "comp": [], "cook": "null"},
]
ORDER = {"id": "1", "dish": "Sushi_Cucumber",
         "composition": ["Seaweed", "BoiledSushiRice", "Cucumber"]}

COUNTER = {"t": "counter", "n": "assembly", "gx": 7, "gz": 0,
           "x": 7 * 1.2, "z": 0.0}
STACK = {"t": "plate_stack", "n": "stack1", "gx": -1, "gz": 0,
         "x": -1.2, "z": 0.0}
DRYING = {"t": "plate_return", "n": "DryingPart", "gx": -3, "gz": 0,
          "x": -3 * 1.2, "z": 0.0}
SINK = {"t": "washing", "n": "sink1", "gx": -5, "gz": 0,
        "x": -5 * 1.2, "z": 0.0}


def plate(n, gx, gz, contents=(), count=None):
    return {"n": n, "x": gx * 1.2, "z": gz * 1.2,
            "contents": list(contents),
            "count": len(contents) if count is None else count}


def test_place_clean_plate_claims_plate_and_counter():
    """认领盘 + 拼装格（spec §6.1 第 1 号）。"""
    t = T.PlaceCleanPlate(order_id="1", resources=("p1", (7, 0)))
    assert set(t.claims()) == {("p1", "place"), ((7, 0), "assemble")}


def test_place_clean_plate_feasible_when_plate_clean_and_counter_free():
    w = FakeWorld(stations=[COUNTER, STACK], recipes=RECIPES,
                  orders=[ORDER], plates=[plate("p1", -1, 0)])
    ctx, _ = make_ctx(w)
    t = T.PlaceCleanPlate(order_id="1", resources=("p1", (7, 0)))
    assert t.feasible(ctx) is True


def test_place_clean_plate_infeasible_when_plate_not_clean():
    """盘里已有料 → 不是"干净盘"，不该再拿它去摆。"""
    w = FakeWorld(stations=[COUNTER, STACK], recipes=RECIPES,
                  orders=[ORDER], plates=[plate("p1", -1, 0, ["Seaweed"])])
    ctx, _ = make_ctx(w)
    t = T.PlaceCleanPlate(order_id="1", resources=("p1", (7, 0)))
    assert t.feasible(ctx) is False


def test_place_clean_plate_infeasible_when_plate_gone():
    """认领的盘不在快照里（被端走）→ 不可行（spec §4.1 触发点 3）。"""
    w = FakeWorld(stations=[COUNTER, STACK], recipes=RECIPES,
                  orders=[ORDER], plates=[])
    ctx, _ = make_ctx(w)
    t = T.PlaceCleanPlate(order_id="1", resources=("p1", (7, 0)))
    assert t.feasible(ctx) is False


def test_place_clean_plate_infeasible_when_already_done():
    """盘已在拼装格上 → 已完成，不再可行（上下游咬合）。"""
    w = FakeWorld(stations=[COUNTER], recipes=RECIPES, orders=[ORDER],
                  plates=[plate("p1", 7, 0)])
    ctx, _ = make_ctx(w)
    t = T.PlaceCleanPlate(order_id="1", resources=("p1", (7, 0)))
    assert t.done(ctx) is True
    assert t.feasible(ctx) is False


def test_place_clean_plate_done_binds_specific_plate():
    """Review Focus 3：完成判定绑定具体盘。

    盘 B 到了拼装格不该让针对盘 A 的 task 报成功。
    """
    w = FakeWorld(stations=[COUNTER], recipes=RECIPES, orders=[ORDER],
                  plates=[plate("pB", 7, 0), plate("pA", -1, 0)])
    ctx, _ = make_ctx(w)
    assert T.PlaceCleanPlate("1", ("pA", (7, 0))).done(ctx) is False
    assert T.PlaceCleanPlate("1", ("pB", (7, 0))).done(ctx) is True


def test_place_clean_plate_tree_is_two_interactions():
    """spec §7.3：树退化为几步线性顺序。拿盘 + 放盘。"""
    t = T.PlaceCleanPlate(order_id="1", resources=("p1", (7, 0)))
    tree = t.build()
    assert isinstance(tree, PR.Timeout)
    assert isinstance(tree.child, Sequence)
    assert len(tree.child.children) == 2


def test_wash_plate_claims_sink():
    assert T.WashPlate(resources=("sink1",)).claims() == (("sink1", "wash"),)


def test_wash_plate_feasible_when_dirty_exists_and_no_clean():
    w = FakeWorld(stations=[SINK, DRYING], recipes=RECIPES, orders=[ORDER],
                  items=[{"n": "DirtyPlateStack", "x": 0.0, "z": -12.0}],
                  plates=[])
    ctx, _ = make_ctx(w)
    assert T.WashPlate(resources=("sink1",)).feasible(ctx) is True


def test_wash_plate_infeasible_when_clean_plate_available():
    """有干净盘就别洗——洗盘很慢，是最后手段。"""
    w = FakeWorld(stations=[SINK, DRYING, STACK], recipes=RECIPES,
                  orders=[ORDER],
                  items=[{"n": "DirtyPlateStack", "x": 0.0, "z": -12.0}],
                  plates=[plate("p1", -1, 0)])
    ctx, _ = make_ctx(w)
    assert T.WashPlate(resources=("sink1",)).feasible(ctx) is False


def test_wash_plate_infeasible_without_dirty_plates():
    w = FakeWorld(stations=[SINK, DRYING], recipes=RECIPES, orders=[ORDER],
                  items=[], plates=[])
    ctx, _ = make_ctx(w)
    assert T.WashPlate(resources=("sink1",)).feasible(ctx) is False


def test_wash_plate_done_requires_clean_plate_at_drying_or_stack():
    """spec §6.1 第 2 号注：洗完的盘从**晾干架**出来，不是盘堆。

    初版完成判定只写"出现可用干净盘"没说去哪取，实施时会漏。
    """
    w = FakeWorld(stations=[SINK, DRYING], recipes=RECIPES, orders=[ORDER],
                  plates=[plate("p1", -3, 0)])     # 在晾干架位置
    ctx, _ = make_ctx(w)
    assert T.WashPlate(resources=("sink1",)).done(ctx) is True

    w2 = FakeWorld(stations=[SINK, DRYING], recipes=RECIPES, orders=[ORDER],
                   plates=[plate("p1", 2, 0)])     # 在别处的空盘
    ctx2, _ = make_ctx(w2)
    assert T.WashPlate(resources=("sink1",)).done(ctx2) is False


def test_reset_plate_feasible_when_contents_match_no_order():
    """spec §6.1 第 14 号：盘内容与任何现存订单需求都不匹配 → 清盘。

    场景①：盘里被放错料，错料盘停在拼装格上占着位置。
    """
    w = FakeWorld(stations=[COUNTER], recipes=RECIPES, orders=[ORDER],
                  plates=[plate("p1", 7, 0, ["Lettuce"])])
    ctx, _ = make_ctx(w)
    t = T.ResetPlate(order_id=None, resources=("p1", (7, 0)))
    assert t.feasible(ctx) is True


def test_reset_plate_infeasible_when_contents_fit_some_order():
    """内容还对得上某单 → 别清，那是半成品。"""
    w = FakeWorld(stations=[COUNTER], recipes=RECIPES, orders=[ORDER],
                  plates=[plate("p1", 7, 0, ["Seaweed"])])
    ctx, _ = make_ctx(w)
    t = T.ResetPlate(order_id=None, resources=("p1", (7, 0)))
    assert t.feasible(ctx) is False


def test_reset_plate_infeasible_when_plate_empty():
    """空盘没什么要清的。"""
    w = FakeWorld(stations=[COUNTER], recipes=RECIPES, orders=[ORDER],
                  plates=[plate("p1", 7, 0)])
    ctx, _ = make_ctx(w)
    assert T.ResetPlate(None, ("p1", (7, 0))).feasible(ctx) is False


def test_reset_plate_feasible_when_order_expired_with_full_plate():
    """场景②：端着料齐的盘，订单过期了（orders 为空）。

    没有这个 task，手里的满盘无法恢复（spec §6.1 第 14 号）。
    内容用实测帧的真实词汇。
    """
    w = FakeWorld(stations=[COUNTER], recipes=RECIPES, orders=[],
                  plates=[plate("p1", 7, 0,
                                ["CookedCompositeAssembledNode",
                                 "Cucumber", "Seaweed"])])
    ctx, _ = make_ctx(w)
    assert T.ResetPlate(None, ("p1", (7, 0))).feasible(ctx) is True


def test_reset_plate_spares_plate_with_cooked_token():
    """绝不能把正在正常装配的盘扣进垃圾桶。

    盘里刚倒进熟饭 → contents 是 ['CookedCompositeAssembledNode']，
    而订单需求写的是 BoiledSushiRice。若 any_order_wants 做字面比较，
    这张盘会被判成"对不上任何订单"，ResetPlate 把队友刚倒好的饭倒掉。
    """
    w = FakeWorld(stations=[COUNTER], recipes=RECIPES, orders=[ORDER],
                  plates=[plate("p1", 7, 0,
                                ["CookedCompositeAssembledNode"])])
    ctx, _ = make_ctx(w)
    assert T.ResetPlate(None, ("p1", (7, 0))).feasible(ctx) is False


def test_reset_plate_spares_fully_assembled_plate():
    """做完的三料盘更不能清——它该走 DeliverPlate。"""
    w = FakeWorld(stations=[COUNTER], recipes=RECIPES, orders=[ORDER],
                  plates=[plate("p1", 7, 0,
                                ["CookedCompositeAssembledNode",
                                 "Cucumber", "Seaweed"])])
    ctx, _ = make_ctx(w)
    assert T.ResetPlate(None, ("p1", (7, 0))).feasible(ctx) is False


def test_reset_plate_done_when_empty_or_moved_off_counter():
    w = FakeWorld(stations=[COUNTER], recipes=RECIPES, orders=[ORDER],
                  plates=[plate("p1", 7, 0)])
    ctx, _ = make_ctx(w)
    assert T.ResetPlate(None, ("p1", (7, 0))).done(ctx) is True

    w2 = FakeWorld(stations=[COUNTER], recipes=RECIPES, orders=[ORDER],
                   plates=[plate("p1", 2, 0, ["Lettuce"])])
    ctx2, _ = make_ctx(w2)
    assert T.ResetPlate(None, ("p1", (7, 0))).done(ctx2) is True


def test_plate_tasks_are_registered():
    for name in ("PlaceCleanPlate", "WashPlate", "ResetPlate"):
        assert name in T.TASK_TYPES
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `python3 -m pytest tests/test_tasks.py -v -k "place_clean or wash_plate or reset_plate"`
Expected: FAIL，`AttributeError: module 'ai.tasks' has no attribute 'PlaceCleanPlate'`

- [ ] **Step 3: 实现三个共享谓词与三个 task**

创建 `ai/tasks/plate.py`：

```python
"""盘管理三 task：PlaceCleanPlate / WashPlate / ResetPlate（spec §6.1 第 1/2/14 号）。

本模块只放盘管理这一组，共享的身份机制与跨组谓词在 base.py。
外部不直接 import 本模块，一律 `from ai import tasks as T`
（分组边界是实现细节，见 ai/tasks/__init__.py）。
"""
from typing import Any, Dict, List, Optional, Tuple

from ai import predicates as P
from ai import primitives as PR
from ai import stations as S
from ai.bt import Node, Sequence
from ai.tasks.base import (TASK_TIMEOUT_TICKS, Task, _register, plate_by_id,
                           plate_cell, station_at_cell)

# ---------- 盘相关谓词（仅本组用） ----------

def any_order_wants(ctx, contents):
    # type: (Dict, Tuple[str, ...]) -> bool
    """盘里这堆料还对得上某张现存订单吗（ResetPlate 的可行性）。

    判据是"盘里的每样料都在某单的需求里"。多放一样不属于该单的料就
    对不上了——那张盘再加料也做不成那道菜。

    比较必须在**装盘态词汇**里做（book.plate_token），不能拿盘内容去
    比订单需求名：盘里记的是 CookedCompositeAssembledNode 而订单写的是
    BoiledSushiRice。字面比较会让一张正在正常装配的盘被判成"对不上任何
    订单"，于是 ResetPlate 把队友刚倒好的饭扣进垃圾桶。
    """
    if not contents:
        return True          # 空盘无所谓，交给 ResetPlate 自己判不可行
    book = ctx["book"]
    for order in ctx["w"].orders:
        need = set()
        for r in book.requirements(order):
            token = book.plate_token(r)
            if token:
                need.add(token)
        if all(c in need for c in contents):
            return True
    return False


def clean_plate_exists(ctx):
    # type: (Dict) -> bool
    """快照里有没有可用的干净空盘。"""
    for p in ctx["w"].plates:
        if P.plate_count(p) == 0:
            return True
    return False


def dirty_plate_exists(ctx):
    # type: (Dict) -> bool
    """脏盘摞（items 里的 DirtyPlateStack）。"""
    for it in ctx["w"].items:
        if "dirtyplate" in str(it.get("n") or "").lower():
            return True
    return False


@_register
class PlaceCleanPlate(Task):
    """第 1 号：拿一块干净盘摆到拼装格。

    resources = (plate_id, counter_cell)。counter_cell 是 (gx, gz)，由
    Planner 的候选生成绑定——spec §9 记 PLATE_COUNTERS 的替代品是
    "Planner 的多拼装位候选生成 + 认领，不是一个 find 函数"（归 Plan 3）。
    """

    TYPE = "PlaceCleanPlate"

    @property
    def plate_id(self):
        return self._resources[0]

    @property
    def counter_cell(self):
        return self._resources[1]

    def claims(self):
        return ((self.plate_id, "place"), (self.counter_cell, "assemble"))

    def done(self, ctx):
        # type: (Dict) -> bool
        """认领盘的格 == 拼装格（spec §6.1）。**绑定具体盘**求值。"""
        p = plate_by_id(ctx, self.plate_id)
        return plate_cell(ctx, p) == tuple(self.counter_cell)

    def feasible(self, ctx):
        # type: (Dict) -> bool
        if self.done(ctx):
            return False
        p = plate_by_id(ctx, self.plate_id)
        if p is None:
            return False                   # 盘被端走/消失
        return P.plate_count(p) == 0       # 必须是干净空盘

    def build(self):
        # type: () -> Node
        plate_id, cell = self.plate_id, self.counter_cell
        return PR.Timeout(Sequence([
            PR.InteractWith(
                lambda c: plate_by_id(c, plate_id), "pickup",
                lambda c: P.held_is_plate(c["chef"])),
            PR.InteractWith(
                lambda c: station_at_cell(c, cell), "placement",
                lambda c: plate_cell(
                    c, plate_by_id(c, plate_id)) == tuple(cell)),
        ]), TASK_TIMEOUT_TICKS)


@_register
class WashPlate(Task):
    """第 2 号：洗一块脏盘。

    完成判定是"干净盘出现在**晾干架**或盘堆"——spec §6.1 第 2 号注：
    "洗完的盘从晾干架出来，不是盘堆……初版完成判定只写'出现可用干净盘'
    没说去哪取，实施时会漏。"Plan 1.5 的 stations._is_plate_source 已
    把 t=="plate_return" 且名含 drying 的站点算作可取盘位（实测帧确有
    {'t':'plate_return','n':'DryingPart'}）。
    """

    TYPE = "WashPlate"

    @property
    def sink_id(self):
        return self._resources[0]

    def claims(self):
        return ((self.sink_id, "wash"),)

    def done(self, ctx):
        # type: (Dict) -> bool
        src_cells = set()
        for st in ctx["w"].stations:
            if not S._is_plate_source(st):
                continue
            gx, gz = st.get("gx"), st.get("gz")
            if gx is not None and gz is not None:
                src_cells.add((gx, gz))
        if not src_cells:
            return False
        for p in ctx["w"].plates:
            if P.plate_count(p) != 0:
                continue
            if plate_cell(ctx, p) in src_cells:
                return True
        return False

    def feasible(self, ctx):
        # type: (Dict) -> bool
        if clean_plate_exists(ctx):
            return False      # 有干净盘就别洗——洗盘很慢，是最后手段
        return dirty_plate_exists(ctx)

    def build(self):
        # type: () -> Node
        sink_id = self.sink_id
        done = self.done

        def sink(c):
            return S.find_station(c["w"].stations, "washing") or \
                station_at_cell(c, None)

        def dirty(c):
            for it in c["w"].items:
                if "dirtyplate" in str(it.get("n") or "").lower():
                    return it
            return None

        return PR.Timeout(Sequence([
            PR.InteractWith(dirty, "pickup",
                            lambda c: not P.hands_free(c["chef"]),
                            face=False),
            PR.InteractWith(sink, "placement",
                            lambda c: P.hands_free(c["chef"])),
            PR.HoldUntil("chop", done, timeout=WASH_TIMEOUT_TICKS),
        ]), TASK_TIMEOUT_TICKS)


@_register
class ResetPlate(Task):
    """第 14 号：清掉内容与任何现存订单都不匹配的盘。

    spec §6.1 原话："第 14 号 ResetPlate 是新增的必要能力（初版遗漏）。
    两个场景会楔死拼装格：① 盘里被放错料 → 对账只'失效认领'，那只错料
    盘仍停在拼装格上，PlaceCleanPlate 于是去放另一只盘，几轮后无格可用；
    ② AI 端着料齐的盘走向出餐口时订单过期 → 承诺被弃 → 手里一只满盘。
    没有这个 task，上述两种状态无法恢复。"

    清法：端起盘送到垃圾桶倒掉（盘里的料只能倒垃圾桶），空盘放回盘堆。
    完成判定放宽为"盘空 **或** 已移出拼装格"——后者已经解除了楔死。
    """

    TYPE = "ResetPlate"

    @property
    def plate_id(self):
        return self._resources[0]

    @property
    def counter_cell(self):
        return self._resources[1]

    def claims(self):
        return ((self.plate_id, "reset"), (self.counter_cell, "assemble"))

    def done(self, ctx):
        # type: (Dict) -> bool
        p = plate_by_id(ctx, self.plate_id)
        if p is None:
            return True       # 盘不见了，楔死也解除了
        if P.plate_count(p) == 0:
            return True
        return plate_cell(ctx, p) != tuple(self.counter_cell)

    def feasible(self, ctx):
        # type: (Dict) -> bool
        p = plate_by_id(ctx, self.plate_id)
        if p is None:
            return False
        contents = P.plate_contents(p)
        if not contents:
            return False      # 空盘没什么要清的
        return not any_order_wants(ctx, contents)

    def build(self):
        # type: () -> Node
        plate_id = self.plate_id

        def bin_st(c):
            return S.find_station(c["w"].stations, "bin",
                                  (c["chef"]["x"], c["chef"]["z"]))

        return PR.Timeout(Sequence([
            PR.InteractWith(lambda c: plate_by_id(c, plate_id), "pickup",
                            lambda c: P.held_is_plate(c["chef"])),
            PR.InteractWith(
                bin_st, "placement",
                lambda c: P.plate_count(plate_by_id(c, plate_id)) == 0),
        ]), TASK_TIMEOUT_TICKS)
```

在模块常量区补：

```python
# 洗盘按住交互键的上限（拍）。旧实现用 confirm_wait >= 20 做兜底，
# spec §3.4 指出 20 Hz 下那是约 1 秒而非 4 秒。洗一摞盘实测要数秒，
# 故给 120 拍（6 秒）。HoldUntil 的 timeout 无默认值、必须显式传。
WASH_TIMEOUT_TICKS = 120
```

- [ ] **Step 3b: 把本组挂上包门面**

在 `ai/tasks/__init__.py` 追加一行（顺序跟随 spec §6.1 的分组次序）：

```python
from ai.tasks.plate import *        # noqa: F401,F403
```

漏这一行不会报错，只是 `TASK_TYPES` 静默少这三个 task，
`tree_viz` 就看不到它们——所以下一步的 registry 断言是必要的卡口。

- [ ] **Step 4: 运行测试，确认通过**

Run: `python3 -m pytest tests/test_tasks.py -v`
Expected: PASS，35 个测试全绿。

- [ ] **Step 5: 运行全量测试**

Run: `python3 -m pytest -v`
Expected: 全绿。

- [ ] **Step 6: 提交**

```bash
git add ai/tasks/ tests/test_tasks.py
git commit -m "feat(tasks): 盘管理三 task（PlaceCleanPlate/WashPlate/ResetPlate）

ResetPlate 是 spec §6.1 第 14 号点明的"新增的必要能力（初版遗漏）"：
两个场景会楔死拼装格——① 盘里被放错料，对账只"失效认领"，错料盘仍停
在拼装格上，PlaceCleanPlate 于是去放另一只盘，几轮后无格可用；② 端着
料齐的盘走向出餐口时订单过期，手里一只满盘。没有它这两种状态无法恢复。

WashPlate 的完成判定是"干净盘出现在晾干架或盘堆"，不是"出现可用干净
盘"——spec §6.1 第 2 号注："洗完的盘从晾干架出来，不是盘堆……初版完成
判定只写'出现可用干净盘'没说去哪取，实施时会漏。"复用 Plan 1.5 的
stations._is_plate_source（实测帧确有 {'t':'plate_return','n':'DryingPart'}）。

所有谓词按绑定参数求值：test_place_clean_plate_done_binds_specific_plate
钉住"盘 B 到了拼装格不让针对盘 A 的 task 报成功"（spec §6.0 的具体 bug）。

拼装格 counter_cell 是构造参数而非 task 自己找：spec §9 记 PLATE_COUNTERS
的替代品是 Planner 的多拼装位候选生成 + 认领（归 Plan 3）。

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---
### Task 14: 烹饪四 task

**Files:**
- Create: `ai/tasks/cook.py`
- Modify: `ai/tasks/__init__.py`（补一行导入）
- Modify: `tests/test_tasks.py`（追加）

**Interfaces:**
- Consumes: Task 12-13 的全部产出、`predicates.cooker_for` / `cooker_cooked` / `cooker_burning` / `cooker_has_pot` / `cooker_started` / `held_is_cookware`（Plan 1 + Task 4）、`recipes.find_ingredient` / `RecipeBook.raw_of`（Task 5）、`placement.find_drop_station`（Task 6）
- Produces（spec §6.1 第 3 / 4 / 5 / 6 号）：
  - `FetchToCooker(order_id, resources=(cooker_name,), item=cooked_name)`
  - `TakeCookedToPlate(order_id, resources=(cooker_name, plate_id), item=cooked_name)`
  - `ReturnPot(resources=(cooker_name,))`
  - `RescueCooked(resources=(cooker_name,))`

| # | Task | 可行性 | 完成判定 | 认领 |
|---|---|---|---|---|
| 3 | `FetchToCooker` | 该单需熟料 且 该灶有空锅 且 生料可得 | 该灶 `isCooking` | 灶 |
| 4 | `TakeCookedToPlate` | 该灶 `cooked` 且 **存在缺此料的已就位盘** | 盘 `contents` 含该熟料 | 灶 |
| 5 | `ReturnPot` | **手持空锅** | 手空 且 该灶有锅 | 灶 |
| 6 | `RescueCooked` | 该灶 `cooked` 且 无盘需要它 | 锅已离灶（烧糊计时停止） | 灶 |

**第 5、12 号的可行性是纯观测事实**（spec §6.1 第一个设计点）："'手持空锅' / '地上有锅'，不依赖任何记忆。这是火情丢锅能自动回收的机制——`AI_REVIEW.md` §4 那套专门的 `RecoverPotAction` 恢复逻辑和 `dropped_cooked_pot` 全局 bool **都不再需要**。"这个替代的正确性押在 Task 4 的 `held_is_cookware` 上。

**第 4 号可行性已放宽**（spec §6.1）："改为'存在缺此料的盘'而非'**我这单**需要'，自然包含 P1 那单的盘，接力协助免费获得。但须过第 ③ 步协作过滤"（协作过滤归 Plan 4）。

**第 6 号 `RescueCooked`**（spec §6.1）："熟料在锅里同时是**资产**和**负债**（`cooked→burning→onFire` 计时在跑）。已确认的游戏机制：**把锅从灶台端起即停止烧糊，不必倒进盘子**。所以本 task 成本极低——端起锅放到就近台面。"

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_tasks.py`：

```python
# ---------- 烹饪（spec §6.1 第 3/4/5/6 号）----------

COOKER = {"t": "cooking", "n": "workstation_cooker_01", "gx": 5, "gz": 0,
          "x": 5 * 1.2, "z": 0.0}
COOKER2 = {"t": "cooking", "n": "workstation_cooker_01 (2)", "gx": 5,
           "gz": 2, "x": 5 * 1.2, "z": 2 * 1.2}
RICE_CRATE = {"t": "crate", "n": "crate_rice", "ing": "SushiRice",
              "chop": None, "chop_stages": None,
              "gx": -5, "gz": 0, "x": -5 * 1.2, "z": 0.0}


def cooker(name, **kw):
    base = {"n": name, "x": 5 * 1.2, "z": 0.0, "isCooking": False,
            "isTurnedOn": False, "cooked": None, "burning": False,
            "onFire": False}
    base.update(kw)
    return base


def test_fetch_to_cooker_claims_cooker():
    t = T.FetchToCooker("1", ("workstation_cooker_01",), "BoiledSushiRice")
    assert t.claims() == (("workstation_cooker_01", "fetch_to"),)


def test_fetch_to_cooker_feasible_with_empty_pot_and_raw():
    """空锅（cooked=False 表示有锅但没熟）+ 生料在箱子里 → 可做。"""
    w = FakeWorld(stations=[COOKER, RICE_CRATE], recipes=RECIPES,
                  orders=[ORDER],
                  cookers=[cooker("workstation_cooker_01", cooked=False)])
    ctx, _ = make_ctx(w)
    t = T.FetchToCooker("1", ("workstation_cooker_01",), "BoiledSushiRice")
    assert t.feasible(ctx) is True


def test_fetch_to_cooker_infeasible_without_pot():
    """cooked=None 表示灶上没锅（插件约定）→ 没锅没法下料。"""
    w = FakeWorld(stations=[COOKER, RICE_CRATE], recipes=RECIPES,
                  orders=[ORDER],
                  cookers=[cooker("workstation_cooker_01", cooked=None)])
    ctx, _ = make_ctx(w)
    t = T.FetchToCooker("1", ("workstation_cooker_01",), "BoiledSushiRice")
    assert t.feasible(ctx) is False


def test_fetch_to_cooker_infeasible_when_already_cooking():
    """已在煮 → 完成了，不再可行（上下游咬合）。"""
    w = FakeWorld(stations=[COOKER, RICE_CRATE], recipes=RECIPES,
                  orders=[ORDER],
                  cookers=[cooker("workstation_cooker_01",
                                  isCooking=True, cooked=False)])
    ctx, _ = make_ctx(w)
    t = T.FetchToCooker("1", ("workstation_cooker_01",), "BoiledSushiRice")
    assert t.done(ctx) is True
    assert t.feasible(ctx) is False


def test_fetch_to_cooker_infeasible_when_on_fire():
    """着火的锅不放料（spec §5.3："Normal 模式下火的存在必须影响规划"）。"""
    w = FakeWorld(stations=[COOKER, RICE_CRATE], recipes=RECIPES,
                  orders=[ORDER],
                  cookers=[cooker("workstation_cooker_01", cooked=False,
                                  onFire=True)])
    ctx, _ = make_ctx(w)
    t = T.FetchToCooker("1", ("workstation_cooker_01",), "BoiledSushiRice")
    assert t.feasible(ctx) is False


def test_fetch_to_cooker_infeasible_without_raw_source():
    """生料既不在箱子也不在场上 → 不可做。"""
    w = FakeWorld(stations=[COOKER], recipes=RECIPES, orders=[ORDER],
                  cookers=[cooker("workstation_cooker_01", cooked=False)])
    ctx, _ = make_ctx(w)
    t = T.FetchToCooker("1", ("workstation_cooker_01",), "BoiledSushiRice")
    assert t.feasible(ctx) is False


def test_fetch_to_cooker_binds_specific_cooker():
    """绑定具体灶：1 号灶在煮不该让 2 号灶的 task 报完成。"""
    w = FakeWorld(stations=[COOKER, COOKER2, RICE_CRATE], recipes=RECIPES,
                  orders=[ORDER],
                  cookers=[cooker("workstation_cooker_01", isCooking=True,
                                  cooked=False),
                           cooker("workstation_cooker_01 (2)",
                                  cooked=False)])
    ctx, _ = make_ctx(w)
    assert T.FetchToCooker("1", ("workstation_cooker_01",),
                           "BoiledSushiRice").done(ctx) is True
    assert T.FetchToCooker("1", ("workstation_cooker_01 (2)",),
                           "BoiledSushiRice").done(ctx) is False


def test_take_cooked_feasible_when_cooked_and_plate_lacks():
    w = FakeWorld(stations=[COOKER, COUNTER], recipes=RECIPES,
                  orders=[ORDER],
                  cookers=[cooker("workstation_cooker_01", cooked=True)],
                  plates=[plate("p1", 7, 0, ["Seaweed"])])
    ctx, _ = make_ctx(w)
    t = T.TakeCookedToPlate("1", ("workstation_cooker_01", "p1"),
                            "BoiledSushiRice")
    assert t.feasible(ctx) is True


def test_take_cooked_infeasible_when_not_cooked():
    w = FakeWorld(stations=[COOKER, COUNTER], recipes=RECIPES,
                  orders=[ORDER],
                  cookers=[cooker("workstation_cooker_01",
                                  isCooking=True, cooked=False)],
                  plates=[plate("p1", 7, 0, ["Seaweed"])])
    ctx, _ = make_ctx(w)
    t = T.TakeCookedToPlate("1", ("workstation_cooker_01", "p1"),
                            "BoiledSushiRice")
    assert t.feasible(ctx) is False


def test_take_cooked_infeasible_when_burning():
    """烧糊的料倒进盘里会毁掉整盘 —— 该走 Discard。"""
    w = FakeWorld(stations=[COOKER, COUNTER], recipes=RECIPES,
                  orders=[ORDER],
                  cookers=[cooker("workstation_cooker_01", cooked=True,
                                  burning=True)],
                  plates=[plate("p1", 7, 0, ["Seaweed"])])
    ctx, _ = make_ctx(w)
    t = T.TakeCookedToPlate("1", ("workstation_cooker_01", "p1"),
                            "BoiledSushiRice")
    assert t.feasible(ctx) is False


def test_take_cooked_infeasible_when_plate_already_has_it():
    """盘里已有熟料 → done 成立、feasible 不成立。

    ⚠️ 盘内容用的是实测词汇 CookedCompositeAssembledNode，不是熟料名
    BoiledSushiRice——801 帧实测盘里从不出现熟料的字面名。写
    ["BoiledSushiRice"] 的测试会过，但那是在测一个现实中不存在的状态。
    """
    w = FakeWorld(stations=[COOKER, COUNTER], recipes=RECIPES,
                  orders=[ORDER],
                  cookers=[cooker("workstation_cooker_01", cooked=True)],
                  plates=[plate("p1", 7, 0,
                                ["CookedCompositeAssembledNode"])])
    ctx, _ = make_ctx(w)
    t = T.TakeCookedToPlate("1", ("workstation_cooker_01", "p1"),
                            "BoiledSushiRice")
    assert t.done(ctx) is True
    assert t.feasible(ctx) is False


def test_take_cooked_not_done_on_literal_cooked_name():
    """反向钉死：盘里字面写着熟料名**不算**倒进去了。

    这个状态在真实帧里不存在。若实现退回字面比较，这个测试仍会过，
    所以它配合上一个测试一起用：上面证明 token 能认出，这里证明实现
    不是简单地"两种都认"而掩盖了问题。
    """
    w = FakeWorld(stations=[COOKER, COUNTER], recipes=RECIPES,
                  orders=[ORDER],
                  cookers=[cooker("workstation_cooker_01", cooked=True)],
                  plates=[plate("p1", 7, 0, ["BoiledSushiRice"])])
    ctx, _ = make_ctx(w)
    t = T.TakeCookedToPlate("1", ("workstation_cooker_01", "p1"),
                            "BoiledSushiRice")
    assert t.done(ctx) is False


def test_take_cooked_done_binds_specific_plate():
    """Review Focus 3：P1 把熟饭倒进盘 B，不让针对盘 A 的 task 报成功。"""
    w = FakeWorld(stations=[COOKER, COUNTER], recipes=RECIPES,
                  orders=[ORDER],
                  cookers=[cooker("workstation_cooker_01", cooked=True)],
                  plates=[plate("pA", 7, 0, ["Seaweed"]),
                          plate("pB", 7, 2,
                                ["CookedCompositeAssembledNode"])])
    ctx, _ = make_ctx(w)
    assert T.TakeCookedToPlate("1", ("workstation_cooker_01", "pA"),
                               "BoiledSushiRice").done(ctx) is False
    assert T.TakeCookedToPlate("1", ("workstation_cooker_01", "pB"),
                               "BoiledSushiRice").done(ctx) is True


def test_return_pot_feasible_only_when_holding_cookware():
    """spec §6.1 第 5 号：可行性是纯观测事实"手持空锅"，不依赖记忆。

    这取代了 AI_REVIEW.md §4 的 RecoverPotAction 与 dropped_cooked_pot
    全局 bool。
    """
    w = FakeWorld(stations=[COOKER], recipes=RECIPES, orders=[ORDER],
                  cookers=[cooker("workstation_cooker_01", cooked=None)])
    chef = {"name": "Player 2", "x": 0.0, "z": 0.0,
            "held": "utensil_pot_01", "interactions": inter()}
    ctx, _ = make_ctx(w, chef=chef)
    assert T.ReturnPot(resources=("workstation_cooker_01",)) \
        .feasible(ctx) is True

    ctx2, _ = make_ctx(w)       # 手空
    assert T.ReturnPot(resources=("workstation_cooker_01",)) \
        .feasible(ctx2) is False


def test_return_pot_infeasible_when_cooker_occupied():
    """灶上已有锅 → 放不下，该找别的灶。"""
    w = FakeWorld(stations=[COOKER], recipes=RECIPES, orders=[ORDER],
                  cookers=[cooker("workstation_cooker_01", cooked=False)])
    chef = {"name": "Player 2", "x": 0.0, "z": 0.0,
            "held": "utensil_pot_01", "interactions": inter()}
    ctx, _ = make_ctx(w, chef=chef)
    assert T.ReturnPot(resources=("workstation_cooker_01",)) \
        .feasible(ctx) is False


def test_return_pot_done_when_hands_free_and_cooker_has_pot():
    w = FakeWorld(stations=[COOKER], recipes=RECIPES, orders=[ORDER],
                  cookers=[cooker("workstation_cooker_01", cooked=False)])
    ctx, _ = make_ctx(w)       # 手空
    assert T.ReturnPot(resources=("workstation_cooker_01",)) \
        .done(ctx) is True


def test_return_pot_uses_cookware_recipes_not_keywords():
    """spec §9：锅具识别走配方数据。

    OvenCakeTin 在关键词表里只碰巧匹配 "tin"；这里断言配方里有它时
    必然识别得出（换关卡的正确性来源）。
    """
    recipes = RECIPES + [{"name": "Cake_Plain", "comp": ["X"],
                          "cook": "OvenCakeTin"}]
    w = FakeWorld(stations=[COOKER], recipes=recipes, orders=[ORDER],
                  cookers=[cooker("workstation_cooker_01", cooked=None)])
    chef = {"name": "Player 2", "x": 0.0, "z": 0.0,
            "held": "equipment_OvenCakeTin_01", "interactions": inter()}
    ctx, _ = make_ctx(w, chef=chef)
    assert T.ReturnPot(resources=("workstation_cooker_01",)) \
        .feasible(ctx) is True


def test_rescue_cooked_feasible_when_no_plate_wants_it():
    """spec §6.1 第 6 号：熟了但没盘要它 → 端锅离灶停烧糊计时。"""
    w = FakeWorld(stations=[COOKER], recipes=RECIPES, orders=[ORDER],
                  cookers=[cooker("workstation_cooker_01", cooked=True)],
                  plates=[])
    ctx, _ = make_ctx(w)
    assert T.RescueCooked(resources=("workstation_cooker_01",)) \
        .feasible(ctx) is True


def test_rescue_cooked_infeasible_when_a_plate_wants_it():
    """有盘要这料 → 该走 TakeCookedToPlate，别浪费一趟。"""
    w = FakeWorld(stations=[COOKER, COUNTER], recipes=RECIPES,
                  orders=[ORDER],
                  cookers=[cooker("workstation_cooker_01", cooked=True)],
                  plates=[plate("p1", 7, 0, ["Seaweed"])])
    ctx, _ = make_ctx(w)
    assert T.RescueCooked(resources=("workstation_cooker_01",)) \
        .feasible(ctx) is False


def test_rescue_cooked_done_when_pot_off_cooker():
    """完成判定是"锅已离灶"（cooked 变 None = 灶上没锅）。

    spec §6.1："已确认的游戏机制：把锅从灶台端起即停止烧糊，不必倒进
    盘子。"
    """
    w = FakeWorld(stations=[COOKER], recipes=RECIPES, orders=[ORDER],
                  cookers=[cooker("workstation_cooker_01", cooked=None)])
    ctx, _ = make_ctx(w)
    assert T.RescueCooked(resources=("workstation_cooker_01",)) \
        .done(ctx) is True


def test_cooking_tasks_are_registered():
    for name in ("FetchToCooker", "TakeCookedToPlate", "ReturnPot",
                 "RescueCooked"):
        assert name in T.TASK_TYPES
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `python3 -m pytest tests/test_tasks.py -v -k "fetch_to_cooker or take_cooked or return_pot or rescue"`
Expected: FAIL，`AttributeError: module 'ai.tasks' has no attribute 'FetchToCooker'`

- [ ] **Step 3: 实现四个 task**

创建 `ai/tasks/cook.py`：

```python
"""烹饪四 task：FetchToCooker / TakeCookedToPlate / ReturnPot / RescueCooked（spec §6.1 第 3/4/5/6 号）。

本模块只放烹饪这一组，共享的身份机制与跨组谓词在 base.py。
外部不直接 import 本模块，一律 `from ai import tasks as T`
（分组边界是实现细节，见 ai/tasks/__init__.py）。
"""
from typing import Any, Dict, List, Optional, Tuple

from ai import placement
from ai import predicates as P
from ai import primitives as PR
from ai.bt import Node, Sequence
from ai.tasks.base import (TASK_TIMEOUT_TICKS, Task, _register,
                           ingredient_available, ingredient_source,
                           plate_by_id, plate_cell, station_at_cell)

# ---------- 灶相关谓词（仅本组用） ----------

def cooker_state(ctx, cooker_name):
    # type: (Dict, Any) -> Optional[Dict]
    """按站点实例名取灶的状态条目（资源 id 规则：站点实例名 n）。"""
    return P.cooker_for(ctx["w"].cookers, str(cooker_name))


def cooker_station(ctx, cooker_name):
    # type: (Dict, Any) -> Optional[Dict]
    """按实例名取灶的站点 dict（原语要 gx/gz 定位）。"""
    for st in ctx["w"].stations:
        if st.get("t") != "cooking":
            continue
        if P.station_name_matches(str(st.get("n") or ""), str(cooker_name)):
            return st
    return None


def plates_lacking(ctx, item):
    # type: (Dict, str) -> List[Dict]
    """在场且缺 item 的非空盘（spec §6.1 第 4 号的"存在缺此料的盘"）。

    放宽到"任何盘"而非"我这单的盘"是 spec §6.1 的决定：自然包含 P1 那单
    的盘，接力协助免费获得。分单模式下的筛选由协作过滤负责（Plan 4）。

    用 book.plate_lacks 而非 predicates.plate_lacks——后者是字面比较，
    对熟料恒报缺（盘里记的是不透明 token，见 RecipeBook.plate_token）。
    字面版会让这里返回所有盘，TakeCookedToPlate 于是永远"有盘要熟料"。
    """
    book = ctx["book"]
    out = []
    for p in ctx["w"].plates:
        if book.plate_lacks(p, item):
            out.append(p)
    return out


@_register
class FetchToCooker(Task):
    """第 3 号：把生料下进该灶的空锅。

    item 是**熟料名**（如 BoiledSushiRice）——订单需求里写的是熟料，
    生料由 RecipeBook.raw_of 反查。这样 task 身份与订单需求同口径。
    """

    TYPE = "FetchToCooker"

    @property
    def cooker_id(self):
        return self._resources[0]

    def claims(self):
        return ((self.cooker_id, "fetch_to"),)

    def done(self, ctx):
        # type: (Dict) -> bool
        """该灶 isCooking（spec §6.1）。绑定具体灶求值。"""
        return P.cooker_started(cooker_state(ctx, self.cooker_id))

    def feasible(self, ctx):
        # type: (Dict) -> bool
        c = cooker_state(ctx, self.cooker_id)
        if c is None or not P.cooker_has_pot(c):
            return False       # cooked=None → 灶上没锅，没法下料
        if P.cooker_started(c):
            return False       # 已在煮/已熟
        if P.cooker_burning(c):
            return False       # 着火/烧糊的锅不放料（spec §5.3）
        raw = ctx["book"].raw_of(self.item)
        return ingredient_available(ctx, raw)

    def build(self):
        # type: () -> Node
        cooker_id, cooked = self.cooker_id, self.item

        def raw_src(c):
            return ingredient_source(c, c["book"].raw_of(cooked))

        def holding_raw(c):
            raw = c["book"].raw_of(cooked)
            return P.held_matches(c["chef"], raw)

        return PR.Timeout(Sequence([
            PR.InteractWith(raw_src, "pickup", holding_raw),
            PR.InteractWith(
                lambda c: cooker_station(c, cooker_id), "placement",
                lambda c: P.cooker_started(cooker_state(c, cooker_id))),
        ]), TASK_TIMEOUT_TICKS)


@_register
class TakeCookedToPlate(Task):
    """第 4 号：端起熟料锅倒进缺此料的盘。

    resources = (cooker_name, plate_id)。盘绑定在身份里是 spec §6.0 的
    硬要求：两单都缺同一样料时，P1 倒进盘 B 不能让针对盘 A 的 task 误报
    SUCCESS。

    倒完手里是空锅——由第 5 号 ReturnPot 接手（spec §6.1 的 task 粒度
    说明："倒完饭后'手持空锅'是状态里直接读得到的事实，所以放回灶台
    切成独立 task，中断后看一眼手里就知道该干啥"）。
    """

    TYPE = "TakeCookedToPlate"

    @property
    def cooker_id(self):
        return self._resources[0]

    @property
    def plate_id(self):
        return self._resources[1]

    def claims(self):
        return ((self.cooker_id, "take_cooked"),)

    def done(self, ctx):
        # type: (Dict) -> bool
        """绑定盘已含该熟料（spec §6.1）。

        判定走 book.plate_has 的**装盘态词汇**，不是字面比较：实测盘里
        记的是不透明 token CookedCompositeAssembledNode，而非熟料名。
        字面比较恒报"还缺"→ 本 task 的 done 永不成立 → AI 把饭倒进盘后
        以为没倒，无限重倒（见 RecipeBook.plate_token）。
        """
        p = plate_by_id(ctx, self.plate_id)
        if p is None:
            return False
        return ctx["book"].plate_has(p, self.item)

    def feasible(self, ctx):
        # type: (Dict) -> bool
        c = cooker_state(ctx, self.cooker_id)
        if not P.cooker_cooked(c):
            return False
        if P.cooker_burning(c):
            return False       # 烧糊的料倒进盘会毁掉整盘，该走 Discard
        p = plate_by_id(ctx, self.plate_id)
        if p is None:
            return False
        return ctx["book"].plate_lacks(p, self.item)

    def build(self):
        # type: () -> Node
        cooker_id, plate_id, cooked = self.cooker_id, self.plate_id, self.item

        def plate_target(c):
            return plate_by_id(c, plate_id)

        def poured(c):
            p = plate_by_id(c, plate_id)
            return p is not None and c["book"].plate_has(p, cooked)

        return PR.Timeout(Sequence([
            PR.InteractWith(
                lambda c: cooker_station(c, cooker_id), "pickup",
                lambda c: P.held_is_cookware(c["chef"], c["w"].recipes)),
            PR.InteractWith(plate_target, "placement", poured),
            # 倒料后等 contents 刷新再交给 ReturnPot（spec §7.2 Settle 的
            # 唯一正当用途：等状态刷新，不是等物理稳定）
            PR.Settle(POUR_SETTLE_TICKS),
        ]), TASK_TIMEOUT_TICKS)


@_register
class ReturnPot(Task):
    """第 5 号：手里的空锅放回灶台。

    可行性是**纯观测事实**"手持空锅"（spec §6.1 第一个设计点）：不依赖
    任何记忆字段。这是火情丢锅能自动回收的机制——AI_REVIEW.md §4 那套
    专门的 RecoverPotAction 恢复逻辑和 dropped_cooked_pot 全局 bool
    都不再需要。

    正确性押在 predicates.held_is_cookware 上（spec §9 要求的配方数据
    驱动，见 Task 4）。
    """

    TYPE = "ReturnPot"

    @property
    def cooker_id(self):
        return self._resources[0]

    def claims(self):
        return ((self.cooker_id, "return_pot"),)

    def done(self, ctx):
        # type: (Dict) -> bool
        """手空 且 该灶有锅（spec §6.1）。"""
        if not P.hands_free(ctx["chef"]):
            return False
        return P.cooker_has_pot(cooker_state(ctx, self.cooker_id))

    def feasible(self, ctx):
        # type: (Dict) -> bool
        if not P.held_is_cookware(ctx["chef"], ctx["w"].recipes):
            return False
        c = cooker_state(ctx, self.cooker_id)
        if c is None:
            return False
        return not P.cooker_has_pot(c)    # 灶上已有锅就放不下

    def build(self):
        # type: () -> Node
        cooker_id = self.cooker_id
        return PR.Timeout(Sequence([
            PR.InteractWith(
                lambda c: cooker_station(c, cooker_id), "placement",
                lambda c: (P.hands_free(c["chef"])
                           and P.cooker_has_pot(
                               cooker_state(c, cooker_id)))),
        ]), TASK_TIMEOUT_TICKS)


@_register
class RescueCooked(Task):
    """第 6 号：熟了但没盘要它 → 端锅离灶，停止烧糊计时。

    spec §6.1 原话："熟料在锅里同时是**资产**（现成食材，寿司关复用率
    极高）和**负债**（cooked→burning→onFire 计时在跑）。已确认的游戏
    机制：**把锅从灶台端起即停止烧糊，不必倒进盘子**。所以本 task 成本
    极低——端起锅放到就近台面，同时停计时、腾灶台、留料备用。"

    完成判定因此是"锅已离灶"，不是"料进了盘"。
    """

    TYPE = "RescueCooked"

    @property
    def cooker_id(self):
        return self._resources[0]

    def claims(self):
        return ((self.cooker_id, "rescue"),)

    def done(self, ctx):
        # type: (Dict) -> bool
        """锅已离灶（cooked 变 None = 灶上没锅）。"""
        c = cooker_state(ctx, self.cooker_id)
        if c is None:
            return True
        return not P.cooker_has_pot(c)

    def feasible(self, ctx):
        # type: (Dict) -> bool
        c = cooker_state(ctx, self.cooker_id)
        if not P.cooker_cooked(c):
            return False
        # 有盘要这料 → 该走 TakeCookedToPlate，别浪费一趟
        cooked_items = self._cooked_candidates(ctx)
        for item in cooked_items:
            if plates_lacking(ctx, item):
                return False
        return True

    def _cooked_candidates(self, ctx):
        # type: (Dict) -> List[str]
        """这锅里可能是什么熟料。

        快照不报锅内容，只报 cooked 布尔。所以按本关配方里所有需烹饪的
        熟料名去问"有没有盘缺它"——保守但正确：只要还有盘可能要这锅东西
        就不救，宁可多等一拍走 TakeCookedToPlate。
        """
        out = []
        for order in ctx["w"].orders:
            for ing in ctx["book"].requirements(order):
                if ctx["book"].needs_cooking(ing):
                    out.append(ing)
        return out

    def build(self):
        # type: () -> Node
        cooker_id = self.cooker_id
        return PR.Timeout(Sequence([
            PR.InteractWith(
                lambda c: cooker_station(c, cooker_id), "pickup",
                lambda c: P.held_is_cookware(c["chef"], c["w"].recipes)),
            # 放到就近合法台面：落点约束与 EnsureHandsFree 共用
            # （spec §6.1"落点约束（StashHeld / RescueCooked 共用）"）
            PR.EnsureHandsFree(),
        ]), TASK_TIMEOUT_TICKS)
```

模块常量区补：

```python
# 倒料后等 contents 刷新的拍数。插件 5 Hz 推帧（200 ms），20 Hz 循环下
# 至少要 4 拍才可能看到新内容；给 6 拍留余量。
POUR_SETTLE_TICKS = 6
```

- [ ] **Step 3b: 把本组挂上包门面**

在 `ai/tasks/__init__.py` 追加一行（顺序跟随 spec §6.1 的分组次序）：

```python
from ai.tasks.cook import *        # noqa: F401,F403
```

漏这一行不会报错，只是 `TASK_TYPES` 静默少这四个 task，
`tree_viz` 就看不到它们——所以下一步的 registry 断言是必要的卡口。

- [ ] **Step 4: 运行测试，确认通过**

Run: `python3 -m pytest tests/test_tasks.py -v`
Expected: PASS，56 个测试全绿。

- [ ] **Step 5: 确认旧记忆字段的替代成立**

Run: `grep -rn "pot_cooked\|dropped_cooked_pot\|RecoverPotAction" ai/`
Expected: 无输出。spec §6.1 要求用"手持空锅 / 地上有锅"的纯观测可行性替代这三者。

- [ ] **Step 6: 运行全量测试**

Run: `python3 -m pytest -v`
Expected: 全绿。

- [ ] **Step 7: 提交**

```bash
git add ai/tasks/ tests/test_tasks.py
git commit -m "feat(tasks): 烹饪四 task（FetchToCooker/TakeCookedToPlate/ReturnPot/RescueCooked)

ReturnPot 的可行性是纯观测事实"手持空锅"，不依赖任何记忆字段
（spec §6.1 第一个设计点）。这是火情丢锅能自动回收的机制——
AI_REVIEW.md §4 那套专门的 RecoverPotAction 恢复逻辑和
dropped_cooked_pot 全局 bool 都不再需要。正确性押在 Task 4 的
held_is_cookware（配方数据驱动）上。

TakeCookedToPlate 把盘绑进 task 身份：spec §6.0 的硬要求——两单都缺
同一样料时，P1 倒进盘 B 不能让针对盘 A 的 task 误报 SUCCESS。可行性
放宽为"存在缺此料的盘"而非"我这单需要"（spec §6.1 第 4 号），自然
包含 P1 那单的盘，接力协助免费获得。

RescueCooked 的完成判定是"锅已离灶"而非"料进了盘"：spec §6.1 记已确认
的游戏机制"把锅从灶台端起即停止烧糊，不必倒进盘子"，所以本 task 成本
极低——端锅放就近台面，同时停计时、腾灶台、留料备用。

"倒完饭后手持空锅"切成独立 task（ReturnPot）而非塞进
TakeCookedToPlate：spec §6.1 的 task 粒度说明"边界切在世界状态可明确
观测的地方……中断后看一眼手里就知道该干啥"。

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---
### Task 15: 切菜两 task

**Files:**
- Create: `ai/tasks/chop.py`
- Modify: `ai/tasks/__init__.py`（补一行导入）
- Modify: `tests/test_tasks.py`（追加）

**Interfaces:**
- Consumes: Task 12-14 的全部产出、`recipes.RecipeBook.chop_target` / `needs_chopping`（Task 5）、`predicates.board_item` / `station_name_matches`（Plan 1）、`primitives.HoldUntil`（Task 9）
- Produces（spec §6.1 第 7 / 8 号）：
  - `FetchToBoard(order_id, resources=(board_name,), item=raw_name)`
  - `Chop(order_id, resources=(board_name,), item=raw_name)`

| # | Task | 可行性 | 完成判定 | 认领 |
|---|---|---|---|---|
| 7 | `FetchToBoard` | 该单需切件 且 板空闲 且 生料可得 | 板上 `item == ing` | 板 |
| 8 | `Chop` | 板上有未切完的可切料 | 板上 `item == Chopped*` | 板 |

**`Chop` 的完成判定是 Review Focus 4**（spec §7.3 定稿）："**`Chop` 的完成判定不用字符串前缀匹配**。初版写 `item == Chopped*`，但多阶段切配（需切两轮）或产物命名不以 `Chopped` 开头时会误判。改为'板上产物 == **配方期望的加工态**'，由 `predicates.py` 从配方数据（`crate.chop` / `chop_stages`）取。"

Task 5 的 `RecipeBook.chop_target` 已经按这条实现，本任务是它的消费方。

**`item` 为什么是生料名**：`Chop` 的输入是生料（`Cucumber`），输出是 `chop_target("Cucumber") == "ChoppedCucumber"`。task 身份用生料名，这样 `FetchToBoard` 与 `Chop` 的 `item` 同口径——前者的 `done`（板上是生料）正是后者的 `feasible`（上下游咬合）。

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_tasks.py`：

```python
# ---------- 切菜（spec §6.1 第 7/8 号 + Review Focus 4）----------

BOARD = {"t": "chopping", "n": "board_01", "gx": 1, "gz": 3,
         "x": 1.2, "z": 3 * 1.2}
BOARD2 = {"t": "chopping", "n": "board_01 (2)", "gx": 3, "gz": 3,
          "x": 3 * 1.2, "z": 3 * 1.2}
CUKE_CRATE = {"t": "crate", "n": "crate_cuke", "ing": "Cucumber",
              "chop": "ChoppedCucumber", "chop_stages": 8,
              "gx": -1, "gz": 3, "x": -1.2, "z": 3 * 1.2}

# 盘里要的是切好的黄瓜（ChoppedCucumber），所以订单需求列 Cucumber
# 时 Planner 会先走 FetchToBoard + Chop。这里用一张显式订单避免歧义。
ORDER_CHOP = {"id": "9", "dish": "Sushi_Cucumber",
              "composition": ["Seaweed", "BoiledSushiRice", "Cucumber"]}


def board(name, item=None):
    return {"n": name, "item": item, "interacting": False, "interacters": []}


def test_fetch_to_board_claims_board():
    t = T.FetchToBoard("9", ("board_01",), "Cucumber")
    assert t.claims() == (("board_01", "fetch_to"),)


def test_fetch_to_board_feasible_when_board_free_and_raw_available():
    w = FakeWorld(stations=[BOARD, CUKE_CRATE], recipes=RECIPES,
                  orders=[ORDER_CHOP], workstations=[board("board_01")])
    ctx, _ = make_ctx(w)
    assert T.FetchToBoard("9", ("board_01",), "Cucumber") \
        .feasible(ctx) is True


def test_fetch_to_board_infeasible_when_board_occupied():
    """板上已有东西 → 放不下。"""
    w = FakeWorld(stations=[BOARD, CUKE_CRATE], recipes=RECIPES,
                  orders=[ORDER_CHOP],
                  workstations=[board("board_01", "SushiFish")])
    ctx, _ = make_ctx(w)
    assert T.FetchToBoard("9", ("board_01",), "Cucumber") \
        .feasible(ctx) is False


def test_fetch_to_board_infeasible_for_unchoppable_item():
    """海苔不可切（crate.chop 为 null）→ 没理由放板上。"""
    w = FakeWorld(stations=[BOARD, RICE_CRATE], recipes=RECIPES,
                  orders=[ORDER_CHOP], workstations=[board("board_01")])
    ctx, _ = make_ctx(w)
    assert T.FetchToBoard("9", ("board_01",), "SushiRice") \
        .feasible(ctx) is False


def test_fetch_to_board_done_when_raw_on_board():
    w = FakeWorld(stations=[BOARD, CUKE_CRATE], recipes=RECIPES,
                  orders=[ORDER_CHOP],
                  workstations=[board("board_01", "Cucumber")])
    ctx, _ = make_ctx(w)
    t = T.FetchToBoard("9", ("board_01",), "Cucumber")
    assert t.done(ctx) is True
    assert t.feasible(ctx) is False


def test_fetch_to_board_done_binds_specific_board():
    """绑定具体板：料在 2 号板不该让 1 号板的 task 报完成。"""
    w = FakeWorld(stations=[BOARD, BOARD2, CUKE_CRATE], recipes=RECIPES,
                  orders=[ORDER_CHOP],
                  workstations=[board("board_01"),
                                board("board_01 (2)", "Cucumber")])
    ctx, _ = make_ctx(w)
    assert T.FetchToBoard("9", ("board_01",), "Cucumber").done(ctx) is False
    assert T.FetchToBoard("9", ("board_01 (2)",), "Cucumber") \
        .done(ctx) is True


def test_chop_feasible_when_raw_on_board():
    w = FakeWorld(stations=[BOARD, CUKE_CRATE], recipes=RECIPES,
                  orders=[ORDER_CHOP],
                  workstations=[board("board_01", "Cucumber")])
    ctx, _ = make_ctx(w)
    assert T.Chop("9", ("board_01",), "Cucumber").feasible(ctx) is True


def test_chop_infeasible_when_board_empty():
    w = FakeWorld(stations=[BOARD, CUKE_CRATE], recipes=RECIPES,
                  orders=[ORDER_CHOP], workstations=[board("board_01")])
    ctx, _ = make_ctx(w)
    assert T.Chop("9", ("board_01",), "Cucumber").feasible(ctx) is False


def test_chop_done_uses_recipe_chop_target_not_prefix():
    """Review Focus 4：完成判定是"板上产物 == 配方期望的加工态"。

    spec §7.3 定稿原话："Chop 的完成判定不用字符串前缀匹配。初版写
    item == Chopped*，但多阶段切配或产物命名不以 Chopped 开头时会误判。"
    """
    w = FakeWorld(stations=[BOARD, CUKE_CRATE], recipes=RECIPES,
                  orders=[ORDER_CHOP],
                  workstations=[board("board_01", "ChoppedCucumber")])
    ctx, _ = make_ctx(w)
    t = T.Chop("9", ("board_01",), "Cucumber")
    assert t.done(ctx) is True
    assert t.feasible(ctx) is False


def test_chop_done_with_non_chopped_prefix_product():
    """核心反例：产物名叫 SlicedTuna，不以 Chopped 开头。

    前缀匹配会认为"还没切好"，于是无限切下去。
    """
    tuna_crate = {"t": "crate", "n": "crate_tuna", "ing": "Tuna",
                  "chop": "SlicedTuna", "chop_stages": 4,
                  "gx": -1, "gz": 3, "x": -1.2, "z": 3 * 1.2}
    w = FakeWorld(stations=[BOARD, tuna_crate], recipes=RECIPES,
                  orders=[ORDER_CHOP],
                  workstations=[board("board_01", "SlicedTuna")])
    ctx, _ = make_ctx(w)
    assert T.Chop("9", ("board_01",), "Tuna").done(ctx) is True


def test_chop_not_done_when_product_merely_starts_with_chopped():
    """反向保护：板上是**别的**切好料时不许报成功。

    绑定 Cucumber 的 Chop 看到 ChoppedSushiFish 不是自己干完了。
    """
    fish_crate = {"t": "crate", "n": "crate_fish", "ing": "SushiFish",
                  "chop": "ChoppedSushiFish", "chop_stages": 8,
                  "gx": -3, "gz": 3, "x": -3 * 1.2, "z": 3 * 1.2}
    w = FakeWorld(stations=[BOARD, CUKE_CRATE, fish_crate], recipes=RECIPES,
                  orders=[ORDER_CHOP],
                  workstations=[board("board_01", "ChoppedSushiFish")])
    ctx, _ = make_ctx(w)
    assert T.Chop("9", ("board_01",), "Cucumber").done(ctx) is False


def test_chop_infeasible_for_unchoppable_item():
    """chop_target 为 None → 这料压根不能切，别站那儿按键。"""
    w = FakeWorld(stations=[BOARD, RICE_CRATE], recipes=RECIPES,
                  orders=[ORDER_CHOP],
                  workstations=[board("board_01", "SushiRice")])
    ctx, _ = make_ctx(w)
    assert T.Chop("9", ("board_01",), "SushiRice").feasible(ctx) is False


def test_chop_tree_holds_chop_key_with_explicit_timeout():
    """切菜树 = Approach + Face + HoldUntil（spec §7.3 的样例）。

    HoldUntil 的 timeout 必须显式传（spec §7.2），这里断言它不是 None。
    """
    tree = T.Chop("9", ("board_01",), "Cucumber").build()
    assert isinstance(tree, PR.Timeout)
    seq = tree.child
    assert isinstance(seq, Sequence)
    holds = [n for n in seq.children if isinstance(n, PR.HoldUntil)]
    assert len(holds) == 1
    assert holds[0].key == "chop"
    assert isinstance(holds[0].timeout, int) and holds[0].timeout > 0


def test_chop_timeout_covers_max_stages():
    """timeout 必须够切完最多阶段的料，否则多阶段切配永远切不完。

    实测 Cucumber / SushiFish 的 chop_stages 都是 8。固定一个太小的
    timeout 会让 HoldUntil 在切完前判 FAILURE，然后 Planner 重选同一个
    task、又切一半——无限循环且永不完成。

    早停不靠 timeout 而靠 done 谓词：切好那一拍 HoldUntil 立刻 SUCCESS，
    所以上限取保守值不会浪费时间。
    """
    holds = [n for n in T.Chop("9", ("board_01",), "Cucumber")
             .build().child.children
             if isinstance(n, PR.HoldUntil)]
    assert holds[0].timeout >= T.CHOP_TICKS_PER_STAGE * 8, \
        "timeout 不够切完 8 阶段的料（实测 Cucumber/SushiFish 都是 8）"


def test_chop_early_stops_when_done_before_timeout():
    """切好那一拍立刻 SUCCESS，不等 timeout 耗尽。"""
    w = FakeWorld(stations=[BOARD, CUKE_CRATE], recipes=RECIPES,
                  orders=[ORDER_CHOP],
                  workstations=[board("board_01", "Cucumber")])
    chef = {"name": "Player 2", "x": 1.2, "z": 2 * 1.2, "held": None,
            "interactions": inter(gx=1, gz=3)}
    ctx, ctrl = make_ctx(w, chef=chef)
    tree = T.Chop("9", ("board_01",), "Cucumber").build()
    assert tree.tick(ctx) == Status.RUNNING        # 正在按住切
    assert "chop" in ctx["hands"].held_keys
    w.workstations = [board("board_01", "ChoppedCucumber")]
    assert tree.tick(ctx) == Status.SUCCESS
    assert ctx["hands"].held_keys == set(), "切完必须松键"


def test_chop_tasks_are_registered():
    for name in ("FetchToBoard", "Chop"):
        assert name in T.TASK_TYPES
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `python3 -m pytest tests/test_tasks.py -v -k "fetch_to_board or chop"`
Expected: FAIL，`AttributeError: module 'ai.tasks' has no attribute 'FetchToBoard'`

- [ ] **Step 3: 实现两个 task**

创建 `ai/tasks/chop.py`：

```python
"""切菜两 task：FetchToBoard / Chop（spec §6.1 第 7/8 号）。

本模块只放切菜这一组，共享的身份机制与跨组谓词在 base.py。
外部不直接 import 本模块，一律 `from ai import tasks as T`
（分组边界是实现细节，见 ai/tasks/__init__.py）。
"""
from typing import Any, Dict, List, Optional, Tuple

from ai import predicates as P
from ai import primitives as PR
from ai.bt import Node, Sequence
from ai.tasks.base import (TASK_TIMEOUT_TICKS, Task, _register,
                           ingredient_available, ingredient_source)

# ---------- 板相关谓词（仅本组用） ----------

def board_station(ctx, board_name):
    # type: (Dict, Any) -> Optional[Dict]
    """按实例名取切菜板的站点 dict。"""
    for st in ctx["w"].stations:
        if st.get("t") != "chopping":
            continue
        if P.station_name_matches(str(st.get("n") or ""), str(board_name)):
            return st
    return None


@_register
class FetchToBoard(Task):
    """第 7 号：把生料放到切菜板上。

    item 是**生料名**（Cucumber），与 Chop 同口径——本 task 的 done
    （板上是生料）正是 Chop 的 feasible（spec §6.1 上下游咬合）。
    """

    TYPE = "FetchToBoard"

    @property
    def board_id(self):
        return self._resources[0]

    def claims(self):
        return ((self.board_id, "fetch_to"),)

    def done(self, ctx):
        # type: (Dict) -> bool
        """板上 item == 该生料（spec §6.1）。绑定具体板求值。"""
        return P.board_item(ctx["w"].workstations,
                            str(self.board_id)) == str(self.item).lower()

    def feasible(self, ctx):
        # type: (Dict) -> bool
        if not ctx["book"].needs_chopping(self.item):
            return False       # 不可切的料没理由放板上
        if self.done(ctx):
            return False
        if P.board_item(ctx["w"].workstations, str(self.board_id)):
            return False       # 板上已有东西，放不下
        return ingredient_available(ctx, self.item)

    def build(self):
        # type: () -> Node
        board_id, raw = self.board_id, self.item
        return PR.Timeout(Sequence([
            PR.InteractWith(
                lambda c: ingredient_source(c, raw), "pickup",
                lambda c: P.held_matches(c["chef"], raw)),
            PR.InteractWith(
                lambda c: board_station(c, board_id), "placement",
                lambda c: P.board_item(
                    c["w"].workstations, str(board_id)) == str(raw).lower()),
        ]), TASK_TIMEOUT_TICKS)


@_register
class Chop(Task):
    """第 8 号：在板上把生料切好。

    完成判定是"板上产物 == **配方期望的加工态**"（spec §7.3 定稿），
    由 RecipeBook.chop_target 从箱子的 chop 字段取。spec §7.3 原话：
    "初版写 item == Chopped*，但多阶段切配（需切两轮）或产物命名不以
    Chopped 开头时会误判。"

    另一半同样重要：绑定 Cucumber 的 Chop 看到板上是 ChoppedSushiFish
    不算自己干完了——这就是 §6.0"谓词按绑定参数求值"。
    """

    TYPE = "Chop"

    @property
    def board_id(self):
        return self._resources[0]

    def claims(self):
        return ((self.board_id, "chop"),)

    def done(self, ctx):
        # type: (Dict) -> bool
        target = ctx["book"].chop_target(self.item)
        if not target:
            return False
        on_board = P.board_item(ctx["w"].workstations, str(self.board_id))
        return on_board == str(target).lower()

    def feasible(self, ctx):
        # type: (Dict) -> bool
        if not ctx["book"].needs_chopping(self.item):
            return False
        if self.done(ctx):
            return False
        on_board = P.board_item(ctx["w"].workstations, str(self.board_id))
        return on_board == str(self.item).lower()

    def build(self):
        # type: () -> Node
        board_id, raw = self.board_id, self.item
        done = self.done

        def board_target(c):
            return board_station(c, board_id)

        # HoldUntil 的 timeout 必须在构造时确定（spec §7.2 不许有默认值），
        # 而 chop_stages 要查 ctx。折中：构造时用最大阶段数的保守上限，
        # 真正的早停由 done 谓词负责——切好那一拍 HoldUntil 立刻 SUCCESS。
        timeout = CHOP_TICKS_PER_STAGE * CHOP_MAX_STAGES + CHOP_TIMEOUT_SLACK
        return PR.Timeout(Sequence([
            PR.Approach(board_target, None),
            PR.Face(board_target),
            PR.HoldUntil("chop", done, timeout=timeout),
        ]), TASK_TIMEOUT_TICKS)
```

模块常量区补：

```python
# 切菜：每个 chop_stage 的按住拍数 + 余量。实测 Cucumber / SushiFish 的
# chop_stages 都是 8。timeout 太小会让 HoldUntil 在切完前判 FAILURE，
# Planner 重选同一 task 又切一半——无限循环。所以取能覆盖最多阶段的
# 保守上限；这不浪费时间，因为早停靠 done 谓词而非 timeout。
CHOP_TICKS_PER_STAGE = 8
CHOP_TIMEOUT_SLACK = 20
CHOP_MAX_STAGES = 12
```

- [ ] **Step 3b: 把本组挂上包门面**

在 `ai/tasks/__init__.py` 追加一行（顺序跟随 spec §6.1 的分组次序）：

```python
from ai.tasks.chop import *        # noqa: F401,F403
```

漏这一行不会报错，只是 `TASK_TYPES` 静默少这两个 task，
`tree_viz` 就看不到它们——所以下一步的 registry 断言是必要的卡口。

- [ ] **Step 4: 运行测试，确认通过**

Run: `python3 -m pytest tests/test_tasks.py -v`
Expected: PASS，72 个测试全绿（Task 14 的 56 个 + 本任务 16 个）。

- [ ] **Step 5: 确认没有前缀匹配残留**

Run: `grep -rn 'startswith("Chopped")\|startswith(.chopped.)\|"chopped" in' ai/tasks/`
Expected: 无输出。完成判定一律走 `book.chop_target`。

（`ai/predicates.py` 的 `board_chopped` 仍有 `"chopped" in` 的写法——它是 Plan 1 移植的旧谓词，本计划的 task 不调它。grep 只扫 `ai/tasks/`，命中即违规。）

- [ ] **Step 6: 运行全量测试**

Run: `python3 -m pytest -v`
Expected: 全绿。

- [ ] **Step 7: 提交**

```bash
git add ai/tasks/ tests/test_tasks.py
git commit -m "feat(tasks): 切菜两 task，完成判定走配方数据不做前缀匹配

Chop 的完成判定是"板上产物 == 配方期望的加工态"（spec §7.3 定稿），
由 RecipeBook.chop_target 从箱子的 chop 字段取。spec 原话："初版写
item == Chopped*，但多阶段切配（需切两轮）或产物命名不以 Chopped 开头
时会误判。"test_chop_done_with_non_chopped_prefix_product 用
SlicedTuna 钉住这个反例——前缀匹配会认为"还没切好"于是无限切下去。

反向保护同样重要：绑定 Cucumber 的 Chop 看到板上是 ChoppedSushiFish
不算自己干完了（spec §6.0 谓词按绑定参数求值）。

HoldUntil 的 timeout 取能覆盖最多阶段的保守上限：实测 Cucumber/SushiFish
都是 8 阶段，timeout 太小会让 HoldUntil 在切完前判 FAILURE，Planner 重选
同一 task 又切一半——无限循环且永不完成。上限保守不浪费时间，因为早停靠
done 谓词：切好那一拍 HoldUntil 立刻 SUCCESS 并松键。

item 用生料名使 FetchToBoard 与 Chop 同口径：前者的 done（板上是生料）
正是后者的 feasible（spec §6.1 上下游咬合，同一谓词写一份两处用）。

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---
### Task 16: 装盘与出餐两 task

**Files:**
- Create: `ai/tasks/assembly.py`
- Modify: `ai/tasks/__init__.py`（补一行导入）
- Modify: `tests/test_tasks.py`（追加）

**Interfaces:**
- Consumes: Task 12-15 的全部产出、`RecipeBook.plate_has` / `plate_lacks` / `missing` / `requirements`（Task 5）、`base.plate_by_id` / `plate_cell` / `ingredient_available` / `ingredient_source`、`predicates.held_is_plate` / `hands_free`、`stations.find_station`
- Produces（spec §6.1 第 9 / 10 号）：
  - `FetchToPlate(order_id, resources=(plate_id,), item=ingredient)`
  - `DeliverPlate(order_id, resources=(plate_id,))`

| # | Task | 可行性 | 完成判定 | 认领 |
|---|---|---|---|---|
| 9 | `FetchToPlate` | 盘已就位 且 盘缺此料 且 该料可得 | 盘 `contents` 含该料 | — |
| 10 | `DeliverPlate` | 盘内必需料齐 且 对应订单仍在 | 手空 **且**（订单消失 **或** 分数增加） | 盘 |

**`FetchToPlate` 的完成判定是 Review Focus 3 的原始出处**（spec §6.0 原话）："两单都缺黄瓜时，P1 把黄瓜放进盘 B，若 `FetchToPlate.done` 不绑定盘，针对盘 A 的 task 会误报 SUCCESS，盘 A 其实还空着。"所以盘进 `resources`，谓词按它求值。

**`FetchToPlate` 与 `Chop` 的衔接**：需切的料走 `FetchToBoard → Chop → FetchToPlate`，`item` 三处都是**生料名**（`Cucumber`）。`FetchToPlate` 从板上取的是 `ChoppedCucumber`，但倒进盘后 `contents` 里回落成 `Cucumber`（Task 5 实测确认）——所以取料目标要查 `chop_target`，完成判定要查 `plate_token`，两者是同一个 `item` 的两种形态。这是装盘态词汇那一层非写不可的第二个理由。

**第 9 号为什么不认领**（spec §6.1 表格"认领"列是空的）：料在箱子里是无限供应，盘已由 `PlaceCleanPlate` 认领过。重复认领盘会把自己的下游 task 挡在外面。

**第 10 号完成判定用双信号**（spec §6.1）："订单消失 **或** 分数增加，因为人类可能同时送餐，单看订单消失会误判。"实测帧确认两信号同时发生且 `score` 字段可读：帧 194 `gone={'1'}`、`score.deliveries 0→1`、`base 0→60`。但也实测到 **`combo`/`multiplier` 会在没送餐时归零**（帧 686：`gone=set()`、`(2,120,11,2,2)→(2,120,11,0,0)`）——所以"分数增加"只能看 `deliveries` 与 `base`，不能看整个 score dict 变化，否则连击断掉会被误判成送餐成功。

**`DeliverPlate` 需要跨拍基线**：两个信号都是"相对上一拍的变化"，而 `done(ctx)` 只看当前快照。基线存哪里是本任务的设计点——存黑板 scratch（`bb.deliver_baseline`）会让 `done` 有副作用且 Planner 每拍重算时失真。本计划的解法：`done` 读 `ctx["baseline"]`，由 Executor 在树构建时抓一次快照传入。这是 spec §7.6"Executor 持有树"的自然延伸，Plan 3 接线。

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_tasks.py`：

```python
# ---------- 装盘与出餐（spec §6.1 第 9/10 号）----------

PLATE_STATION = {"t": "plate_station", "n": "workstation_plate_station",
                 "gx": 9, "gz": 0, "x": 9 * 1.2, "z": 0.0}
SEAWEED_CRATE = {"t": "crate", "n": "crate_seaweed", "ing": "Seaweed",
                 "chop": None, "chop_stages": None,
                 "gx": -7, "gz": 0, "x": -7 * 1.2, "z": 0.0}

# 实测词汇：做完的盘长这样（801 帧验证，见 Task 5）
FULL = ["CookedCompositeAssembledNode", "Cucumber", "Seaweed"]


def test_fetch_to_plate_claims_nothing():
    """spec §6.1 第 9 号的认领列是空的。

    料在箱子里无限供应，盘已由 PlaceCleanPlate 认领过——重复认领盘会把
    自己的下游 task（DeliverPlate）挡在外面。
    """
    t = T.FetchToPlate("1", ("p1",), "Seaweed")
    assert t.claims() == ()


def test_fetch_to_plate_feasible_when_plate_placed_and_lacks():
    w = FakeWorld(stations=[COUNTER, SEAWEED_CRATE], recipes=RECIPES,
                  orders=[ORDER], plates=[plate("p1", 7, 0)])
    ctx, _ = make_ctx(w)
    assert T.FetchToPlate("1", ("p1",), "Seaweed").feasible(ctx) is True


def test_fetch_to_plate_infeasible_when_plate_gone():
    """盘被端走 → 不可行，不能崩（Review Focus 5）。"""
    w = FakeWorld(stations=[COUNTER, SEAWEED_CRATE], recipes=RECIPES,
                  orders=[ORDER], plates=[])
    ctx, _ = make_ctx(w)
    assert T.FetchToPlate("1", ("p1",), "Seaweed").feasible(ctx) is False


def test_fetch_to_plate_infeasible_when_ingredient_unavailable():
    """没有箱子也没散落 → 取不到。"""
    w = FakeWorld(stations=[COUNTER], recipes=RECIPES, orders=[ORDER],
                  plates=[plate("p1", 7, 0)])
    ctx, _ = make_ctx(w)
    assert T.FetchToPlate("1", ("p1",), "Seaweed").feasible(ctx) is False


def test_fetch_to_plate_done_and_infeasible_when_present():
    w = FakeWorld(stations=[COUNTER, SEAWEED_CRATE], recipes=RECIPES,
                  orders=[ORDER], plates=[plate("p1", 7, 0, ["Seaweed"])])
    ctx, _ = make_ctx(w)
    t = T.FetchToPlate("1", ("p1",), "Seaweed")
    assert t.done(ctx) is True
    assert t.feasible(ctx) is False


def test_fetch_to_plate_done_binds_specific_plate():
    """Review Focus 3 的原始出处（spec §6.0 原话）。

    两单都缺黄瓜，P1 把黄瓜放进盘 B。若 done 不绑定盘，针对盘 A 的 task
    会误报 SUCCESS——AI 以为干完了，盘 A 其实还空着。
    """
    w = FakeWorld(stations=[COUNTER, CUKE_CRATE], recipes=RECIPES,
                  orders=[ORDER], plates=[plate("pA", 7, 0),
                                          plate("pB", 7, 2, ["Cucumber"])])
    ctx, _ = make_ctx(w)
    assert T.FetchToPlate("1", ("pA",), "Cucumber").done(ctx) is False
    assert T.FetchToPlate("2", ("pB",), "Cucumber").done(ctx) is True


def test_fetch_to_plate_done_for_cooked_item_uses_token():
    """熟料的"已在盘里"靠不透明 token 判（见 Task 5）。

    盘里从不出现 BoiledSushiRice 的字面名。字面比较会让 FetchToPlate
    一直认为盘缺熟料，跟 TakeCookedToPlate 抢同一件事。
    """
    w = FakeWorld(stations=[COUNTER], recipes=RECIPES, orders=[ORDER],
                  plates=[plate("p1", 7, 0,
                                ["CookedCompositeAssembledNode"])])
    ctx, _ = make_ctx(w)
    assert T.FetchToPlate("1", ("p1",), "BoiledSushiRice").done(ctx) is True


def test_fetch_to_plate_takes_chopped_form_from_board():
    """切好的料：取 ChoppedCucumber，但完成判定看 Cucumber。

    item 三处（FetchToBoard / Chop / FetchToPlate）都是生料名，取料目标
    查 chop_target、完成判定查 plate_token——同一个 item 的两种形态。
    """
    w = FakeWorld(stations=[COUNTER, BOARD, CUKE_CRATE], recipes=RECIPES,
                  orders=[ORDER], plates=[plate("p1", 7, 0)],
                  workstations=[board("board_01", "ChoppedCucumber")])
    ctx, _ = make_ctx(w)
    t = T.FetchToPlate("1", ("p1",), "Cucumber")
    assert t.feasible(ctx) is True
    tree = t.build()
    assert isinstance(tree, PR.Timeout)


def test_fetch_to_plate_prefers_chopped_when_item_needs_chopping():
    """需切的料没切好时不该从箱子直接拿生料放盘。

    生黄瓜进盘做不成寿司。该等 Chop 干完——所以这里判不可行，把机会
    让给 FetchToBoard / Chop。
    """
    w = FakeWorld(stations=[COUNTER, BOARD, CUKE_CRATE], recipes=RECIPES,
                  orders=[ORDER], plates=[plate("p1", 7, 0)],
                  workstations=[board("board_01")])
    ctx, _ = make_ctx(w)
    assert T.FetchToPlate("1", ("p1",), "Cucumber").feasible(ctx) is False


def test_deliver_plate_claims_plate():
    t = T.DeliverPlate("1", ("p1",))
    assert t.claims() == (("p1", "deliver"),)


def test_deliver_plate_feasible_when_complete_and_order_alive():
    w = FakeWorld(stations=[PLATE_STATION], recipes=RECIPES, orders=[ORDER],
                  plates=[plate("p1", 7, 0, FULL)])
    ctx, _ = make_ctx(w)
    assert T.DeliverPlate("1", ("p1",)).feasible(ctx) is True


def test_deliver_plate_infeasible_when_plate_incomplete():
    w = FakeWorld(stations=[PLATE_STATION], recipes=RECIPES, orders=[ORDER],
                  plates=[plate("p1", 7, 0, ["Seaweed"])])
    ctx, _ = make_ctx(w)
    assert T.DeliverPlate("1", ("p1",)).feasible(ctx) is False


def test_deliver_plate_infeasible_when_order_gone():
    """订单过期 → 别送了，该走 ResetPlate（spec §6.1 第 14 号场景②）。"""
    w = FakeWorld(stations=[PLATE_STATION], recipes=RECIPES, orders=[],
                  plates=[plate("p1", 7, 0, FULL)])
    ctx, _ = make_ctx(w)
    assert T.DeliverPlate("1", ("p1",)).feasible(ctx) is False


def test_deliver_plate_done_when_order_disappeared():
    """信号一：订单消失。基线由 Executor 在建树时抓。"""
    w = FakeWorld(stations=[PLATE_STATION], recipes=RECIPES, orders=[],
                  plates=[])
    ctx, _ = make_ctx(w)
    ctx["baseline"] = T.delivery_baseline_from(
        FakeWorld(orders=[ORDER], recipes=RECIPES))
    assert T.DeliverPlate("1", ("p1",)).done(ctx) is True


def test_deliver_plate_done_when_score_increased():
    """信号二：分数增加——人类同时送餐时订单消失不可靠（spec §6.1）。

    这里订单仍在（另一张同菜单），但 deliveries 涨了，说明送出去了。
    """
    w = FakeWorld(stations=[PLATE_STATION], recipes=RECIPES, orders=[ORDER],
                  plates=[], score={"deliveries": 1, "base": 60})
    ctx, _ = make_ctx(w)
    ctx["baseline"] = T.delivery_baseline_from(
        FakeWorld(orders=[ORDER], recipes=RECIPES,
                  score={"deliveries": 0, "base": 0}))
    assert T.DeliverPlate("1", ("p1",)).done(ctx) is True


def test_deliver_plate_not_done_when_combo_resets():
    """实测反例：连击断掉时 combo/multiplier 归零，没人送餐。

    帧 686 实测 gone=set()、(2,120,11,2,2)→(2,120,11,0,0)。若"分数增加"
    看整个 score dict 是否变化，连击断掉会被误判成送餐成功——AI 以为送
    出去了，那盘菜一直留在手上。只看 deliveries 与 base。
    """
    w = FakeWorld(stations=[PLATE_STATION], recipes=RECIPES, orders=[ORDER],
                  plates=[plate("p1", 7, 0, FULL)],
                  score={"deliveries": 2, "base": 120, "tips": 11,
                         "combo": 0, "multiplier": 0})
    ctx, _ = make_ctx(w)
    ctx["baseline"] = T.delivery_baseline_from(
        FakeWorld(orders=[ORDER], recipes=RECIPES,
                  score={"deliveries": 2, "base": 120, "tips": 11,
                         "combo": 2, "multiplier": 2}))
    assert T.DeliverPlate("1", ("p1",)).done(ctx) is False


def test_deliver_plate_not_done_while_still_holding():
    """手里还端着盘 → 没送到（双信号都要求"手空且……"）。"""
    w = FakeWorld(stations=[PLATE_STATION], recipes=RECIPES, orders=[],
                  plates=[])
    chef = {"name": "Player 2", "x": 0.0, "z": 0.0,
            "held": "equipment_plate_01", "interactions": inter()}
    ctx, _ = make_ctx(w, chef=chef)
    ctx["baseline"] = T.delivery_baseline_from(
        FakeWorld(orders=[ORDER], recipes=RECIPES))
    assert T.DeliverPlate("1", ("p1",)).done(ctx) is False


def test_deliver_plate_done_without_baseline_is_false():
    """没基线不能瞎猜"送到了"——宁可多站一拍，不可误判。"""
    w = FakeWorld(stations=[PLATE_STATION], recipes=RECIPES, orders=[],
                  plates=[])
    ctx, _ = make_ctx(w)
    assert ctx.get("baseline") is None
    assert T.DeliverPlate("1", ("p1",)).done(ctx) is False


def test_deliver_plate_tree_picks_up_then_serves():
    tree = T.DeliverPlate("1", ("p1",)).build()
    assert isinstance(tree, PR.Timeout)
    seq = tree.child
    assert isinstance(seq, Sequence)
    assert len(seq.children) == 2
    assert all(isinstance(n, PR.InteractWith) for n in seq.children)


def test_assembly_tasks_are_registered():
    for name in ("FetchToPlate", "DeliverPlate"):
        assert name in T.TASK_TYPES
```

`FakeWorld` 要加 `score` 字段（Task 12 的脚手架没有它）：

```python
        self.score = kw.get("score") or {}
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `python3 -m pytest tests/test_tasks.py -v -k "fetch_to_plate or deliver"`
Expected: FAIL，`AttributeError: module 'ai.tasks' has no attribute 'FetchToPlate'`

- [ ] **Step 3: 实现两个 task**

创建 `ai/tasks/assembly.py`：

```python
"""装盘与出餐两 task：FetchToPlate / DeliverPlate（spec §6.1 第 9/10 号）。

本模块只放装盘出餐这一组，共享的身份机制与跨组谓词在 base.py。
外部不直接 import 本模块，一律 `from ai import tasks as T`
（分组边界是实现细节，见 ai/tasks/__init__.py）。
"""
from typing import Any, Dict, Optional, Tuple

from ai import predicates as P
from ai import primitives as PR
from ai import stations as S
from ai.bt import Node, Sequence
from ai.tasks.base import (TASK_TIMEOUT_TICKS, Task, _register,
                           ingredient_available, ingredient_source,
                           plate_by_id)


def delivery_baseline_from(world):
    # type: (Any) -> Dict
    """抓一份出餐判定的基线快照（订单 id 集合 + 分数计数）。

    为什么需要基线：spec §6.1 第 10 号的两个信号都是"相对上一拍的变化"
    （订单消失 / 分数增加），而 done(ctx) 只看当前快照。基线由 Executor
    在建树时抓一次、塞进 ctx["baseline"]——这是 spec §7.6"Executor 持有
    树"的自然延伸。存黑板会让 done 有副作用，且 Planner 每拍重算时失真。

    只记 deliveries 与 base，不记整个 score dict：实测 combo/multiplier
    会在没送餐时归零（帧 686：gone=set()、(2,120,11,2,2)→(2,120,11,0,0)），
    拿整个 dict 比较会把连击断掉误判成送餐成功。
    """
    score = getattr(world, "score", None) or {}
    return {
        "order_ids": frozenset(
            str(o.get("id")) for o in (getattr(world, "orders", None) or ())),
        "deliveries": _int(score.get("deliveries")),
        "base": _int(score.get("base")),
    }


def _int(v):
    # type: (Any) -> int
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


@_register
class FetchToPlate(Task):
    """第 9 号：把一样料放进已就位的盘。

    item 是**订单成分名**（Seaweed / Cucumber / BoiledSushiRice），与
    订单需求同口径。取料目标与完成判定是同一个 item 的两种形态：
      - 需切的料：取 chop_target(item)（板上的 ChoppedCucumber）
      - 完成判定：plate_token(item)（盘里的 cucumber）
    见 RecipeBook.plate_token 的长注释。

    **不认领任何资源**（spec §6.1 第 9 号的认领列为空）：料在箱子里无限
    供应，盘已由 PlaceCleanPlate 认领过。重复认领盘会把自己的下游
    DeliverPlate 挡在外面。
    """

    TYPE = "FetchToPlate"

    @property
    def plate_id(self):
        return self._resources[0]

    def claims(self):
        return ()

    def done(self, ctx):
        # type: (Dict) -> bool
        """绑定盘已含该料。按装盘态词汇判（spec §6.0 的绑定参数求值）。"""
        p = plate_by_id(ctx, self.plate_id)
        if p is None:
            return False
        return ctx["book"].plate_has(p, self.item)

    def feasible(self, ctx):
        # type: (Dict) -> bool
        p = plate_by_id(ctx, self.plate_id)
        if p is None:
            return False       # 盘被端走（Review Focus 5）
        if self.done(ctx):
            return False
        return ingredient_available(ctx, self._fetch_form(ctx))

    def _fetch_form(self, ctx):
        # type: (Dict) -> Optional[str]
        """该去取哪个形态的料。

        需切的料取切好的产物：生黄瓜进盘做不成寿司，所以没切好时这里
        返回 ChoppedCucumber、ingredient_available 查不到 → 判不可行，
        把机会让给 FetchToBoard / Chop。
        """
        book = ctx["book"]
        if book.needs_chopping(self.item):
            return book.chop_target(self.item)
        return self.item

    def build(self):
        # type: () -> Node
        plate_id, item = self.plate_id, self.item

        def src(c):
            return ingredient_source(c, _fetch_form_of(c, item))

        def holding(c):
            return P.held_matches(c["chef"], _fetch_form_of(c, item))

        def in_plate(c):
            p = plate_by_id(c, plate_id)
            return p is not None and c["book"].plate_has(p, item)

        return PR.Timeout(Sequence([
            PR.InteractWith(src, "pickup", holding),
            PR.InteractWith(lambda c: plate_by_id(c, plate_id),
                            "placement", in_plate),
        ]), TASK_TIMEOUT_TICKS)


def _fetch_form_of(ctx, item):
    # type: (Dict, Optional[str]) -> Optional[str]
    """FetchToPlate._fetch_form 的自由函数版，供树内闭包用。"""
    book = ctx["book"]
    if book.needs_chopping(item):
        return book.chop_target(item)
    return item


@_register
class DeliverPlate(Task):
    """第 10 号：端起料齐的盘送到出餐口。

    **完成判定用双信号**（spec §6.1）："订单消失 **或** 分数增加，因为
    人类可能同时送餐，单看订单消失会误判。"两者都要求手先空下来——盘
    还在手上时什么都没发生。

    基线见 delivery_baseline_from：只比 deliveries 与 base，不比整个
    score dict，因为 combo/multiplier 会在没送餐时归零。
    """

    TYPE = "DeliverPlate"

    @property
    def plate_id(self):
        return self._resources[0]

    def claims(self):
        return ((self.plate_id, "deliver"),)

    def _order(self, ctx):
        # type: (Dict) -> Optional[Dict]
        for o in ctx["w"].orders:
            if str(o.get("id")) == str(self.order_id):
                return o
        return None

    def done(self, ctx):
        # type: (Dict) -> bool
        base = ctx.get("baseline")
        if not base:
            return False       # 没基线不瞎猜：宁可多站一拍，不可误判
        if not P.hands_free(ctx["chef"]):
            return False       # 盘还在手上，什么都没发生
        now_ids = frozenset(
            str(o.get("id")) for o in ctx["w"].orders)
        if str(self.order_id) in base["order_ids"] \
                and str(self.order_id) not in now_ids:
            return True        # 信号一：我这单消失了
        score = getattr(ctx["w"], "score", None) or {}
        return (_int(score.get("deliveries")) > base["deliveries"]
                or _int(score.get("base")) > base["base"])

    def feasible(self, ctx):
        # type: (Dict) -> bool
        p = plate_by_id(ctx, self.plate_id)
        if p is None:
            return False
        order = self._order(ctx)
        if order is None:
            return False       # 订单没了 → 该走 ResetPlate
        return ctx["book"].missing(order, p) == ()

    def build(self):
        # type: () -> Node
        plate_id = self.plate_id

        def serving(c):
            return S.find_station(c["w"].stations, "plate_station",
                                  (c["chef"]["x"], c["chef"]["z"]))

        return PR.Timeout(Sequence([
            PR.InteractWith(lambda c: plate_by_id(c, plate_id), "pickup",
                            lambda c: P.held_is_plate(c["chef"])),
            PR.InteractWith(serving, "placement",
                            lambda c: P.hands_free(c["chef"])),
        ]), TASK_TIMEOUT_TICKS)
```

- [ ] **Step 3b: 把本组挂上包门面**

在 `ai/tasks/__init__.py` 追加一行（顺序跟随 spec §6.1 的分组次序）：

```python
from ai.tasks.assembly import *        # noqa: F401,F403
```

同时把 `delivery_baseline_from` 加到 `__init__.py` 的显式 re-export——Plan 3 的 Executor 要用它。

- [ ] **Step 4: 运行测试，确认通过**

Run: `python3 -m pytest tests/test_tasks.py -v`
Expected: PASS，92 个测试全绿（Task 15 的 72 个 + 本任务 20 个）。

- [ ] **Step 5: 确认分数判定没退回整 dict 比较**

Run: `grep -n 'score ==\|score !=\|!= base\["score"\]' ai/tasks/assembly.py`
Expected: 无输出。只比 `deliveries` 与 `base` 两个计数，见 `test_deliver_plate_not_done_when_combo_resets`。

- [ ] **Step 6: 运行全量测试**

Run: `python3 -m pytest -v`
Expected: 全绿。

- [ ] **Step 7: 提交**

```bash
git add ai/tasks/ tests/test_tasks.py
git commit -m "feat(tasks): 装盘与出餐两 task，出餐用双信号判定

FetchToPlate 的完成判定绑定具体盘——这是 spec §6.0 给的原始 bug：
两单都缺黄瓜时，P1 把黄瓜放进盘 B，若不绑定盘则针对盘 A 的 task 会
误报 SUCCESS，盘 A 其实还空着。

取料目标与完成判定是同一个 item 的两种形态：需切的料取 chop_target
（板上的 ChoppedCucumber），完成判定查 plate_token（盘里的 cucumber）。
没切好时判不可行，把机会让给 FetchToBoard / Chop——生黄瓜进盘做不成
寿司。

DeliverPlate 用双信号（spec §6.1 第 10 号）：订单消失或分数增加，因为
人类可能同时送餐，单看订单消失会误判。但"分数增加"只看 deliveries 与
base：实测帧 686 记录到 combo/multiplier 在没送餐时归零
（gone=set()、(2,120,11,2,2)→(2,120,11,0,0)），拿整个 score dict 比较
会把连击断掉误判成送餐成功，那盘菜就一直留在手上。

两个信号都是相对上一拍的变化，而 done 只看当前快照，所以基线由
Executor 建树时抓一次塞进 ctx（delivery_baseline_from）。存黑板会让
done 有副作用，且 Planner 每拍重算时失真。没基线时 done 返回 False——
宁可多站一拍，不可误判。

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---
### Task 17: 异常清理与预取四 task

**Files:**
- Create: `ai/tasks/cleanup.py`
- Modify: `ai/tasks/__init__.py`（补最后一行导入）
- Modify: `tests/test_tasks.py`（追加）

**Interfaces:**
- Consumes: Task 12-16 的全部产出、`predicates.held_is_cookware` / `cooker_burning` / `cooker_has_pot`、`placement.find_drop_station` / `find_drop_cell`、`stations.find_station`、`primitives.EnsureHandsFree` / `InteractWith`
- Produces（spec §6.1 第 11 / 12 / 13 / 15 号）：
  - `Discard(resources=(cooker_name,))`
  - `RecoverLoosePot(resources=(cooker_name,), item=pot_name)`
  - `StashHeld()`
  - `Prefetch(item=ingredient)`

| # | Task | 可行性 | 完成判定 | 认领 |
|---|---|---|---|---|
| 11 | `Discard` | 该灶 `burning` / 已糊 | 锅空 且 在灶上 | 灶 + **垃圾桶** |
| 12 | `RecoverLoosePot` | **地上有散落锅** | 该锅在某灶上 | 灶 |
| 13 | `StashHeld` | 手持与任何订单无关的东西 | 手空 | 落点格 |
| 15 | `Prefetch` | 存在后续订单必然需要、且可提前取的料 | 料已在台面 | 料 |

**第 11 号必须去垃圾桶**（spec §6.1）："烧焦的锅内容**只能倒进垃圾桶**才能清空，不是随便放下。现有 `_discard_tick`（`agent.py:852`）有完整流程（找 bin → 走过去 → 倒掉 → 取回锅），`task.find_station(w,"bin",...)` 是真实站点。故 `Discard` 的认领含垃圾桶。"实测帧确有 `{'t':'bin','n':'Bin 1','gx':-7,'gz':2}`。

**第 12 号的可行性是纯观测事实**（spec §6.1 第一个设计点，与第 5 号同源）："'地上有锅'，不依赖任何记忆。这是火情丢锅能自动回收的机制。"

**第 13 号的落点必须满足约束**（spec §6.1）："'随便放下'是不够的"——复用 Task 6 的 `placement`，与 `RescueCooked` 共用。

**第 15 号 `Prefetch` 的世界谓词**（spec §6.1 最后一条）：初版把 `Idle` 的可行性写成"无其他可行 task"被否决了，但 `Prefetch` "保留在候选集中并给了真正的世界谓词"。本计划的谓词：**该料被 ≥2 张现存订单需要、且手空、且台面上还没有它**。这是"必然需要"的可观测近似——两单都要的料提前取一份不会浪费。

**`Idle` 不在本任务**（spec §6.1 明确）：它是 Planner 流水线第 ⑤ 步之后的 fallback，不参与打分，归 Plan 3。

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_tasks.py`：

```python
# ---------- 异常清理与预取（spec §6.1 第 11/12/13/15 号）----------

BIN = {"t": "bin", "n": "Bin 1", "gx": -7, "gz": 2,
       "x": -7 * 1.2, "z": 2 * 1.2}
ORDER_FISH2 = {"id": "2", "dish": "Sushi_Cucumber",
               "composition": ["Seaweed", "BoiledSushiRice", "Cucumber"]}


def test_discard_claims_cooker_and_bin():
    """spec §6.1 第 11 号：认领含垃圾桶——烧焦内容只能倒进垃圾桶。"""
    t = T.Discard(resources=("workstation_cooker_01",))
    assert set(t.claims()) == {("workstation_cooker_01", "discard"),
                               ("bin", "discard")}


def test_discard_feasible_when_burning():
    w = FakeWorld(stations=[COOKER, BIN], recipes=RECIPES, orders=[ORDER],
                  cookers=[cooker("workstation_cooker_01", cooked=True,
                                  burning=True)])
    ctx, _ = make_ctx(w)
    assert T.Discard(resources=("workstation_cooker_01",)) \
        .feasible(ctx) is True


def test_discard_infeasible_when_not_burning():
    """没糊的锅不丢——那是好料。"""
    w = FakeWorld(stations=[COOKER, BIN], recipes=RECIPES, orders=[ORDER],
                  cookers=[cooker("workstation_cooker_01", cooked=True)])
    ctx, _ = make_ctx(w)
    assert T.Discard(resources=("workstation_cooker_01",)) \
        .feasible(ctx) is False


def test_discard_infeasible_without_bin():
    """没垃圾桶就倒不掉（spec §6.1："不是随便放下"）。"""
    w = FakeWorld(stations=[COOKER], recipes=RECIPES, orders=[ORDER],
                  cookers=[cooker("workstation_cooker_01", cooked=True,
                                  burning=True)])
    ctx, _ = make_ctx(w)
    assert T.Discard(resources=("workstation_cooker_01",)) \
        .feasible(ctx) is False


def test_discard_done_when_pot_empty_on_cooker():
    """完成判定：锅空 且 在灶上（cooked=False = 有锅未熟 = 空锅）。"""
    w = FakeWorld(stations=[COOKER, BIN], recipes=RECIPES, orders=[ORDER],
                  cookers=[cooker("workstation_cooker_01", cooked=False,
                                  burning=False)])
    ctx, _ = make_ctx(w)
    assert T.Discard(resources=("workstation_cooker_01",)).done(ctx) is True


def test_discard_not_done_while_still_burning():
    w = FakeWorld(stations=[COOKER, BIN], recipes=RECIPES, orders=[ORDER],
                  cookers=[cooker("workstation_cooker_01", cooked=True,
                                  burning=True)])
    ctx, _ = make_ctx(w)
    assert T.Discard(resources=("workstation_cooker_01",)).done(ctx) is False


def test_discard_tree_goes_to_bin_then_returns_pot():
    """spec §6.1：完整流程是 取锅 → 去垃圾桶倒掉 → 锅放回灶台。"""
    tree = T.Discard(resources=("workstation_cooker_01",)).build()
    assert isinstance(tree, PR.Timeout)
    assert len(tree.child.children) == 3


def test_recover_loose_pot_feasible_when_pot_on_floor():
    """spec §6.1 第 12 号：可行性是纯观测事实"地上有锅"。

    与第 5 号同源——这是火情丢锅能自动回收的机制，不依赖
    dropped_cooked_pot 之类的记忆字段。
    """
    w = FakeWorld(stations=[COOKER], recipes=RECIPES, orders=[ORDER],
                  cookers=[cooker("workstation_cooker_01", cooked=None)],
                  items=[{"n": "utensil_pot_01", "x": 2.4, "z": 0.0}])
    ctx, _ = make_ctx(w)
    t = T.RecoverLoosePot(resources=("workstation_cooker_01",),
                          item="utensil_pot_01")
    assert t.feasible(ctx) is True


def test_recover_loose_pot_infeasible_without_loose_pot():
    w = FakeWorld(stations=[COOKER], recipes=RECIPES, orders=[ORDER],
                  cookers=[cooker("workstation_cooker_01", cooked=None)],
                  items=[])
    ctx, _ = make_ctx(w)
    assert T.RecoverLoosePot(resources=("workstation_cooker_01",),
                             item="utensil_pot_01").feasible(ctx) is False


def test_recover_loose_pot_ignores_non_cookware_items():
    """地上的黄瓜不是锅——锅具识别走配方数据（spec §9）。"""
    w = FakeWorld(stations=[COOKER], recipes=RECIPES, orders=[ORDER],
                  cookers=[cooker("workstation_cooker_01", cooked=None)],
                  items=[{"n": "Cucumber", "x": 2.4, "z": 0.0}])
    ctx, _ = make_ctx(w)
    assert T.RecoverLoosePot(resources=("workstation_cooker_01",),
                             item="Cucumber").feasible(ctx) is False


def test_recover_loose_pot_infeasible_when_cooker_occupied():
    """目标灶已有锅 → 放不下。"""
    w = FakeWorld(stations=[COOKER], recipes=RECIPES, orders=[ORDER],
                  cookers=[cooker("workstation_cooker_01", cooked=False)],
                  items=[{"n": "utensil_pot_01", "x": 2.4, "z": 0.0}])
    ctx, _ = make_ctx(w)
    assert T.RecoverLoosePot(resources=("workstation_cooker_01",),
                             item="utensil_pot_01").feasible(ctx) is False


def test_recover_loose_pot_done_when_pot_on_cooker():
    w = FakeWorld(stations=[COOKER], recipes=RECIPES, orders=[ORDER],
                  cookers=[cooker("workstation_cooker_01", cooked=False)],
                  items=[])
    ctx, _ = make_ctx(w)
    assert T.RecoverLoosePot(resources=("workstation_cooker_01",),
                             item="utensil_pot_01").done(ctx) is True


def test_stash_held_feasible_when_holding_irrelevant_item():
    """spec §6.1 第 13 号：手里莫名拿着无用物（误拾、订单被抢后剩的料）。"""
    w = FakeWorld(stations=[COUNTER], recipes=RECIPES, orders=[ORDER])
    chef = {"name": "Player 2", "x": 0.0, "z": 0.0, "held": "Lettuce",
            "interactions": inter()}
    ctx, _ = make_ctx(w, chef=chef)
    assert T.StashHeld().feasible(ctx) is True


def test_stash_held_infeasible_when_holding_needed_item():
    """手里拿着某单要的料 → 别放下，去装盘。"""
    w = FakeWorld(stations=[COUNTER], recipes=RECIPES, orders=[ORDER])
    chef = {"name": "Player 2", "x": 0.0, "z": 0.0, "held": "Seaweed",
            "interactions": inter()}
    ctx, _ = make_ctx(w, chef=chef)
    assert T.StashHeld().feasible(ctx) is False


def test_stash_held_infeasible_when_holding_cookware():
    """手持锅走 ReturnPot / RecoverLoosePot，不走 StashHeld。

    锅扔台面上会让灶位空着没锅可用。
    """
    w = FakeWorld(stations=[COUNTER], recipes=RECIPES, orders=[ORDER])
    chef = {"name": "Player 2", "x": 0.0, "z": 0.0,
            "held": "utensil_pot_01", "interactions": inter()}
    ctx, _ = make_ctx(w, chef=chef)
    assert T.StashHeld().feasible(ctx) is False


def test_stash_held_infeasible_when_hands_free():
    w = FakeWorld(stations=[COUNTER], recipes=RECIPES, orders=[ORDER])
    ctx, _ = make_ctx(w)
    assert T.StashHeld().feasible(ctx) is False


def test_stash_held_done_when_hands_free():
    w = FakeWorld(stations=[COUNTER], recipes=RECIPES, orders=[ORDER])
    ctx, _ = make_ctx(w)
    assert T.StashHeld().done(ctx) is True


def test_stash_held_tree_is_ensure_hands_free():
    """落点约束与 RescueCooked 共用（spec §6.1）。"""
    tree = T.StashHeld().build()
    assert isinstance(tree, PR.Timeout)
    assert any(isinstance(n, PR.EnsureHandsFree)
               for n in tree.child.children)


def test_stash_held_has_no_order_id():
    """spec §6.0：StashHeld / RecoverLoosePot 与订单无关，order_id 为 None。

    打分时订单价值项记 0（spec §6.0 倒数第二段）。
    """
    assert T.StashHeld().order_id is None


def test_prefetch_feasible_when_two_orders_need_it():
    """spec §6.1 第 15 号的世界谓词：≥2 单需要 且 手空 且 台面没有它。

    "必然需要"的可观测近似——两单都要的料提前取一份不会浪费。
    """
    w = FakeWorld(stations=[SEAWEED_CRATE], recipes=RECIPES,
                  orders=[ORDER, ORDER_FISH2])
    ctx, _ = make_ctx(w)
    assert T.Prefetch(item="Seaweed").feasible(ctx) is True


def test_prefetch_infeasible_for_single_order_item():
    """只有一单要 → 不提前取，正常流程会取。"""
    w = FakeWorld(stations=[SEAWEED_CRATE], recipes=RECIPES, orders=[ORDER])
    ctx, _ = make_ctx(w)
    assert T.Prefetch(item="Seaweed").feasible(ctx) is False


def test_prefetch_infeasible_when_hands_busy():
    w = FakeWorld(stations=[SEAWEED_CRATE], recipes=RECIPES,
                  orders=[ORDER, ORDER_FISH2])
    chef = {"name": "Player 2", "x": 0.0, "z": 0.0, "held": "Cucumber",
            "interactions": inter()}
    ctx, _ = make_ctx(w, chef=chef)
    assert T.Prefetch(item="Seaweed").feasible(ctx) is False


def test_prefetch_infeasible_when_already_loose_on_counter():
    """台面上已有一份 → 别再取（完成判定即"料已在台面"）。"""
    w = FakeWorld(stations=[SEAWEED_CRATE], recipes=RECIPES,
                  orders=[ORDER, ORDER_FISH2],
                  items=[{"n": "Seaweed", "x": 2.4, "z": 0.0}])
    ctx, _ = make_ctx(w)
    t = T.Prefetch(item="Seaweed")
    assert t.done(ctx) is True
    assert t.feasible(ctx) is False


def test_prefetch_infeasible_for_cooked_item():
    """熟料没法"提前取"——得先下锅煮，那是 FetchToCooker 的活。"""
    w = FakeWorld(stations=[RICE_CRATE], recipes=RECIPES,
                  orders=[ORDER, ORDER_FISH2])
    ctx, _ = make_ctx(w)
    assert T.Prefetch(item="BoiledSushiRice").feasible(ctx) is False


def test_cleanup_tasks_are_registered():
    for name in ("Discard", "RecoverLoosePot", "StashHeld", "Prefetch"):
        assert name in T.TASK_TYPES


def test_all_fifteen_spec_tasks_are_registered():
    """spec §6.1 的 15 个 task 全部登记（Idle 不在候选集，归 Plan 3）。

    这是 TASK_TYPES 完整性的总卡口：漏登记不会报错，只是 tree_viz
    看不到那组 task。
    """
    expected = {
        "PlaceCleanPlate", "WashPlate",                       # 1, 2
        "FetchToCooker", "TakeCookedToPlate", "ReturnPot",    # 3, 4, 5
        "RescueCooked",                                       # 6
        "FetchToBoard", "Chop",                               # 7, 8
        "FetchToPlate",                                       # 9
        "DeliverPlate",                                       # 10
        "Discard", "RecoverLoosePot", "StashHeld",            # 11, 12, 13
        "ResetPlate",                                         # 14
        "Prefetch",                                           # 15
    }
    assert set(T.TASK_TYPES) == expected, \
        "缺 %r / 多 %r" % (sorted(expected - set(T.TASK_TYPES)),
                           sorted(set(T.TASK_TYPES) - expected))
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `python3 -m pytest tests/test_tasks.py -v -k "discard or recover_loose or stash or prefetch"`
Expected: FAIL，`AttributeError: module 'ai.tasks' has no attribute 'Discard'`

- [ ] **Step 3: 实现四个 task**

创建 `ai/tasks/cleanup.py`：

```python
"""异常清理与预取四 task：Discard / RecoverLoosePot / StashHeld / Prefetch
（spec §6.1 第 11/12/13/15 号）。

本模块只放清理与预取这一组，共享的身份机制与跨组谓词在 base.py。
外部不直接 import 本模块，一律 `from ai import tasks as T`
（分组边界是实现细节，见 ai/tasks/__init__.py）。

Idle 不在这里（spec §6.1 最后一条）：它是 Planner 流水线第 ⑤ 步之后的
fallback，不参与打分，归 Plan 3。
"""
from typing import Any, Dict, List, Optional, Tuple

from ai import predicates as P
from ai import primitives as PR
from ai import stations as S
from ai.bt import Node, Sequence
from ai.tasks.base import (TASK_TIMEOUT_TICKS, Task, _register,
                           ingredient_source)

# 垃圾桶的资源 id。站点实例名随关卡变（实测 "Bin 1"），但认领只需要一个
# 稳定的标识符——同一关卡内只有一个垃圾桶被认领就够了。
BIN_RESOURCE = "bin"


def loose_cookware(ctx):
    # type: (Dict) -> List[Dict]
    """地上/台面上散落的锅具（spec §6.1 第 12 号的纯观测可行性）。

    锅具识别走 predicates.held_is_cookware 的同一套配方数据判定
    （spec §9），不做名字关键词匹配——DeepFatFryer / OvenTray /
    OvenCakeTin / Steamer 四种名字里既无 pot 也无 pan。
    """
    out = []
    recipes = ctx["w"].recipes
    for it in ctx["w"].items:
        if P.held_is_cookware({"held": it.get("n")}, recipes):
            out.append(it)
    return out


def item_wanted_by_orders(ctx, item):
    # type: (Dict, Optional[str]) -> int
    """该料被几张现存订单需要（按订单成分名比较）。"""
    if not item:
        return 0
    want = str(item).lower()
    book = ctx["book"]
    n = 0
    for order in ctx["w"].orders:
        for ing in book.requirements(order):
            if str(ing).lower() == want:
                n += 1
                break
    return n


def loose_item_named(ctx, item):
    # type: (Dict, Optional[str]) -> Optional[Dict]
    """台面/地上是否已散落着一份该料。"""
    if not item:
        return None
    want = str(item).lower()
    for it in ctx["w"].items:
        if str(it.get("n") or "").lower() == want:
            return it
    return None


@_register
class Discard(Task):
    """第 11 号：烧糊的锅内容倒进垃圾桶，空锅放回灶台。

    spec §6.1 原话："烧焦的锅内容**只能倒进垃圾桶**才能清空，不是随便
    放下。现有 _discard_tick（agent.py:852）有完整流程（找 bin → 走过去
    → 倒掉 → 取回锅）……故 Discard 的认领含垃圾桶。"

    完成判定是"锅空 且 在灶上"——倒完还要把锅放回去，否则灶位空着没锅，
    下一单的 FetchToCooker 不可行。
    """

    TYPE = "Discard"

    @property
    def cooker_id(self):
        return self._resources[0]

    def claims(self):
        return ((self.cooker_id, "discard"), (BIN_RESOURCE, "discard"))

    def done(self, ctx):
        # type: (Dict) -> bool
        c = _cooker(ctx, self.cooker_id)
        if c is None:
            return False
        if not P.cooker_has_pot(c):
            return False       # 锅还没放回灶台
        return not P.cooker_burning(c) and not P.cooker_cooked(c)

    def feasible(self, ctx):
        # type: (Dict) -> bool
        c = _cooker(ctx, self.cooker_id)
        if c is None or not P.cooker_burning(c):
            return False
        # 没垃圾桶就倒不掉——spec §6.1："不是随便放下"
        return S.find_station(ctx["w"].stations, "bin") is not None

    def build(self):
        # type: () -> Node
        cooker_id = self.cooker_id

        def cooker_st(c):
            return _cooker_station(c, cooker_id)

        def bin_st(c):
            return S.find_station(c["w"].stations, "bin",
                                  (c["chef"]["x"], c["chef"]["z"]))

        return PR.Timeout(Sequence([
            PR.InteractWith(
                cooker_st, "pickup",
                lambda c: P.held_is_cookware(c["chef"], c["w"].recipes)),
            PR.InteractWith(
                bin_st, "placement",
                lambda c: not P.cooker_burning(_cooker(c, cooker_id))),
            PR.InteractWith(
                cooker_st, "placement",
                lambda c: (P.hands_free(c["chef"])
                           and P.cooker_has_pot(_cooker(c, cooker_id)))),
        ]), TASK_TIMEOUT_TICKS)


@_register
class RecoverLoosePot(Task):
    """第 12 号：地上的散落锅捡回灶台。

    可行性是**纯观测事实**"地上有锅"（spec §6.1 第一个设计点，与第 5 号
    同源）：不依赖任何记忆。这是火情丢锅能自动回收的机制——
    AI_REVIEW.md §4 那套 RecoverPotAction 与 dropped_cooked_pot 不再需要。

    item 是散落锅的实例名，绑定在身份里：地上有两口锅时它们是两个
    不同的 task（spec §6.0）。
    """

    TYPE = "RecoverLoosePot"

    @property
    def cooker_id(self):
        return self._resources[0]

    def claims(self):
        return ((self.cooker_id, "recover"),)

    def done(self, ctx):
        # type: (Dict) -> bool
        """该锅在某灶上。用目标灶判——绑定参数求值（spec §6.0）。"""
        return P.cooker_has_pot(_cooker(ctx, self.cooker_id))

    def feasible(self, ctx):
        # type: (Dict) -> bool
        c = _cooker(ctx, self.cooker_id)
        if c is None or P.cooker_has_pot(c):
            return False       # 目标灶已有锅，放不下
        for it in loose_cookware(ctx):
            if str(it.get("n")) == str(self.item):
                return True
        return False

    def build(self):
        # type: () -> Node
        cooker_id, pot_name = self.cooker_id, self.item

        def pot(c):
            for it in loose_cookware(c):
                if str(it.get("n")) == str(pot_name):
                    return it
            return None

        return PR.Timeout(Sequence([
            # 地面物不需要精确朝向（旧 _goto_grab_cell 的那条路）
            PR.InteractWith(
                pot, "pickup",
                lambda c: P.held_is_cookware(c["chef"], c["w"].recipes),
                face=False),
            PR.InteractWith(
                lambda c: _cooker_station(c, cooker_id), "placement",
                lambda c: (P.hands_free(c["chef"])
                           and P.cooker_has_pot(_cooker(c, cooker_id)))),
        ]), TASK_TIMEOUT_TICKS)


@_register
class StashHeld(Task):
    """第 13 号：手里莫名拿着无用物时清空手。

    spec §6.1 原话："是兜底：手里莫名拿着无用物（误拾、订单被抢后剩的
    料），清空手好继续干活。**但'随便放下'是不够的**，落点必须满足约束。"
    落点走 placement（Task 6），与 RescueCooked 共用。

    手持锅具不走这里（走 ReturnPot / RecoverLoosePot）：锅扔台面上会让
    灶位空着没锅可用。

    与订单无关，order_id 为 None——打分时订单价值项记 0（spec §6.0）。
    """

    TYPE = "StashHeld"

    def claims(self):
        return ()

    def done(self, ctx):
        # type: (Dict) -> bool
        return P.hands_free(ctx["chef"])

    def feasible(self, ctx):
        # type: (Dict) -> bool
        chef = ctx["chef"]
        if P.hands_free(chef):
            return False
        if P.held_is_cookware(chef, ctx["w"].recipes):
            return False       # 锅走 ReturnPot / RecoverLoosePot
        if P.held_is_plate(chef):
            return False       # 盘走 PlaceCleanPlate / DeliverPlate / ResetPlate
        held = P.held_name(chef)
        # 手里的东西还有单要 → 别放下，去装盘
        return item_wanted_by_orders(ctx, held) == 0

    def build(self):
        # type: () -> Node
        return PR.Timeout(Sequence([PR.EnsureHandsFree()]),
                          TASK_TIMEOUT_TICKS)


@_register
class Prefetch(Task):
    """第 15 号：把多单都要的料提前取一份放台面。

    spec §6.1 最后一条否决了 Idle 的"无其他可行 task"写法（那不是世界
    谓词），但保留 Prefetch 并要求"真正的世界谓词"。本计划的谓词：
    **该料被 ≥2 张现存订单需要 且 手空 且 台面上还没有它**。

    这是"必然需要"的可观测近似——两单都要的料提前取一份不会浪费。
    熟料排除在外：那得先下锅煮，是 FetchToCooker 的活。
    """

    TYPE = "Prefetch"

    def claims(self):
        return ()

    def done(self, ctx):
        # type: (Dict) -> bool
        """料已在台面（spec §6.1 第 15 号）。"""
        return loose_item_named(ctx, self.item) is not None

    def feasible(self, ctx):
        # type: (Dict) -> bool
        if not P.hands_free(ctx["chef"]):
            return False
        if ctx["book"].needs_cooking(self.item):
            return False       # 熟料要下锅，不是"取一份"能解决的
        if self.done(ctx):
            return False
        if item_wanted_by_orders(ctx, self.item) < PREFETCH_MIN_ORDERS:
            return False
        return ingredient_source(ctx, self.item) is not None

    def build(self):
        # type: () -> Node
        item = self.item
        return PR.Timeout(Sequence([
            PR.InteractWith(lambda c: ingredient_source(c, item), "pickup",
                            lambda c: P.held_matches(c["chef"], item)),
            PR.EnsureHandsFree(),
        ]), TASK_TIMEOUT_TICKS)


# ---------- 本组内部辅助 ----------

def _cooker(ctx, cooker_name):
    # type: (Dict, Any) -> Optional[Dict]
    return P.cooker_for(ctx["w"].cookers, str(cooker_name))


def _cooker_station(ctx, cooker_name):
    # type: (Dict, Any) -> Optional[Dict]
    for st in ctx["w"].stations:
        if st.get("t") != "cooking":
            continue
        if P.station_name_matches(str(st.get("n") or ""), str(cooker_name)):
            return st
    return None
```

模块常量区补：

```python
# Prefetch 的触发门槛：被几张单需要才值得提前取。设 2 的理由是"不会
# 浪费"——两单都要的料取一份必然用得上。设 1 会让 AI 在正常流程之外
# 抢着取料，反而打乱节奏。
PREFETCH_MIN_ORDERS = 2
```

**`_cooker` / `_cooker_station` 与 `cook.py` 的同名函数重复**：两份实现相同，但让 `cleanup.py` 去 import `cook.py` 会在"同级子模块"之间拉一条依赖边，而分组的意义就是互不依赖。若 review 认为该去重，正确的落点是上移到 `base.py`（和 `plate_by_id` 一样的处理），不是跨组 import。实施时按这个方向做，并在提交信息里记一笔。

- [ ] **Step 3b: 把本组挂上包门面**

在 `ai/tasks/__init__.py` 追加最后一行：

```python
from ai.tasks.cleanup import *        # noqa: F401,F403
```

- [ ] **Step 4: 运行测试，确认通过**

Run: `python3 -m pytest tests/test_tasks.py -v`
Expected: PASS，118 个测试全绿（Task 16 的 92 个 + 本任务 26 个）。

- [ ] **Step 5: 确认包门面五行齐全**

Run: `grep -c "^from ai.tasks\..* import \*" ai/tasks/__init__.py`
Expected: `5`（plate / cook / chop / assembly / cleanup）。

少一行 `TASK_TYPES` 就静默少一组 task，`test_all_fifteen_spec_tasks_are_registered` 是它的断言卡口。

- [ ] **Step 6: 确认 400 行上限未破**

Run: `wc -l ai/*.py ai/tasks/*.py | sort -rn | head -8`
Expected: 无单个文件 > 400 行（spec §11.5 的代码硬指标）。

- [ ] **Step 7: 运行全量测试**

Run: `python3 -m pytest -v`
Expected: 全绿。

- [ ] **Step 8: 提交**

```bash
git add ai/tasks/ tests/test_tasks.py
git commit -m "feat(tasks): 异常清理与预取四 task，补齐 spec 的 15 个 task

Discard 的认领含垃圾桶（spec §6.1 第 11 号）："烧焦的锅内容只能倒进
垃圾桶才能清空，不是随便放下。"完成判定是"锅空且在灶上"——倒完还要把
锅放回去，否则灶位空着没锅，下一单的 FetchToCooker 不可行。实测帧确有
{'t':'bin','n':'Bin 1'}。

RecoverLoosePot 的可行性是纯观测事实"地上有锅"（与 ReturnPot 同源）：
不依赖任何记忆字段。锅具识别走配方数据而非名字关键词——DeepFatFryer /
OvenTray / OvenCakeTin / Steamer 四种名字里既无 pot 也无 pan（spec §9）。

StashHeld 排除手持锅具与盘：锅扔台面上会让灶位空着没锅可用，盘有自己
的三个 task。落点走 placement，与 RescueCooked 共用约束（spec §6.1）。

Prefetch 给了真正的世界谓词（spec §6.1 最后一条要求）：被 >=2 张现存
订单需要 且 手空 且 台面还没有它。这是"必然需要"的可观测近似——两单
都要的料提前取一份不会浪费。熟料排除在外，那是 FetchToCooker 的活。

test_all_fifteen_spec_tasks_are_registered 是 TASK_TYPES 完整性的总
卡口：漏登记不会报错，只是 tree_viz 看不到那组 task。

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---
### Task 18: `tree_viz.py` 离线冒烟

**Files:**
- Create: `tools/tree_viz.py`
- Test: `tests/test_tree_viz.py`

**Interfaces:**
- Consumes: `tasks.TASK_TYPES` / `Task`（Task 12-17）、`primitives.*`（Task 7-11）、`hands.Hands`（Task 2）、`world.World`（Plan 1.5）、`replay.load_frames`（Plan 1）
- Produces:
  - `render(node)` → `List[str]`：ASCII 树
  - `label(node)` → `str`：节点标签
  - `NullController`：记录调用但不碰键盘的假 controller
  - `make_ctx(world, chef=None, tick=0, with_controller=False)`：从真实 `World` 装配 ctx。`with_controller=True` 时返回 `(ctx, NullController)`，否则只返回 `ctx`
  - `placeholder_task(cls)` → `Task`：用占位资源构造某类型的实例，供全类型覆盖用
  - `load_frame(path, index=0)` → `Dict`
  - `enumerate_tasks(ctx)` → `List[Task]`：对一帧枚举可构造的 task 实例
  - `main(argv)` → `int`

**这是 Plan 2 的交付验证**（spec §11.2 阶段 3）："`tree_viz.py` 冒烟：打印树、假状态走分支、**不注入按键**。"

**与 `oc/ai/tree_viz.py` 的关系**：旧版 114 行，`import agent as A` 然后 `A.Controller = NoopCtrl`、`A.StateClient = DummyClient` —— 靠**猴补丁**绕开真实 controller。新版不需要：`Hands` 构造时注入 controller（Task 2 的设计），`NullController` 是正常参数。旧版还依赖 `ag._build_tree(w, order)`，而新架构里树由每个 task 自己 `build()`。

**放 `tools/` 而非 `ai/`**：它是开发工具不是运行时模块，与 `tools/record_frames.py` 同列。spec §8.2 把 `tree_viz.py` 列在 `ai/` 旁边，但那张表记的是职责归属而非目录——`ai/` 下不该有 `__main__` 脚本（Global Constraints 的"不接运行时决策路径"）。这是一处偏离，记为 Ruling。

- [ ] **Step 1: 写失败测试**

创建 `tests/test_tree_viz.py`：

```python
"""tree_viz 离线冒烟的测试。

Plan 2 的交付标准是"打印树、假状态走分支、不注入按键"（spec §11.2
阶段 3），所以这里最重要的两条断言是：
  1. 整个调用链不导入 Quartz（真键盘注入库）；
  2. 走完全部 task 的树后，NullController 记下了调用但没有真实按键。
"""
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import tree_viz as V        # noqa: E402

from ai import primitives as PR   # noqa: E402
from ai import tasks as T   # noqa: E402
from ai.bt import Sequence, Status   # noqa: E402
from ai.world import World  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures",
                       "real_frame_sushi_1_4.json")


@pytest.fixture
def frame():
    import json
    if not os.path.exists(FIXTURE):
        pytest.skip("缺 tests/fixtures/real_frame_sushi_1_4.json")
    with open(FIXTURE) as f:
        return json.load(f)


# ---------- 零按键注入（Plan 2 的交付标准）----------

def test_module_does_not_import_quartz():
    """Quartz 是真键盘注入库。它出现在依赖链里就说明可能真按键。"""
    assert "Quartz" not in sys.modules, \
        "导入 tree_viz 把 Quartz 带进来了，离线冒烟不该碰真键盘"


def test_null_controller_records_without_pressing():
    c = V.NullController()
    c.press("left")
    c.tap("pickup")
    c.release("left")
    c.release_all()
    assert c.calls == [("press", "left"), ("tap", "pickup"),
                       ("release", "left"), ("release_all",)]


# ---------- 渲染 ----------

def test_label_for_primitives():
    assert "Approach" in V.label(PR.Approach({"n": "c"}, "pickup"))
    assert "Tap" in V.label(PR.Tap("pickup"))
    assert "HoldUntil" in V.label(
        PR.HoldUntil("chop", lambda c: True, timeout=5))


def test_label_shows_hold_until_key_and_timeout():
    """按住哪个键、最多多久，是看树时最想知道的两件事。"""
    s = V.label(PR.HoldUntil("chop", lambda c: True, timeout=42))
    assert "chop" in s and "42" in s


def test_label_shows_interact_with_kind():
    s = V.label(PR.InteractWith({"n": "c"}, "placement", lambda c: True))
    assert "InteractWith" in s and "placement" in s


def test_render_nests_children():
    tree = Sequence([PR.Tap("pickup"), PR.Settle(2)])
    lines = V.render(tree)
    assert len(lines) == 3
    assert "Sequence" in lines[0]
    assert lines[1].startswith(("   ", "│", "├", "└")) or \
        lines[1].lstrip() != lines[1], "子节点该有缩进"


def test_render_timeout_shows_child():
    tree = PR.Timeout(Sequence([PR.Tap("pickup")]), 200)
    lines = V.render(tree)
    assert any("Timeout" in l for l in lines)
    assert any("Tap" in l for l in lines)


# ---------- 从真实帧装配 ----------

def test_make_ctx_from_real_frame(frame):
    w = World(frame)
    ctx = V.make_ctx(w)
    for key in ("w", "chef", "hands", "bb", "field", "book", "tick"):
        assert key in ctx, "ctx 缺 %s（原语的契约，见 primitives 头注释）" % key
    assert ctx["field"], "距离场为空，ETA 查表会全失效"


def test_make_ctx_uses_p2(frame):
    """控制的是 P2。认错厨师 = 控制错对象。"""
    ctx = V.make_ctx(World(frame))
    assert ctx["chef"].get("name") == "Player 2"


def test_enumerate_tasks_covers_multiple_types(frame):
    """枚举出的 task 该覆盖多种类型，否则冒烟只测了一条路。"""
    ctx = V.make_ctx(World(frame))
    tasks = V.enumerate_tasks(ctx)
    assert len(tasks) >= 5
    types = set(t.TYPE for t in tasks)
    assert len(types) >= 3, "只枚举出 %r，覆盖面太窄" % sorted(types)


def test_enumerate_tasks_all_build_without_error(frame):
    """每个 task 都能 build 出树——这是 Plan 2"离线构树"的核心断言。"""
    ctx = V.make_ctx(World(frame))
    for t in V.enumerate_tasks(ctx):
        tree = t.build()
        assert tree is not None
        assert V.render(tree), "%r 的树渲染为空" % (t,)


def test_every_task_type_builds(frame):
    """TASK_TYPES 里的每个类型都要能构造+建树。

    enumerate_tasks 只产出当前帧可行的；这里用占位资源强制覆盖全部 15 个
    类型，免得某个 task 的 build 写错了却因为当前帧不触发而没被发现。
    """
    ctx = V.make_ctx(World(frame))
    for name, cls in sorted(T.TASK_TYPES.items()):
        t = V.placeholder_task(cls)
        tree = t.build()
        assert tree is not None, "%s.build() 返回 None" % name
        assert V.render(tree), "%s 的树渲染为空" % name


def test_predicates_evaluate_without_error(frame):
    """每个 task 的 feasible / done 在真实帧上都不许抛。

    谓词抛异常会让 Planner 整拍崩掉——比判错严重得多。
    """
    ctx = V.make_ctx(World(frame))
    for name, cls in sorted(T.TASK_TYPES.items()):
        t = V.placeholder_task(cls)
        assert t.feasible(ctx) in (True, False), "%s.feasible 没返回 bool" % name
        assert t.done(ctx) in (True, False), "%s.done 没返回 bool" % name


# ---------- 走分支不按键 ----------

def test_ticking_trees_presses_no_real_keys(frame):
    """假状态走分支：tick 每棵树若干拍，断言只碰了 NullController。

    这是 spec §11.2 阶段 3 验收标准"不注入按键"的落点。
    """
    w = World(frame)
    ctx, ctrl = V.make_ctx(w, with_controller=True)
    for t in V.enumerate_tasks(ctx):
        tree = t.build()
        for _ in range(3):
            status = tree.tick(ctx)
            assert status in (Status.SUCCESS, Status.FAILURE, Status.RUNNING)
            ctx["hands"].tick()
        tree.halt()
    assert "Quartz" not in sys.modules
    # 走完必须零残留按键（spec §11.5 的浸泡测试硬标准，这里离线先验一次）
    assert ctx["hands"].held_keys == set()
    assert ctx["hands"].move_dir is None


def test_cli_runs_on_fixture_and_exits_zero():
    """命令行能跑：这是人实际使用冒烟工具的方式。"""
    if not os.path.exists(FIXTURE):
        pytest.skip("缺夹具")
    root = os.path.join(os.path.dirname(__file__), "..")
    r = subprocess.run(
        [sys.executable, os.path.join("tools", "tree_viz.py"), FIXTURE],
        cwd=root, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "Sequence" in r.stdout or "InteractWith" in r.stdout


def test_cli_missing_file_exits_nonzero():
    root = os.path.join(os.path.dirname(__file__), "..")
    r = subprocess.run(
        [sys.executable, os.path.join("tools", "tree_viz.py"),
         "no_such_frame.json"],
        cwd=root, capture_output=True, text=True)
    assert r.returncode != 0
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `python3 -m pytest tests/test_tree_viz.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'tree_viz'`

- [ ] **Step 3: 实现 `tools/tree_viz.py`**

```python
#!/usr/bin/env python3
"""离线冒烟：从一帧快照构建全部 task 的树、打印 ASCII、走分支不按键。

用法:
  python3 tools/tree_viz.py tests/fixtures/real_frame_sushi_1_4.json
  python3 tools/tree_viz.py frames.ndjson.gz --frame 100
  python3 tools/tree_viz.py <帧文件> --tick 5      # 每棵树多走几拍

这是 Plan 2 的交付验证（spec §11.2 阶段 3）："打印树、假状态走分支、
**不注入按键**"。

与 oc/ai/tree_viz.py 的差异：旧版靠**猴补丁**绕开真实 controller
（import agent as A; A.Controller = NoopCtrl; A.StateClient = DummyClient）。
新版不需要——Hands 构造时注入 controller（见 ai/hands.py 的设计理由），
NullController 是正常参数。旧版还依赖 ag._build_tree(w, order) 这个
Agent 方法，而新架构里树由每个 task 自己 build()。

放 tools/ 而非 ai/：这是开发工具不是运行时模块，与 tools/record_frames.py
同列。spec §8.2 把 tree_viz.py 列在 ai/ 旁边，但那张表记的是职责归属
而非目录——ai/ 下不该有 __main__ 脚本。
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                ".."))

from ai import primitives as PR            # noqa: E402
from ai import tasks as T                  # noqa: E402
from ai.blackboard import Blackboard       # noqa: E402
from ai.bt import Node, Status             # noqa: E402
from ai.hands import Hands                 # noqa: E402
from ai.navigation import distance_field   # noqa: E402
from ai.predicates import P2_NAME, chef_by_name   # noqa: E402
from ai.recipes import RecipeBook          # noqa: E402
from ai.world import World                 # noqa: E402


class NullController(object):
    """记录调用但不碰键盘。

    注意它**不是** oc/ai/tree_viz.py 的 NoopCtrl：那个是为了猴补丁
    Agent.Controller 而存在的空壳，这个是 Hands 的正常注入参数，并且
    记下调用序列供断言。
    """

    def __init__(self):
        # type: () -> None
        self.calls = []

    def tap(self, name, hold=0.12):
        self.calls.append(("tap", name))

    def press(self, name):
        self.calls.append(("press", name))

    def release(self, name):
        self.calls.append(("release", name))

    def release_all(self):
        self.calls.append(("release_all",))


# ---------- 渲染 ----------

def label(node):
    # type: (Node) -> str
    """节点的一行标签。

    每个原语最想知道的参数不同：Tap 是按哪个键，HoldUntil 还要知道
    最多等多久（timeout 写错会让切菜永远切不完），InteractWith 要知道
    是 pickup 还是 placement（放错句柄会把盘丢到出餐口）。
    """
    cls = type(node).__name__
    if isinstance(node, PR.Timeout):
        return "Timeout(%d)" % node.max_ticks
    if isinstance(node, PR.HoldUntil):
        return "HoldUntil(%s, timeout=%d)" % (node.key, node.timeout)
    if isinstance(node, PR.TapWhileHolding):
        return "TapWhileHolding(tap=%s)" % node.tap_key
    if isinstance(node, PR.Tap):
        return "Tap(%s)" % node.key
    if isinstance(node, PR.InteractWith):
        return "InteractWith(%s)" % (node.kind or "-")
    if isinstance(node, PR.Approach):
        return "Approach(%s)" % (node.kind or "-")
    if isinstance(node, PR.Settle):
        return "Settle(%d)" % node.ticks
    if isinstance(node, PR.Nudge):
        return "Nudge(%s, %d)" % (node.direction, node.ticks)
    return cls


def render(node, prefix="", is_last=True, lines=None):
    # type: (Node, str, bool, list) -> list
    """ASCII 树。Timeout 与 InteractWith 的内部子节点也展开。"""
    lines = lines if lines is not None else []
    connector = "└─ " if is_last else "├─ "
    lines.append(prefix + connector + label(node))
    child_prefix = prefix + ("   " if is_last else "│  ")

    children = list(getattr(node, "children", None) or ())
    if isinstance(node, PR.Timeout):
        children = [node.child]
    elif isinstance(node, PR.InteractWith):
        children = node._children()

    for i, c in enumerate(children):
        render(c, child_prefix, i == len(children) - 1, lines)
    return lines


# ---------- 装配 ----------

def make_ctx(world, chef=None, tick=0, with_controller=False):
    # type: (World, dict, int, bool) -> object
    """从真实 World 装配原语要的 ctx（契约见 ai/primitives.py 头注释）。

    控制的是 P2——认错厨师就是控制错对象（spec §11.2.1 已知缝隙表把
    "chefs[].slot 全为 null"记为 Plan 3 接入时要验的项，这里按 name 取）。
    """
    if chef is None:
        chef = chef_by_name(world.chefs, P2_NAME)
    if chef is None:
        # 没有 P2 的帧（菜单画面等）：取任一带坐标的厨师，纯为渲染
        for c in world.chefs:
            if c.get("x") is not None:
                chef = c
                break
    if chef is None:
        chef = {"name": P2_NAME, "x": 0.0, "z": 0.0, "held": None,
                "interactions": {}}

    ctrl = NullController()
    start = world.to_grid(chef.get("x") or 0.0, chef.get("z") or 0.0)
    ctx = {
        "w": world,
        "chef": chef,
        "hands": Hands(ctrl),
        "bb": Blackboard(),
        "field": distance_field(start, world.walkable, world.belt_dir),
        "book": RecipeBook(world.recipes, world.stations),
        "tick": tick,
    }
    if with_controller:
        return ctx, ctrl
    return ctx


def _first(seq, default=None):
    for x in seq or ():
        return x
    return default


def placeholder_task(cls):
    # type: (type) -> T.Task
    """用占位资源构造某类型的一个实例，只为验证 build() 与谓词不抛。

    enumerate_tasks 只产出当前帧可行的 task；这个函数强制覆盖全部 15 个
    类型，免得某个 build 写错却因当前帧不触发而逃过冒烟。
    """
    needs = {
        "PlaceCleanPlate": ("p0", (0, 0)),
        "ResetPlate": ("p0", (0, 0)),
        "WashPlate": ("sink0",),
        "FetchToCooker": ("cooker0",),
        "TakeCookedToPlate": ("cooker0", "p0"),
        "ReturnPot": ("cooker0",),
        "RescueCooked": ("cooker0",),
        "Discard": ("cooker0",),
        "RecoverLoosePot": ("cooker0",),
        "FetchToBoard": ("board0",),
        "Chop": ("board0",),
        "FetchToPlate": ("p0",),
        "DeliverPlate": ("p0",),
        "StashHeld": (),
        "Prefetch": (),
    }
    return cls(order_id="0", resources=needs.get(cls.TYPE, ()),
               item="Cucumber")


def enumerate_tasks(ctx):
    # type: (dict) -> list
    """对当前帧枚举可构造的 task 实例（绑定真实资源）。

    这不是 Planner 的候选生成（那是 Plan 3，还要过可行性/协作/打分）。
    这里只求覆盖面：每类资源取一个真实实例，让冒烟能走到多种树。
    """
    w = ctx["w"]
    book = ctx["book"]
    out = []

    plate = _first(w.plates)
    plate_id = plate.get("n") if plate else None
    cooker = _first(w.cookers)
    cooker_id = cooker.get("n") if cooker else None
    board = _first([s for s in w.stations if s.get("t") == "chopping"])
    board_id = board.get("n") if board else None
    sink = _first([s for s in w.stations if s.get("t") == "washing"])
    sink_id = sink.get("n") if sink else None
    counter_cell = (0, 0)
    for s in w.stations:
        if s.get("t") == "counter" and s.get("gx") is not None:
            counter_cell = (s["gx"], s["gz"])
            break

    order = _first(w.orders) or {}
    oid = order.get("id")
    reqs = book.requirements(order)

    if plate_id is not None:
        out.append(T.PlaceCleanPlate(oid, (plate_id, counter_cell)))
        out.append(T.ResetPlate(None, (plate_id, counter_cell)))
        out.append(T.DeliverPlate(oid, (plate_id,)))
        for ing in reqs:
            out.append(T.FetchToPlate(oid, (plate_id,), ing))
    if sink_id is not None:
        out.append(T.WashPlate(resources=(sink_id,)))
    if cooker_id is not None:
        out.append(T.ReturnPot(resources=(cooker_id,)))
        out.append(T.RescueCooked(resources=(cooker_id,)))
        out.append(T.Discard(resources=(cooker_id,)))
        out.append(T.RecoverLoosePot(resources=(cooker_id,),
                                     item="utensil_pot_01"))
        for ing in reqs:
            if book.needs_cooking(ing):
                out.append(T.FetchToCooker(oid, (cooker_id,), ing))
                if plate_id is not None:
                    out.append(T.TakeCookedToPlate(
                        oid, (cooker_id, plate_id), ing))
    if board_id is not None:
        for ing in reqs:
            if book.needs_chopping(ing):
                out.append(T.FetchToBoard(oid, (board_id,), ing))
                out.append(T.Chop(oid, (board_id,), ing))
    out.append(T.StashHeld())
    for ing in reqs:
        out.append(T.Prefetch(item=ing))
    return out


# ---------- 命令行 ----------

def load_frame(path, index=0):
    # type: (str, int) -> dict
    """读一帧。.json 当单帧，.ndjson/.gz 按行取第 index 帧。"""
    if path.endswith(".json"):
        with open(path) as f:
            return json.load(f)
    from ai.replay import load_frames
    frames = load_frames(path)
    if not frames:
        raise ValueError("帧文件为空: %s" % path)
    return frames[min(index, len(frames) - 1)]


def main(argv=None):
    # type: (list) -> int
    ap = argparse.ArgumentParser(description="离线构树与冒烟（不注入按键）")
    ap.add_argument("frame", help="帧文件（.json / .ndjson / .ndjson.gz）")
    ap.add_argument("--frame-index", type=int, default=0,
                    help="ndjson 里取第几帧")
    ap.add_argument("--tick", type=int, default=0,
                    help="每棵树走几拍（默认 0，只打印不走）")
    args = ap.parse_args(argv)

    if not os.path.exists(args.frame):
        sys.stderr.write("帧文件不存在: %s\n" % args.frame)
        return 2

    world = World(load_frame(args.frame, args.frame_index))
    ctx, ctrl = make_ctx(world, with_controller=True)

    print("关卡 %s  phase=%s  订单 %r"
          % (world.level, world.phase,
             [o.get("dish") for o in world.orders]))
    print("P2 @ %r  held=%r"
          % (ctx["chef"].get("x"), ctx["chef"].get("held")))
    print("")

    tasks = enumerate_tasks(ctx)
    for t in tasks:
        ok = t.feasible(ctx)
        done = t.done(ctx)
        flag = "✓" if ok else ("·" if not done else "✔done")
        print("%s %r" % (flag, t))
        print("\n".join("    " + l for l in render(t.build())))
        if args.tick > 0:
            tree = t.build()
            for _ in range(args.tick):
                tree.tick(ctx)
                ctx["hands"].tick()
            tree.halt()
        print("")

    print("共 %d 个 task，可行 %d 个"
          % (len(tasks), sum(1 for t in tasks if t.feasible(ctx))))
    print("按键注入：%d 次调用，全部落在 NullController（零真实按键）"
          % len(ctrl.calls))
    if ctx["hands"].held_keys or ctx["hands"].move_dir:
        sys.stderr.write("⚠️ 有残留按键状态：held=%r move=%r\n"
                         % (ctx["hands"].held_keys, ctx["hands"].move_dir))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: 运行测试，确认通过**

Run: `python3 -m pytest tests/test_tree_viz.py -v`
Expected: PASS，16 个测试全绿。

- [ ] **Step 5: 手工跑一遍冒烟，读输出**

Run: `python3 tools/tree_viz.py tests/fixtures/real_frame_sushi_1_4.json`
Expected: 退出码 0；输出含关卡名、订单列表、每个 task 的 ASCII 树，末行报告"零真实按键"。

**读输出时确认三件事**（这是阶段 3 的人工验收，不是自动断言）：
- 树的形状符合 spec §7.3 的样例——`FetchToBoard` 是两个 `InteractWith`，`Chop` 是 `Approach` + `Face` + `HoldUntil`。
- 可行的 task 数量合理（实测帧是暂停态开局、盘都空着，应有 `PlaceCleanPlate` / `FetchToCooker` / `FetchToBoard` 一类可行，`DeliverPlate` 不可行）。
- 无 `⚠️ 有残留按键状态`。

- [ ] **Step 6: 走分支冒烟**

Run: `python3 tools/tree_viz.py tests/fixtures/real_frame_sushi_1_4.json --tick 5`
Expected: 退出码 0，末行仍报告零真实按键、无残留按键告警。

- [ ] **Step 7: 确认全链零 Quartz**

Run: `cd /Users/dongyi/Documents/coding/oc2 && python3 -c "import sys; sys.path.insert(0,'tools'); import tree_viz; assert 'Quartz' not in sys.modules; print('OK: 无 Quartz')"`
Expected: `OK: 无 Quartz`

- [ ] **Step 8: 运行全量测试**

Run: `python3 -m pytest -v`
Expected: 全绿。

- [ ] **Step 9: 提交**

```bash
git add tools/tree_viz.py tests/test_tree_viz.py
git commit -m "feat(tools): tree_viz 离线冒烟，Plan 2 的交付验证

spec §11.2 阶段 3 的验收方式："打印树、假状态走分支、不注入按键"。

与 oc/ai/tree_viz.py 的差异：旧版靠猴补丁绕开真实 controller
（A.Controller = NoopCtrl; A.StateClient = DummyClient），新版不需要——
Hands 构造时注入 controller，NullController 是正常参数。旧版还依赖
Agent._build_tree(w, order)，而新架构里树由每个 task 自己 build()。

placeholder_task 用占位资源强制覆盖全部 15 个 task 类型：
enumerate_tasks 只产出当前帧可行的，某个 build 写错却因当前帧不触发
就会逃过冒烟。test_predicates_evaluate_without_error 同理——谓词抛异常
会让 Planner 整拍崩掉，比判错严重得多。

test_ticking_trees_presses_no_real_keys 走完每棵树后断言零残留按键，
这是 spec §11.5 浸泡测试硬标准的离线预演。

放 tools/ 而非 ai/：开发工具不是运行时模块，与 tools/record_frames.py
同列。spec §8.2 把它列在 ai/ 旁边，但那张表记的是职责归属而非目录——
ai/ 下不该有 __main__ 脚本。记为 Ruling。

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## 完成标准

全部任务完成后应满足：

- `python3 -m pytest -v` 全绿，含 Plan 1 / 1.5 的既有测试。
- `python3 tools/tree_viz.py tests/fixtures/real_frame_sushi_1_4.json` 退出码 0，输出含全部 task 的 ASCII 树，报告零真实按键。
- `grep -rn "Quartz" ai/ tools/tree_viz.py tests/` 只在 `ai/controller.py`（本计划不创建该文件，若无则无输出）命中——**离线链路零 Quartz**。
- `set(ai.tasks.TASK_TYPES) == spec §6.1 的 15 个 task`（由 `test_all_fifteen_spec_tasks_are_registered` 断言）。
- `wc -l ai/*.py ai/tasks/*.py` 无单个文件 > 400 行（spec §11.5 的代码硬指标）。
- `grep -rn "pot_cooked\|dropped_cooked_pot\|RecoverPotAction\|CHOPPABLE" ai/` 无输出——spec §6.1 / §9 要求的三个记忆字段与一张硬编码表都被纯观测可行性与配方数据取代。
- `oc/` 目录零改动（`git status` 不出现 `oc/` 下的文件）。
- 运行时决策路径未动：`oc2/ai/` 下无主循环，旧 AI 照旧能跑（spec §11.2.1 拆分判据）。

## 不在本计划范围

| 项 | 归属 | 理由 |
|---|---|---|
| `arbiter.py`（三行模式表 + 卡住守卫） | Plan 3 | spec §11.2.1 表格 |
| `planner.py`（候选生成 / 可行性过滤 / 打分 / 承诺惯性） | Plan 3 | 本计划交付的是它要挑选的东西与各自的可做条件 |
| `Idle` fallback | Plan 3 | spec §6.1 最后一条：它是流水线第 ⑤ 步之后的 fallback，不是候选集成员 |
| 新 `agent.py` 主循环十三步 | Plan 3 | Global Constraints"不接运行时决策路径" |
| `trace.py` 决策记录 | Plan 3 | 需要 Planner 的 `decision_record` 才有内容可记 |
| `find_plate_counter` / `PLATE_COUNTERS` 的删除 | Plan 3 | spec §9：替代品是 Planner 的多拼装位候选生成 + 认领，不是一个 find 函数。本计划里 `counter_cell` 是构造参数 |
| `delivery_baseline_from` 的接线（谁在什么时候抓基线） | Plan 3 | 本计划交付函数与 `done` 的消费逻辑，Executor 的调用时机归主循环 |
| `p1model.py` / 协作过滤 / 多订单认领 | Plan 4 | spec §11.2.1 表格 |
| 多熟料菜的区分（`Pasta_Marinara_New` 要三份不同熟料） | Plan 3 已知缺口 | 盘里熟料是不透明 token，同盘两份熟料无法区分。本关 Sushi_1_4 每菜一份熟料，不受影响。需靠 count 计数或插件补字段 |
| `L0` 脱困短路（`skip_ticks` 主循环第 ② 步） | Plan 3 | spec §8.1 的主循环装配。本计划交付 `StuckScratch.skip_ticks` 字段与三个脱困原语 |
| 卡住**检测**（`last_cell` / `same_count` 计数与分级升级） | Plan 3 | spec §5.1 的跨模式前置守卫在 Arbiter。本计划交付被它调用的 `MarkBlocked` / `Nudge` / `AbandonTarget` |
| 实机验证 | Plan 3 | 阶段 3 的验收方式是离线冒烟（spec §11.2），实机定性门槛在阶段 4 |
