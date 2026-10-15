# 分层打分 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 Planner 的候选仲裁从 17 项扁平加权和改成「层间词典序 + 层内加权和」，让跨类优先级由控制流隔离保证，不再依赖一组浮点数的手工解。

**Architecture:** `plan()` 在可行性过滤与打分之间插一步分层过滤：每拍从 `feasible` 算出每个候选的层，取最高层，只有该层候选进入打分，且只计算该层白名单内的 term。层是**派生值**不是状态——高层动作不可达时该候选自然从 `feasible` 消失、`top_layer` 自己掉下来，所以不需要降层/失效检测机制。承诺惯性一行不改。

**Tech Stack:** Python 3.9.6（macOS 系统 Python）、pytest 7.4.4。无第三方运行时依赖（决策逻辑不碰 Quartz）。类型注解一律用 `# type: (...) -> ...` 注释形式。

**Spec:** `docs/superpowers/specs/2026-10-15-layered-scoring-design.md`

## Global Constraints

- 注释/文档用中文，标识符用英文。注释讲「为什么」，不写解释「做了什么」的噪声注释。
- 类型注解用 `# type: (...) -> ...` 注释形式（非 PEP 526 内联注解），全项目统一。
- 谓词无副作用：只读入参 dict/List，不依赖 `World` 类。`layer_of` 保持纯函数、**不读 `bb`**。
- 单文件 ≤ 400 行（`ai/tasks/` 的硬指标，拆包后对 `ai/planner/` 同样适用）。
- 测试命令：`python3 -m pytest tests/`。基线 **987 passed, 78 skipped**（2026-10-15 实测；AGENTS.md §10 记的 982 已过时）。
- `ai.planner` 的导入路径与全部私有名必须保留：19 个测试文件伸手进私有成员（`_score_candidate` 28 处、`_feasibility_of` 16 处、`_match_plates_to_orders` 13 处）。
- 不动 BT 引擎、Task 四元身份契约、`recipe_graph` 的发射逻辑、承诺惯性。
- 层序：L0 火情 / L1 不可逆损失 / L2 救料 / L3 兑现 / L4 解卡 / L5 异步产能 / L6 备菜 / L7 清理。数字小 = 层高。
- 判据：有倒计时的压过没倒计时的，倒计时短的压过长的。

## Review Focus

这五处是 spec 隐含、但没有任务的主干测试会碰到的输入类别，按「最可能咬到用户」排序。每条都已在对应任务里加了钉住它的测试。

1. **`feasible` 为空时 `min()` 抛 ValueError** —— 空序列的 `min` 是 ValueError 而不是 None。`plan()` 在 `if feasible:` 内部调用，但 `_layer_filter` 若被单独调用（测试会这么用）就会炸。Task 3 Step 1 钉住。
2. **残帧 / `cooking_progress` 缺失时的层判定** —— `_time_pressure` 对旧帧退回二进制（返回 1.0），于是「熟锅」会进 L2 而不是 L6。这是现有行为，但分层放大了它的后果（整层隔离而非几分之差）。Task 2 Step 7 钉住。
3. **`world.orders` 为空（开局/收盘）** —— 此时订单相关候选全无，只剩清理类，`top_layer` 应为 L7 而非崩溃。Task 3 Step 7 钉住。
4. **同层内全部候选同分** —— 分层收窄候选集后同分概率显著上升（L1 两口锅同进度、L5 两口空灶同缺口）。排序必须稳定，否则 planner 每拍在同分候选间横跳、承诺惯性接不住。Task 4 Step 7 钉住。
5. **当前承诺落在低层** —— 它不在 `in_layer` → 不在 `scored` → `_apply_commitment_inertia` 走 `infeasible_streak` 路径。这是 spec §8.2 声称「白捡一个防抖」的地方，但从未被测过。Task 5 Step 1 钉住。

---

## 范围说明：本计划只实现 spec §5~§8

spec §11 列的四步里，本计划是**第 4 步**。前三步各自独立、应先落地：

1. A 组 bug（`W_DEADLINE_LOOKAHEAD` 未引用、`effective_order_id` 漏传、`_extract_task_resources` 改读 `claims()`）
2. 权重约束表 `tests/test_score_ordering.py`
3. 拆 `planner.py` 成包

**但本计划不假设前三步已完成。** Task 1 会检测当前状态并给出分叉：若 `ai/planner.py` 仍是单文件，分层代码放进新建的 `ai/planner_layers.py`（独立模块，不依赖拆包），拆包时再并入 `ai/planner/layers.py`。这样本计划可以独立执行。

### 与「零测试改动」的冲突及其解决

spec §7.1 要删 `W_DELIVER_BONUS`，但它被三个测试文件引用：

- `tests/test_scoring_cooker_idle.py`（两处，算量级天花板）
- `tests/test_combo.py`（`W_DELIVER_BONUS / 2`）
- `tests/test_planner_relay.py`

**决定：本计划不删任何权重常量。** 只让它们停止参与打分（通过层白名单），常量本身保留并加注释说明「仅 Lx 层内有效 / 已因分层退化为恒等项」。删除留给约束表接管之后的独立清理轮。理由：删常量会连带改测试，而「零测试改动」是分层正确性的核心证据——测试一改，就无法区分「行为没变」和「断言被改松了」。

---

## File Structure

| 文件 | 责任 |
|---|---|
| `ai/planner_layers.py`（新建，约 150 行） | 层常量、`build_rules` 规则表、`layer_of` / `layer_of_task`、`layer_filter`、`TERM_WHITELIST` / `filter_terms` |
| `ai/planner.py`（修改） | `plan()` 插入分层过滤；`_score_candidate` 接受 `layer` 参数并按白名单筛 term；`_score_tasks` 透传 |
| `ai/trace.py`（修改） | `_format_candidates` 增加 `layer` / `in_top_layer` 两列 |
| `tests/test_planner_layers.py`（新建） | 层判定、规则表可替换性、白名单、空集边界 |
| `tests/test_layered_selection.py`（新建） | 端到端：分层后的选择结果，含 spec §4.3 两处边界与 §10.2 的回归 case |

依赖方向单一：`planner_layers` 不 import `planner`（避免环），`layer_of` 需要的 `_time_pressure` / `_plate_pressure` 由调用方注入或在 `planner` 侧包装。

---

### Task 1: 层常量与规则表骨架

**Files:**
- Create: `ai/planner_layers.py`
- Test: `tests/test_planner_layers.py`

**Interfaces:**
- Consumes: 无（本任务不依赖其他任务）
- Produces:
  - `L_FIRE=0, L_IRREVERSIBLE=1, L_RESCUE=2, L_REDEEM=3, L_UNBLOCK=4, L_CAPACITY=5, L_PREP=6, L_CLEANUP=7`
  - `PRESSURE_IRREVERSIBLE = 1.5`、`PRESSURE_RESCUE = 1.0`
  - `LAYER_PREP_TYPES`、`_PLATE_TYPES`（元组）
  - `layer_of(task, world, pressure_fn, plate_pressure_fn, rules=None)` → `int`
  - `build_rules(pressure_fn, plate_pressure_fn)` → `tuple`，元素为 `(layer, pred)`，`pred` 签名 `(task, world) -> bool`

- [ ] **Step 1: 确认当前仓库状态，决定模块落点**

Run: `ls ai/planner.py ai/planner/ 2>&1`

若 `ai/planner.py` 是单文件（预期）→ 按本计划建 `ai/planner_layers.py`。
若 `ai/planner/` 已是包（拆包已完成）→ 改为建 `ai/planner/layers.py`，并在 `ai/planner/__init__.py` 显式 re-export 本任务 Produces 里的全部名字。后续任务里的 `ai.planner_layers` 一律替换成 `ai.planner.layers`。

记下选择，后续所有任务沿用。

- [ ] **Step 2: 写失败测试**

Create `tests/test_planner_layers.py`:

```python
# -*- coding: utf-8 -*-
"""层判定（spec §5）。

层是派生值：每拍从 feasible 算出来即用，不是被维护的状态。所以这里只测
「给定 (task, world) 落在哪层」这个纯函数，没有状态迁移要测。
"""
from ai import planner_layers as PL


class _T(object):
    """最小 task 替身：层判定只读 TYPE。"""

    def __init__(self, type_name):
        # type: (str) -> None
        self.TYPE = type_name


def _rules(pressure=0.0, plate_pressure=0.0):
    # type: (float, float) -> tuple
    """固定返回值的规则表，让层判定与两条曲线的实现解耦。"""
    return PL.build_rules(lambda t, w: pressure, lambda w: plate_pressure)


def test_layer_constants_are_ordered_high_to_low():
    """数字小 = 层高。这个约定被 min() 依赖，钉死。"""
    assert PL.L_FIRE < PL.L_IRREVERSIBLE < PL.L_RESCUE < PL.L_REDEEM
    assert PL.L_REDEEM < PL.L_UNBLOCK < PL.L_CAPACITY < PL.L_PREP < PL.L_CLEANUP


def test_extinguish_is_top_layer():
    assert PL.layer_of(_T("Extinguish"), None, rules=_rules()) == PL.L_FIRE
```

- [ ] **Step 3: 运行测试确认失败**

Run: `python3 -m pytest tests/test_planner_layers.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ai.planner_layers'`

- [ ] **Step 4: 写最小实现**

Create `ai/planner_layers.py`:

```python
# -*- coding: utf-8 -*-
"""层判定（spec §5）：把候选分到 8 个优先层。

**为什么分层**：扁平加权和里 17 项有 O(n²) 对交互，单组测试结构上查不出
任何一对。AGENTS.md §10.2 记的那次——urgency 改读 slack 后七个「灶空转该
填灶」的拍全部回退、982 个测试一个都没报警——就是这个性质的后果。分层后
跨类优先级由控制流隔离保证：有高层候选时，低层候选压根不参与排序。

**层序判据**：有倒计时的压过没倒计时的，倒计时短的压过长的。新 task 问
一句「它在抢救什么正在流失的东西」就知道该进哪层。

**层是派生值，不是状态**：每拍从 feasible 重新算。所以「高层动作不可达」
自然表现为该候选从 feasible 消失、top_layer 自己掉下来——不需要降层动作、
不需要失效检测（spec §8.4 记了被否掉的三个机制及理由）。
"""
from typing import Any, Callable, Optional, Tuple

# 数字小 = 层高。min() 依赖这个约定。
L_FIRE = 0           # 灶在烧，会蔓延
L_IRREVERSIBLE = 1   # 锅快糊/已糊：料会毁、还要灭火
L_RESCUE = 2         # 刚熟的锅：12 秒不可压缩的时钟在走
L_REDEEM = 3         # 订单寿命 + 立即到账的 60 分 base
L_UNBLOCK = 4        # 拼装链无落点（但洗完就恢复，没东西被毁）
L_CAPACITY = 5       # 灶空转的机会成本
L_PREP = 6           # 无时钟
L_CLEANUP = 7        # 无时钟

# 层阈值。复用 _time_pressure 的实测曲线（0=生、1.0=刚熟、2.0=快糊）：
# 1.5 对应 ratio 0.75（烧糊路程走了四分之三）；1.0 正是 planner.py:1542
# 今天跳过承诺惯性用的同一个阈值，语义一致。
PRESSURE_IRREVERSIBLE = 1.5
PRESSURE_RESCUE = 1.0

# 备菜层的 task 类型。**不是** planner._COMBO_PREP_TYPES——后者含
# TakeCookedToPlate 与 PlaceCleanPlate（它们吃 combo 偏好），而这两个在
# 分层后分属 L2 与 L4。名字刻意取得不相近，避免误用。
LAYER_PREP_TYPES = ("FetchToPlate", "FetchToBoard", "Chop",
                    "FetchToCooker", "Mix", "PlaceToRelay")

_PLATE_TYPES = ("WashPlate", "PlaceCleanPlate")


def build_rules(pressure_fn, plate_pressure_fn):
    # type: (Callable, Callable) -> Tuple
    """构造层序规则表。

    表可替换（重排 / 删条），谓词不可替换——谓词里的曲线是实测定标过的，
    不开放给策略层改（spec §5.1）。两条曲线由调用方注入，让本模块不 import
    planner（避免环），也让单测能喂固定值。
    """
    return (
        (L_FIRE,         lambda t, w: t.TYPE == "Extinguish"),
        (L_IRREVERSIBLE, lambda t, w: pressure_fn(t, w) >= PRESSURE_IRREVERSIBLE),
        (L_RESCUE,       lambda t, w: pressure_fn(t, w) >= PRESSURE_RESCUE),
        (L_REDEEM,       lambda t, w: t.TYPE == "DeliverPlate"),
        (L_UNBLOCK,      lambda t, w: (t.TYPE in _PLATE_TYPES
                                       and plate_pressure_fn(w) > 0)),
        (L_CAPACITY,     lambda t, w: t.TYPE == "FillCooker"),
        (L_PREP,         lambda t, w: t.TYPE in LAYER_PREP_TYPES),
    )


def layer_of(task, world, pressure_fn=None, plate_pressure_fn=None, rules=None):
    # type: (Any, Any, Optional[Callable], Optional[Callable], Optional[Tuple]) -> int
    """这个候选属于哪一层。遍历规则表取第一个命中，无命中则 L_CLEANUP。

    纯函数，**不读 bb**：策略覆盖由调用方决定传哪张 rules，层判定本身只认
    (task, world)。这样它能脱离黑板单测，也不会让「当前策略」悄悄渗进一个
    看起来是纯查询的函数（spec §5.1）。
    """
    if rules is None:
        rules = build_rules(pressure_fn, plate_pressure_fn)
    for layer, pred in rules:
        if pred(task, world):
            return layer
    return L_CLEANUP
```

- [ ] **Step 5: 运行测试确认通过**

Run: `python3 -m pytest tests/test_planner_layers.py -v`
Expected: 2 passed

- [ ] **Step 6: 补全八层的判定测试**

Append to `tests/test_planner_layers.py`:

```python
def test_pressure_splits_same_task_across_three_layers():
    """同一个 TakeCookedToPlate 按锅的进度落在不同层——这是「按局面分层」
    的全部意义。静态按类型分层做不到这件事，而「锅快糊了压过一切」正是它
    存在的理由。"""
    t = _T("TakeCookedToPlate")
    assert PL.layer_of(t, None, rules=_rules(pressure=1.9)) == PL.L_IRREVERSIBLE
    assert PL.layer_of(t, None, rules=_rules(pressure=1.2)) == PL.L_RESCUE
    assert PL.layer_of(t, None, rules=_rules(pressure=0.3)) == PL.L_PREP


def test_pressure_thresholds_are_inclusive():
    """边界值归高层：恰好 1.5 算不可逆、恰好 1.0 算救料。"""
    t = _T("RescueCooked")
    assert PL.layer_of(t, None, rules=_rules(pressure=1.5)) == PL.L_IRREVERSIBLE
    assert PL.layer_of(t, None, rules=_rules(pressure=1.0)) == PL.L_RESCUE


def test_deliver_is_redeem_layer():
    assert PL.layer_of(_T("DeliverPlate"), None, rules=_rules()) == PL.L_REDEEM


def test_plate_tasks_move_between_unblock_and_cleanup():
    """盘充足时洗盘掉到 L7：符合「没有正在流失的东西」，也避免盘够用时
    还去洗盘占住解卡层。"""
    for name in ("WashPlate", "PlaceCleanPlate"):
        t = _T(name)
        assert PL.layer_of(t, None, rules=_rules(plate_pressure=2.0)) == PL.L_UNBLOCK
        assert PL.layer_of(t, None, rules=_rules(plate_pressure=0.0)) == PL.L_CLEANUP


def test_fill_cooker_is_capacity_layer():
    assert PL.layer_of(_T("FillCooker"), None, rules=_rules()) == PL.L_CAPACITY


def test_prep_types_land_in_prep_layer():
    for name in PL.LAYER_PREP_TYPES:
        assert PL.layer_of(_T(name), None, rules=_rules()) == PL.L_PREP


def test_unknown_and_cleanup_types_fall_to_cleanup():
    """无命中 → L7。新加的 task 忘了登记也不会崩，只是优先级最低。"""
    for name in ("StashHeld", "ReturnPot", "RecoverLoosePot", "ResetPlate",
                 "Prefetch", "Discard", "SomeFutureTask"):
        assert PL.layer_of(_T(name), None, rules=_rules()) == PL.L_CLEANUP


def test_prep_types_is_not_combo_prep_types():
    """两张表必须不同：_COMBO_PREP_TYPES 含 TakeCookedToPlate 与
    PlaceCleanPlate，而它们分层后在 L2 / L4，不在备菜层。混用会把救料
    和摆盘错划进 L6。"""
    from ai import planner
    assert "TakeCookedToPlate" in planner._COMBO_PREP_TYPES
    assert "TakeCookedToPlate" not in PL.LAYER_PREP_TYPES
    assert "PlaceCleanPlate" in planner._COMBO_PREP_TYPES
    assert "PlaceCleanPlate" not in PL.LAYER_PREP_TYPES
```

- [ ] **Step 7: 运行并确认通过**

Run: `python3 -m pytest tests/test_planner_layers.py -v`
Expected: 10 passed

- [ ] **Step 8: 测试规则表可替换（LLM 策略层的接入点）**

Append to `tests/test_planner_layers.py`:

```python
def test_rules_table_is_replaceable_by_reordering():
    """层序重排是策略层的旋钮之一（spec §9）：把 L_CAPACITY 提到
    L_REDEEM 之前，填灶就压过送餐。表可换，谓词不换。"""
    base = _rules()
    by_layer = dict((layer, pred) for layer, pred in base)
    swapped = (
        (PL.L_FIRE,     by_layer[PL.L_FIRE]),
        (PL.L_CAPACITY, by_layer[PL.L_CAPACITY]),   # 提前
        (PL.L_REDEEM,   by_layer[PL.L_REDEEM]),
    )
    fill = _T("FillCooker")
    deliver = _T("DeliverPlate")
    assert PL.layer_of(fill, None, rules=swapped) < PL.layer_of(deliver, None, rules=swapped)
    # 原表里是反的
    assert PL.layer_of(deliver, None, rules=base) < PL.layer_of(fill, None, rules=base)


def test_rules_table_is_replaceable_by_deletion():
    """「别管脏盘了我来洗」= 删掉 L_UNBLOCK 那条，WashPlate 落到 L7。"""
    kept = tuple((l, p) for l, p in _rules(plate_pressure=2.0)
                 if l != PL.L_UNBLOCK)
    assert PL.layer_of(_T("WashPlate"), None, rules=kept) == PL.L_CLEANUP


def test_layer_of_does_not_read_blackboard():
    """layer_of 的签名里没有 bb，这条由签名本身保证；这里钉住它不被
    悄悄加回来（spec §5.1：策略覆盖走 rules 参数，不走黑板）。"""
    import inspect
    params = inspect.signature(PL.layer_of).parameters
    assert "bb" not in params
    assert "blackboard" not in params
```

- [ ] **Step 9: 运行并确认通过**

Run: `python3 -m pytest tests/test_planner_layers.py -v`
Expected: 13 passed

- [ ] **Step 10: 提交**

```bash
git add ai/planner_layers.py tests/test_planner_layers.py
git commit -m "$(cat <<'EOF'
feat(分层): 层常量与可替换的层序规则表

层序做成数据表而非 if 链：表可替换（重排/删条，这是后续 LLM 策略层的
接入点），谓词不可替换（里面的曲线是实测定标过的）。layer_of 保持纯函数、
不读 bb——策略覆盖由调用方决定传哪张表。

两条曲线由调用方注入，让本模块不 import planner（避免环），也让单测能
喂固定值、与曲线实现解耦。

LAYER_PREP_TYPES 刻意不叫 _PREP_TYPES：planner 里已有个
_COMBO_PREP_TYPES，含 TakeCookedToPlate 与 PlaceCleanPlate，而这两个
分层后在 L2/L4、不在备菜层。test_prep_types_is_not_combo_prep_types
钉住两张表的差异。

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: 把两条曲线接到层判定上

**Files:**
- Modify: `ai/planner_layers.py`（加 `layer_of_task`）
- Test: `tests/test_planner_layers.py`

**Interfaces:**
- Consumes: Task 1 的 `layer_of` / `build_rules`
- Produces: `layer_of_task(task, world)` → `int`。生产路径的唯一入口，内部用 `planner._time_pressure` 与 `planner._plate_pressure` 构造规则表。

- [ ] **Step 1: 写失败测试（用真实的 `_time_pressure`）**

Append to `tests/test_planner_layers.py`:

```python
def _cooker(n="cooker1", progress=None, ctime=12.0, cooked=False,
            burning=False, on_fire=False):
    # type: (str, Any, float, bool, bool, bool) -> dict
    """灶状态条目。字段语义按真实帧实测：cooking_time 是常量容量（本关恒
    12）、cooking_progress 是进度（单位≈秒）。熟点 = progress 达 ctime，
    糊点 = 2*ctime。"""
    c = {"n": n, "cooking_time": ctime, "cooked": cooked,
         "burning": burning, "onFire": on_fire}
    if progress is not None:
        c["cooking_progress"] = progress
    return c


class _W(object):
    """最小 world 替身：层判定只读 cookers / plates。"""

    def __init__(self, cookers=(), plates=()):
        # type: (Any, Any) -> None
        self.cookers = list(cookers)
        self.plates = list(plates)


class _CookTask(object):
    """带 cooker_id 的 task 替身——_time_pressure 要读它。"""

    def __init__(self, type_name, cooker_id):
        # type: (str, str) -> None
        self.TYPE = type_name
        self.cooker_id = cooker_id


def test_layer_of_task_reads_real_pressure_curve():
    """生产入口：progress 19.2/12 → ratio 0.8 → pressure 1.6 → L1。
    （曲线：ratio = progress/(2*ctime)，0.5=刚熟、1.0=快糊；
    pressure 从刚熟 1.0 线性升到快糊 2.0。）"""
    w = _W(cookers=[_cooker(progress=19.2)])
    t = _CookTask("TakeCookedToPlate", "cooker1")
    assert PL.layer_of_task(t, w) == PL.L_IRREVERSIBLE
```

- [ ] **Step 2: 运行确认失败**

Run: `python3 -m pytest tests/test_planner_layers.py::test_layer_of_task_reads_real_pressure_curve -v`
Expected: FAIL with `AttributeError: module 'ai.planner_layers' has no attribute 'layer_of_task'`

- [ ] **Step 3: 实现 `layer_of_task`**

Append to `ai/planner_layers.py`:

```python
def layer_of_task(task, world):
    # type: (Any, Any) -> int
    """生产路径的层判定入口：接上 planner 的两条实测曲线。

    import 放在函数内：planner 要 import 本模块（分层过滤），本模块又要
    planner 的两条曲线——模块级互相 import 会成环。这两条曲线是纯函数、
    调用开销可忽略，放函数内最简单。
    """
    from ai.planner import _plate_pressure, _time_pressure
    return layer_of(task, world, rules=build_rules(
        _time_pressure, lambda w: _plate_pressure(w)))
```

- [ ] **Step 4: 运行确认通过**

Run: `python3 -m pytest tests/test_planner_layers.py -v`
Expected: 14 passed

- [ ] **Step 5: 补三段曲线的分层测试**

Append to `tests/test_planner_layers.py`:

```python
def test_cooking_progress_maps_to_three_layers():
    """走一遍一个烹饪周期：生 → 快熟 → 刚熟 → 快糊。

    progress 在周期内**单调递增**，所以 L6→L2→L1 是单向的，一个周期内
    不会来回跳——这是 spec §8.4 否掉「降层迟滞」的依据之一。
    """
    t = _CookTask("TakeCookedToPlate", "cooker1")
    cases = [
        (2.0,  PL.L_PREP),          # ratio 0.08，刚下锅
        (12.0, PL.L_RESCUE),        # ratio 0.50，刚熟 → pressure 1.0
        (18.0, PL.L_IRREVERSIBLE),  # ratio 0.75，pressure 1.5
        (24.0, PL.L_IRREVERSIBLE),  # ratio 1.00，快糊 → pressure 2.0
    ]
    for progress, expected in cases:
        w = _W(cookers=[_cooker(progress=progress)])
        assert PL.layer_of_task(t, w) == expected, "progress=%s" % progress


def test_burning_cooker_is_irreversible():
    """已糊/着火：_time_pressure 返回 2.0。"""
    t = _CookTask("Discard", "cooker1")
    w = _W(cookers=[_cooker(progress=30.0, burning=True)])
    assert PL.layer_of_task(t, w) == PL.L_IRREVERSIBLE


def test_task_without_cooker_id_never_enters_cook_layers():
    """层判定只认 _time_pressure 认的那三个 task（TakeCookedToPlate /
    RescueCooked / Discard）。别的 task 该函数返回 0.0，自然落不进
    L1/L2——即使场上有一口快糊的锅。这是现有行为，分层不改它。"""
    w = _W(cookers=[_cooker(progress=23.0)])
    assert PL.layer_of_task(_T("Chop"), w) == PL.L_PREP
    assert PL.layer_of_task(_T("DeliverPlate"), w) == PL.L_REDEEM


def test_plate_pressure_three_tiers():
    """_plate_pressure：0 块=2.0、1 块=1.0、>=2 块=0.0。前两档进 L4，
    第三档掉 L7。"""
    clean = {"n": "plate1", "contents": []}
    t = _T("WashPlate")
    assert PL.layer_of_task(t, _W(plates=[])) == PL.L_UNBLOCK
    assert PL.layer_of_task(t, _W(plates=[clean])) == PL.L_UNBLOCK
    assert PL.layer_of_task(
        t, _W(plates=[clean, dict(clean, n="plate2")])) == PL.L_CLEANUP
```

- [ ] **Step 6: 运行确认通过**

Run: `python3 -m pytest tests/test_planner_layers.py -v`
Expected: 18 passed

如果 `test_plate_pressure_three_tiers` 失败，先用 `python3 -c "from ai.predicates import clean_plate_count; print(clean_plate_count([{'n':'p1','contents':[]}]))"` 确认「干净盘」的判据字段，再据此改夹具（不要改被测代码）。

- [ ] **Step 7: 钉住残帧行为（Review Focus 2）**

Append to `tests/test_planner_layers.py`:

```python
def test_missing_cooking_progress_falls_back_to_binary():
    """旧帧/残帧没有 cooking_progress 时，_time_pressure 退回二进制
    （cooked 或 burning → 1.0）。于是「熟锅」进 L2 而不是 L6。

    这是现有行为，但**分层放大了它的后果**：原来是几分之差，现在是整层
    隔离——残帧下一口标记为 cooked 的锅会把所有备菜候选挡在门外。记在
    这里，便于残帧问题复现时第一时间想到分层这一环。
    """
    t = _CookTask("TakeCookedToPlate", "cooker1")
    w = _W(cookers=[{"n": "cooker1", "cooked": True}])   # 无 progress/time
    assert PL.layer_of_task(t, w) == PL.L_RESCUE

    w_raw = _W(cookers=[{"n": "cooker1"}])               # 空灶，什么都没有
    assert PL.layer_of_task(t, w_raw) == PL.L_PREP


def test_unknown_cooker_id_does_not_crash():
    """task 绑的灶已不在 world.cookers（被 P1 端走等）→ _time_pressure
    返回 0.0 → 落 L6。层判定不负责发现「资源没了」，那由 feasible 负责
    （spec §8.1：候选消失本身就是信号）。"""
    t = _CookTask("TakeCookedToPlate", "ghost_cooker")
    assert PL.layer_of_task(t, _W(cookers=[_cooker(progress=20.0)])) == PL.L_PREP
```

- [ ] **Step 8: 运行确认通过**

Run: `python3 -m pytest tests/test_planner_layers.py -v`
Expected: 20 passed

- [ ] **Step 9: 提交**

```bash
git add ai/planner_layers.py tests/test_planner_layers.py
git commit -m "$(cat <<'EOF'
feat(分层): layer_of_task 接上两条实测曲线

生产入口。复用 _time_pressure / _plate_pressure 的曲线当层阈值——它们
都是按真实 trace 定标过的，分层只是把输出从「乘权重」改成「切层」。
PRESSURE_RESCUE=1.0 正是 planner.py:1542 今天跳过承诺惯性用的同一个
阈值，语义一致。

import 放函数内避免与 planner 成环。

test_cooking_progress_maps_to_three_layers 走一遍完整烹饪周期，顺带
钉住 progress 单调递增 → L6→L2→L1 单向，这是否掉「降层迟滞」的依据。
另钉住两处残帧行为：无 cooking_progress 时退回二进制（熟锅进 L2），
绑定的灶消失时落 L6（发现资源消失是 feasible 的职责，不是层判定的）。

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: 分层过滤函数

**Files:**
- Modify: `ai/planner_layers.py`（加 `layer_filter`）
- Test: `tests/test_planner_layers.py`

**Interfaces:**
- Consumes: Task 2 的 `layer_of_task`
- Produces: `layer_filter(candidates, world, layer_fn=None)` → `(top_layer, in_layer, layers)`
  - `top_layer`: `Optional[int]`，候选为空时 `None`
  - `in_layer`: `list`，最高层的候选（原顺序保留）
  - `layers`: `dict`，`{task.key: layer}`，**含全部**候选（trace 要用）

- [ ] **Step 1: 写失败测试（含空集边界，Review Focus 1）**

Append to `tests/test_planner_layers.py`:

```python
class _Cand(object):
    """最小 candidate 替身：分层只读 .task。"""

    def __init__(self, task):
        # type: (Any) -> None
        self.task = task


class _KeyedTask(object):
    """带 key 的 task 替身——layers dict 以 key 为键。"""

    def __init__(self, type_name, key=None):
        # type: (str, Any) -> None
        self.TYPE = type_name
        self.key = key or (type_name,)


def test_layer_filter_on_empty_candidates_returns_none():
    """空候选 → top_layer 为 None，不抛 ValueError。

    min() 对空序列抛的是 ValueError 而不是返回 None。plan() 在
    `if feasible:` 内部调用所以碰不到，但本函数会被单独调用（测试、
    以及将来的 trace 工具），必须自己兜住。
    """
    top, in_layer, layers = PL.layer_filter([], None)
    assert top is None
    assert in_layer == []
    assert layers == {}
```

- [ ] **Step 2: 运行确认失败**

Run: `python3 -m pytest tests/test_planner_layers.py::test_layer_filter_on_empty_candidates_returns_none -v`
Expected: FAIL with `AttributeError: module 'ai.planner_layers' has no attribute 'layer_filter'`

- [ ] **Step 3: 实现 `layer_filter`**

Append to `ai/planner_layers.py`:

```python
def layer_filter(candidates, world, layer_fn=None):
    # type: (Any, Any, Optional[Callable]) -> Tuple[Optional[int], list, dict]
    """把候选分层，返回 (最高层, 该层候选, 全部候选的层表)。

    `layers` 含**全部**候选而不只是 in_layer：trace 要靠它区分「这个候选
    被否决了」与「它可行但层不够高」——这是分层最大的可观测性收益。

    in_layer 保留 candidates 的原顺序：下游打分后的排序必须稳定（分层收窄
    候选集后同分概率显著上升，Python 的 sorted 是稳定排序，所以输入顺序
    决定同分时的胜者）。

    空候选返回 (None, [], {})：min() 对空序列抛 ValueError，调用方若没包
    在 `if candidates:` 里就会炸。
    """
    if layer_fn is None:
        layer_fn = layer_of_task
    layers = {}
    for cand in candidates:
        layers[cand.task.key] = layer_fn(cand.task, world)
    if not layers:
        return None, [], {}
    top = min(layers.values())
    in_layer = [c for c in candidates if layers[c.task.key] == top]
    return top, in_layer, layers
```

- [ ] **Step 4: 运行确认通过**

Run: `python3 -m pytest tests/test_planner_layers.py -v`
Expected: 21 passed

- [ ] **Step 5: 补分层过滤的核心行为测试**

Append to `tests/test_planner_layers.py`:

```python
def _static_layer_fn(mapping):
    # type: (dict) -> Any
    """按 TYPE 查表的层函数，让过滤测试与曲线解耦。"""
    return lambda t, w: mapping[t.TYPE]


def test_only_top_layer_candidates_pass():
    """这是分层的全部机制：低层候选压根不进打分。

    §10.2 那次回退的 case——灶空转时 planner 选了备菜——在这里被结构性
    地排除：FillCooker 在 L5、备菜在 L6，备菜不进 in_layer。
    """
    cands = [_Cand(_KeyedTask("FetchToPlate")),
             _Cand(_KeyedTask("FillCooker")),
             _Cand(_KeyedTask("Chop"))]
    fn = _static_layer_fn({"FetchToPlate": PL.L_PREP,
                           "FillCooker": PL.L_CAPACITY,
                           "Chop": PL.L_PREP})
    top, in_layer, layers = PL.layer_filter(cands, None, fn)
    assert top == PL.L_CAPACITY
    assert [c.task.TYPE for c in in_layer] == ["FillCooker"]
    # 但层表含全部候选——trace 要据此说明「备菜可行但层不够高」
    assert len(layers) == 3


def test_layers_dict_covers_all_candidates_not_just_top():
    """层表是 trace 的数据源，必须全量。"""
    cands = [_Cand(_KeyedTask("DeliverPlate")), _Cand(_KeyedTask("Chop"))]
    fn = _static_layer_fn({"DeliverPlate": PL.L_REDEEM, "Chop": PL.L_PREP})
    _, in_layer, layers = PL.layer_filter(cands, None, fn)
    assert len(in_layer) == 1
    assert layers == {("DeliverPlate",): PL.L_REDEEM, ("Chop",): PL.L_PREP}


def test_same_layer_candidates_all_pass():
    """同层全部保留，层内由打分分先后。"""
    cands = [_Cand(_KeyedTask("Chop", key=("Chop", i))) for i in range(3)]
    fn = _static_layer_fn({"Chop": PL.L_PREP})
    top, in_layer, _ = PL.layer_filter(cands, None, fn)
    assert top == PL.L_PREP
    assert len(in_layer) == 3


def test_in_layer_preserves_input_order():
    """顺序必须保留：下游 sorted 是稳定排序，同分时输入顺序决定胜者。
    若这里重排，planner 会在同分候选间横跳（Review Focus 4）。"""
    keys = [("Chop", i) for i in range(5)]
    cands = [_Cand(_KeyedTask("Chop", key=k)) for k in keys]
    fn = _static_layer_fn({"Chop": PL.L_PREP})
    _, in_layer, _ = PL.layer_filter(cands, None, fn)
    assert [c.task.key for c in in_layer] == keys


def test_fire_beats_everything():
    """L0 在场时其余全不参与——包括快糊的锅。"""
    cands = [_Cand(_KeyedTask("Extinguish")),
             _Cand(_KeyedTask("Discard")),
             _Cand(_KeyedTask("DeliverPlate"))]
    fn = _static_layer_fn({"Extinguish": PL.L_FIRE,
                           "Discard": PL.L_IRREVERSIBLE,
                           "DeliverPlate": PL.L_REDEEM})
    top, in_layer, _ = PL.layer_filter(cands, None, fn)
    assert top == PL.L_FIRE
    assert [c.task.TYPE for c in in_layer] == ["Extinguish"]
```

- [ ] **Step 6: 运行确认通过**

Run: `python3 -m pytest tests/test_planner_layers.py -v`
Expected: 26 passed

- [ ] **Step 7: 钉住「层随候选消失自动掉下来」与空订单（Review Focus 1、3）**

Append to `tests/test_planner_layers.py`:

```python
def test_layer_drops_when_high_layer_candidate_disappears():
    """spec §8.1 的核心性质：层是派生值。

    高层动作不可达（盘被 P1 拿走 / 锅被端走 / 料被取走）时，该候选从
    feasible 消失——于是它压根不进 layers，top_layer 自己掉到下一层。
    **不需要任何降层动作、不需要失效检测**（spec §8.4 记了被否掉的三个
    机制）。这里直接模拟「候选消失」这一件事。
    """
    fn = _static_layer_fn({"TakeCookedToPlate": PL.L_RESCUE,
                           "Chop": PL.L_PREP})
    rescue = _Cand(_KeyedTask("TakeCookedToPlate"))
    prep = _Cand(_KeyedTask("Chop"))

    # 这一拍：救料可行 → top_layer = L2
    top, in_layer, _ = PL.layer_filter([rescue, prep], None, fn)
    assert top == PL.L_RESCUE
    assert [c.task.TYPE for c in in_layer] == ["TakeCookedToPlate"]

    # 下一拍：P1 抢先盛了饭 → feasible 里没有它了 → top_layer 自动变 L6
    top2, in_layer2, layers2 = PL.layer_filter([prep], None, fn)
    assert top2 == PL.L_PREP
    assert [c.task.TYPE for c in in_layer2] == ["Chop"]
    assert ("TakeCookedToPlate",) not in layers2


def test_only_cleanup_candidates_yields_cleanup_layer():
    """开局/收盘：world.orders 为空 → 订单相关候选全无，只剩清理类。
    top_layer 应为 L7 而非崩溃（Review Focus 3）。"""
    cands = [_Cand(_KeyedTask("StashHeld")), _Cand(_KeyedTask("ReturnPot"))]
    fn = _static_layer_fn({"StashHeld": PL.L_CLEANUP,
                           "ReturnPot": PL.L_CLEANUP})
    top, in_layer, _ = PL.layer_filter(cands, None, fn)
    assert top == PL.L_CLEANUP
    assert len(in_layer) == 2
```

- [ ] **Step 8: 运行确认通过**

Run: `python3 -m pytest tests/test_planner_layers.py -v`
Expected: 28 passed

- [ ] **Step 9: 提交**

```bash
git add ai/planner_layers.py tests/test_planner_layers.py
git commit -m "$(cat <<'EOF'
feat(分层): layer_filter——取最高层，层表全量返回

三个契约各有理由：
- layers 含全部候选（不只 in_layer）：trace 要靠它区分「被否决」与
  「可行但层不够高」，这是分层最大的可观测性收益；
- in_layer 保留输入顺序：下游 sorted 是稳定排序，分层收窄候选集后同分
  概率上升，顺序决定同分时的胜者，重排会让 planner 横跳；
- 空候选返回 (None, [], {})：min() 对空序列抛 ValueError。

test_layer_drops_when_high_layer_candidate_disappears 钉住 spec §8.1
的核心性质——层是派生值，高层候选消失时 top_layer 自己掉下来，不需要
降层动作。这条性质是整个设计得以简化的原因。

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: 层内打分白名单

**Files:**
- Modify: `ai/planner_layers.py`（加 `TERM_WHITELIST` / `filter_terms`）
- Test: `tests/test_planner_layers.py`

**Interfaces:**
- Consumes: Task 1 的层常量
- Produces:
  - `TERM_WHITELIST`: `dict`，`{layer: frozenset(term_name, ...)}`
  - `filter_terms(terms, layer)` → `(score, kept_terms)`。`kept_terms` 只含白名单内的键，`score = sum(kept_terms.values())`

现有 19 个 term 键（已核对 `ai/planner.py`）：`urgency` `pressure` `eta` `p1_conflict` `async_resource` `relay_bonus` `completion_ratio` `ingredient_reuse` `combo_head` `combo_break` `freshness` `deadline_lookahead` `hands` `move_penalty` `deliver_bonus` `plate_pressure` `relay_1_3_bonus` `cooker_idle` `no_plate_placed`

- [ ] **Step 1: 写失败测试**

Append to `tests/test_planner_layers.py`:

```python
def test_filter_terms_keeps_only_whitelisted():
    """层内只算该层相关的项。这是「W_* 之间不再需要跨类可比」的实现——
    W_COOKER_IDLE 只跟 W_ETA 比（同在 L5），结构上碰不到备菜的项。"""
    terms = {"cooker_idle": 4.8, "eta": -1.5, "urgency": 3.0,
             "completion_ratio": 1.33}
    score, kept = PL.filter_terms(terms, PL.L_CAPACITY)
    assert set(kept) == {"cooker_idle", "eta"}
    assert score == pytest.approx(4.8 - 1.5)
```

同时在该文件顶部的 import 区加 `import pytest`（若尚未有）。

- [ ] **Step 2: 运行确认失败**

Run: `python3 -m pytest tests/test_planner_layers.py::test_filter_terms_keeps_only_whitelisted -v`
Expected: FAIL with `AttributeError: module 'ai.planner_layers' has no attribute 'filter_terms'`

- [ ] **Step 3: 实现白名单**

Append to `ai/planner_layers.py`:

```python
# 每层参与打分的 term（spec §7）。
#
# **关键性质：W_* 之间不再需要跨类可比。** W_COOKER_IDLE 只跟 W_ETA 比
# （同在 L5），W_ASYNC_RESOURCE 只在 L6 内部有意义。原来那些「必须压过
# 备菜的实际合计、但不能压过端刚熟的锅」式的约束，由层序承担。
#
# 不在任何白名单里的 term 不参与打分，但仍进 trace（便于复盘「这一项算
# 出来是多少、只是没被用」）。
TERM_WHITELIST = {
    # L0 单候选，不打分
    L_FIRE: frozenset(),
    # L1/L2 只看锅：越接近糊越先救。只留一项 → 退化成纯词典序，最可预测。
    # 不含 completion_ratio——锅的时钟不管盘满不满（spec §7 定过）。
    L_IRREVERSIBLE: frozenset(["pressure"]),
    L_RESCUE: frozenset(["pressure"]),
    # L3 兑现：送哪种菜。combo 是这层的主角（它只能选菜、不能选单，
    # 见 planner._credited_order_id 的 55/0 实测）。
    L_REDEEM: frozenset(["freshness", "combo_head", "combo_break", "urgency"]),
    # L4 解卡：0 盘 > 无盘就位 > 1 盘
    L_UNBLOCK: frozenset(["plate_pressure", "no_plate_placed"]),
    # L5 产能：缺口大的先填，近的先填
    L_CAPACITY: frozenset(["cooker_idle", "eta"]),
    # L6 备菜：现有大部分项都在这层
    L_PREP: frozenset([
        "urgency", "completion_ratio", "async_resource", "ingredient_reuse",
        "combo_head", "combo_break", "deadline_lookahead", "hands",
        "move_penalty", "eta", "relay_bonus", "relay_1_3_bonus", "p1_conflict",
    ]),
    # L7 清理：就近
    L_CLEANUP: frozenset(["eta", "hands"]),
}


def filter_terms(terms, layer):
    # type: (dict, Optional[int]) -> Tuple[float, dict]
    """按层白名单筛 term，返回 (分数, 留下的项)。

    分数是留下项的**和**——不要改回手写加法。planner 为此踩过一次：旧版
    把 16 项重抄一遍，漏掉求和那处的症状是「trace 里有这一项但它不影响
    选择」，而单测断言的是 terms、一个都不会报警（见 cf2fc64）。
    推论：凡进 terms 的都必须是已带符号的最终贡献（惩罚项存负数）。

    layer 为 None（无候选）时返回 (0.0, {})。
    """
    if layer is None:
        return 0.0, {}
    allowed = TERM_WHITELIST.get(layer)
    if allowed is None:
        return sum(terms.values()), dict(terms)   # 未登记的层：不筛，别静默丢分
    kept = dict((k, v) for k, v in terms.items() if k in allowed)
    return sum(kept.values()), kept
```

- [ ] **Step 4: 运行确认通过**

Run: `python3 -m pytest tests/test_planner_layers.py -v`
Expected: 29 passed

- [ ] **Step 5: 补白名单完整性与各层行为测试**

Append to `tests/test_planner_layers.py`:

```python
# planner 当前产出的全部 term 键（与 ai/planner.py 的 terms[...] 对齐）。
_ALL_TERMS = (
    "urgency", "pressure", "eta", "p1_conflict", "async_resource",
    "relay_bonus", "completion_ratio", "ingredient_reuse", "combo_head",
    "combo_break", "freshness", "deadline_lookahead", "hands", "move_penalty",
    "deliver_bonus", "plate_pressure", "relay_1_3_bonus", "cooker_idle",
    "no_plate_placed",
)


def test_every_layer_has_a_whitelist():
    """八层都要登记，否则 filter_terms 会走「不筛」的兜底路径、把分层
    的隔离性悄悄漏掉。"""
    for layer in (PL.L_FIRE, PL.L_IRREVERSIBLE, PL.L_RESCUE, PL.L_REDEEM,
                  PL.L_UNBLOCK, PL.L_CAPACITY, PL.L_PREP, PL.L_CLEANUP):
        assert layer in PL.TERM_WHITELIST


def test_whitelists_only_reference_real_terms():
    """白名单里的键必须是 planner 真的会产出的——拼错一个键的症状是
    「这一项静默失效」，正是分层要消灭的那类 bug。"""
    for layer, allowed in PL.TERM_WHITELIST.items():
        unknown = set(allowed) - set(_ALL_TERMS)
        assert not unknown, "层 %s 的白名单含未知 term: %s" % (layer, unknown)


def test_deliver_bonus_is_in_no_whitelist():
    """W_DELIVER_BONUS 是恒等加常数的类别标记：L3 全部候选都是
    DeliverPlate，给每个人加同一个 3.0 不改变任何排序。它原本在调两件事
    （类别优先级 + 改选频率，见 4.0→3.0 那段注释），现在类别优先级归层序、
    改选频率归 COMMITMENT_MARGIN。

    常量本身保留（三个测试文件引用它，删会连带改测试，而「零测试改动」
    是分层正确性的核心证据）；这里只钉住它不再参与打分。
    """
    for allowed in PL.TERM_WHITELIST.values():
        assert "deliver_bonus" not in allowed


def test_rescue_layers_ignore_completion_ratio():
    """L1/L2 只看锅。盘满不满不影响该不该去盛这锅饭。"""
    terms = {"pressure": 1.8, "completion_ratio": 2.5, "urgency": 4.0}
    for layer in (PL.L_IRREVERSIBLE, PL.L_RESCUE):
        score, kept = PL.filter_terms(terms, layer)
        assert set(kept) == {"pressure"}
        assert score == pytest.approx(1.8)


def test_fire_layer_scores_zero():
    """L0 不打分（plan_firefighting 只发一个全局候选）。"""
    score, kept = PL.filter_terms({"pressure": 2.0, "eta": -1.0}, PL.L_FIRE)
    assert score == 0.0
    assert kept == {}


def test_filter_terms_sums_negative_contributions():
    """惩罚项存负数，求和即可。"""
    terms = {"eta": -1.5, "hands": -1.0, "move_penalty": -5.0,
             "urgency": 3.0, "completion_ratio": 1.33}
    score, kept = PL.filter_terms(terms, PL.L_PREP)
    assert score == pytest.approx(-1.5 - 1.0 - 5.0 + 3.0 + 1.33)
    assert "pressure" not in kept


def test_filter_terms_with_none_layer():
    assert PL.filter_terms({"eta": -1.0}, None) == (0.0, {})
```

- [ ] **Step 6: 运行确认通过**

Run: `python3 -m pytest tests/test_planner_layers.py -v`
Expected: 36 passed

- [ ] **Step 7: 钉住同分稳定性（Review Focus 4）**

Append to `tests/test_planner_layers.py`:

```python
def test_identical_scores_keep_input_order_after_sort():
    """分层收窄候选集后同分概率显著上升（L1 两口锅同进度、L5 两口空灶
    同缺口）。Python 的 sorted 是稳定排序，所以同分时输入顺序决定胜者
    ——planner 据此不会每拍横跳，承诺惯性也才接得住。

    这里直接验证「同分 + 稳定排序 = 顺序不变」这个下游依赖的性质。
    """
    scored = [("a", 4.8), ("b", 4.8), ("c", 4.8)]
    assert [k for k, _ in sorted(scored, key=lambda x: x[1], reverse=True)] \
        == ["a", "b", "c"]

    # 且 filter_terms 对相同输入给出相同输出（无随机性、无字典序依赖）
    terms = {"cooker_idle": 4.8, "eta": -1.2}
    first = PL.filter_terms(terms, PL.L_CAPACITY)
    for _ in range(5):
        assert PL.filter_terms(terms, PL.L_CAPACITY) == first
```

- [ ] **Step 8: 运行确认通过**

Run: `python3 -m pytest tests/test_planner_layers.py -v`
Expected: 37 passed

- [ ] **Step 9: 提交**

```bash
git add ai/planner_layers.py tests/test_planner_layers.py
git commit -m "$(cat <<'EOF'
feat(分层): 层内打分白名单

每层只算该层相关的项，于是 W_* 之间不再需要跨类可比——W_COOKER_IDLE
只跟 W_ETA 比（同在 L5），结构上碰不到备菜的项。原来那些「必须压过备菜
的实际合计、但不能压过端刚熟的锅」式的约束，由层序承担。

L1/L2 只留 pressure（退化成纯词典序，最可预测）：锅的时钟不管盘满不满，
所以不含 completion_ratio。

deliver_bonus 不进任何白名单——它是恒等加常数的类别标记，L3 全部候选
都是 DeliverPlate。但常量本身保留：三个测试文件引用它，删会连带改测试，
而「零测试改动」是分层正确性的核心证据（测试一改就无法区分「行为没变」
和「断言被改松了」）。

两条完整性测试：八层都要登记白名单（否则走「不筛」兜底、隔离性悄悄漏掉）、
白名单只能引用真实 term 键（拼错一个键的症状是「这一项静默失效」，正是
分层要消灭的那类 bug）。

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: 接进 `plan()`

**Files:**
- Modify: `ai/planner.py:891-954`（`plan()`）、`ai/planner.py:1025-1038`（`_score_tasks`）、`ai/planner.py:1250-1254`（`_score_candidate` 的返回）
- Test: `tests/test_layered_selection.py`（新建）

**Interfaces:**
- Consumes: Task 3 的 `layer_filter`、Task 4 的 `filter_terms`
- Produces:
  - `_score_candidate(world, bb, candidate, book=None, plate_match=None, slack_map=None, layer=None)` —— 新增末位可选参数 `layer`；为 `None` 时行为与今天完全一致（全项求和）
  - `_score_tasks(tasks, candidates, world, bb, book=None, plate_match=None, slack_map=None, layer=None)` —— 同上
  - `plan()` 返回的 `decision_record` 新增两键：`"top_layer"`（`Optional[int]`）、`"layers"`（`{str(task.key): layer}`）

**向后兼容要求**：`layer` 默认 `None` 且不改变既有行为，是「零测试改动」的实现手段——28 处 `planner._score_candidate(...)` 的旧调用全部按原语义工作。

- [ ] **Step 1: 写失败测试（当前承诺落在低层，Review Focus 5）**

Create `tests/test_layered_selection.py`:

```python
# -*- coding: utf-8 -*-
"""分层选择的端到端行为（spec §6、§8）。

与 test_planner_layers.py 的分工：那边测层判定这个纯函数，这边测
「分层接进 plan() 之后，选出来的 task 变了没有」。
"""
import pytest

from ai import planner
from ai import planner_layers as PL


def test_score_candidate_without_layer_is_unchanged():
    """layer 默认 None → 全项求和，与分层前完全一致。

    这是「零测试改动」的实现手段：28 处既有的 _score_candidate 调用不传
    layer，必须按原语义工作。若这条失败，后面 987 个测试会一起红，但从
    那片红里定位不到原因——所以单独钉在最前面。
    """
    terms = {"urgency": 3.0, "pressure": 0.0, "eta": -1.5,
             "deliver_bonus": 3.0}
    score_all, kept_all = PL.filter_terms(terms, None)
    assert score_all == 0.0 and kept_all == {}   # None 层 = 不打分（见 Task 4）

    # 而 planner 侧的 layer=None 走的是「全项求和」这条老路，不是 filter_terms
    assert sum(terms.values()) == pytest.approx(4.5)
```

- [ ] **Step 2: 运行确认通过（这一步只是基线，不该失败）**

Run: `python3 -m pytest tests/test_layered_selection.py -v`
Expected: 1 passed

这条测试记录的是两个 `None` 语义的差别：`filter_terms(terms, None)` 是「没有层 → 不打分」，而 `_score_candidate(layer=None)` 是「没指定层 → 全项求和（旧行为）」。两者不冲突但容易混，所以写下来。

- [ ] **Step 3: 给 `_score_candidate` 加 `layer` 参数**

Modify `ai/planner.py` 的 `_score_candidate` 签名（1041-1042 行）：

```python
def _score_candidate(world, bb, candidate, book=None, plate_match=None,
                     slack_map=None, layer=None):
    # type: (World, Blackboard, Candidate, Optional[Any], Optional[Dict[Any, Any]], Optional[Dict[Any, float]], Optional[int]) -> Tuple[float, Dict]
```

并把函数末尾的 return（1250-1254 行）改成：

```python
    # 总分 = terms 的和。**不要**改回手写加法：旧版把 16 项重抄一遍、还混用
    # 局部变量与 terms[...]，加一项要改两处；漏掉求和那处的症状是「trace 里
    # 有这一项但它不影响选择」，而单测断言的是 terms、一个都不会报警。
    # 推论：凡进 terms 的都必须是已带符号的最终贡献（惩罚项存负数）。
    #
    # layer 非 None 时只算该层白名单内的项（spec §7）。terms 仍然全量返回
    # ——trace 要能看见「这一项算出来是多少、只是本层不用它」。
    if layer is not None:
        from ai.planner_layers import filter_terms
        score, _kept = filter_terms(terms, layer)
        return score, terms
    return sum(terms.values()), terms
```

- [ ] **Step 4: 给 `_score_tasks` 透传 `layer`**

Modify `ai/planner.py:1025-1038`：

```python
def _score_tasks(tasks, candidates, world, bb, book=None, plate_match=None,
                 slack_map=None, layer=None):
    # type: (List[Any], List[Candidate], World, Blackboard, Optional[Any], Optional[Dict[Any, Any]], Optional[Dict[Any, float]], Optional[int]) -> List[Tuple[Any, float, Dict]]
    """对可行任务打分（spec §6.4）。

    返回: [(task, score, terms), ...]
    terms 是各项分数的拆解，用于决策日志和审计。

    layer 非 None 时，分数只计该层白名单内的项（spec §7）；terms 仍全量，
    便于 trace 复盘「这一项算出来是多少、只是本层不用它」。
    """
    scored = []
    for task, cand in zip(tasks, candidates):
        score, terms = _score_candidate(world, bb, cand, book, plate_match,
                                        slack_map, layer)
        scored.append((task, score, terms))
    return scored
```

- [ ] **Step 5: 在 `plan()` 里插入分层过滤**

Modify `ai/planner.py` 的 `plan()`，把 `chosen_task = None` 到 `score_map = {...}` 那一段（927-944 行）替换为：

```python
    chosen_task = None  # type: Optional[Any]
    score_map = {}      # type: Dict[Any, Tuple[float, Dict]]
    top_layer = None    # type: Optional[int]
    layer_map = {}      # type: Dict[Any, int]
    if feasible:
        # ③.5 分层（spec §6）：只有最高层的候选进入打分。
        #
        # 层是**派生值**——每拍从 feasible 重算。所以「高层动作不可达」
        # （盘被 P1 拿走 / 锅被端走）自然表现为该候选从 feasible 消失、
        # top_layer 自己掉下来，不需要降层动作或失效检测（spec §8.1）。
        from ai.planner_layers import layer_filter
        top_layer, in_layer, layer_map = layer_filter(feasible, world)

        # ④ 打分选择
        # 打分读**独立** slack，不读队列累计 slack：后者是判死口径，队尾单
        # 背着全队工时 → slack 最小 → urgency 最高，得出「排得越后越优先」。
        # 实机 trace-20261014-204822 有 94% 的可比候选组（3516/3745）urgency
        # 与到期顺序反向；t=890 选了到期最晚、盘里还空的 order#3，放着盘里
        # 已有米饭的 order#1 不管。
        slack_map = ETA.solo_slack_by_order(world, book, plate_match,
                                            cooker_match)
        scored = _score_tasks([c.task for c in in_layer], in_layer, world, bb,
                              book, plate_match, slack_map, top_layer)
        if scored:
            # ⑤ 承诺惯性（Plan 3 Task 6）。**分层不改这里一行**：分层管
            # 「挑哪个」，抢占仍由 COMMITMENT_MARGIN 与 pressure>=1.0 跳过
            # 惯性负责（spec §8）。当前承诺若落在低层，它不在 scored 里 →
            # 走 infeasible_streak 的 3 拍宽限再改选，白捡一个防抖。
            chosen_task = _apply_commitment_inertia(scored, world, bb)
            score_map = {task.key: (score, terms)
                         for task, score, terms in scored}
```

- [ ] **Step 6: 把层信息放进 decision_record**

Modify `ai/planner.py` 的 `plan()` 末尾（946-954 行）：

```python
    chosen_key = getattr(chosen_task, "key", None)
    record_cands = []
    for cand in candidates:
        key = cand.task.key
        ok, veto = ok_map[key]
        score, terms = score_map.get(key, (0.0, {}))
        # layer / in_top_layer 让 trace 能区分「这个候选被否决了」与「它
        # 可行但层不够高」——分层最大的可观测性收益（spec §6）。
        layer = layer_map.get(key)
        record_cands.append((cand.task, score, terms, ok, veto,
                             key == chosen_key, layer,
                             layer is not None and layer == top_layer))

    record = _record(record_cands, plate_match, cooker_match)
    record["top_layer"] = top_layer
    record["layers"] = {str(k): v for k, v in layer_map.items()}
    return chosen_task, record
```

- [ ] **Step 7: 运行全量测试**

Run: `python3 -m pytest tests/ -q`
Expected: **987 passed, 78 skipped** —— 与基线一字不差。

若有失败，逐个看失败原因并分类：
- 断言的是**具体分数值**且该项不在所选层白名单里 → 这是分层的预期效果，**但不要改测试**。记下来，在 Step 10 的报告里列出，交给人判断。
- 断言的是**层无关的性质**（候选生成、可行性、匹配）却失败了 → 是 bug，修实现。
- `TypeError: _score_candidate() takes N positional arguments` → `layer` 参数位置放错了，必须在**最后**。

- [ ] **Step 8: 测试当前承诺落在低层的行为（Review Focus 5）**

Append to `tests/test_layered_selection.py`:

```python
def test_commitment_in_lower_layer_goes_through_infeasible_grace():
    """spec §8.2 声称「白捡一个防抖」，但从未被测过。

    当前承诺在 L6，这一拍出现 L5 候选 → 承诺不在 in_layer → 不在 scored
    → _apply_commitment_inertia 的 ③ 把它当「不在可行候选中」，走
    infeasible_streak 的 3 拍宽限再改选，而不是立即切。
    """
    from ai.blackboard import Blackboard, Commitment
    from ai import tasks as T

    prep = T.Chop("order1", ("board1",), "Cucumber")
    fill = T.FillCooker(None, ("cooker1", ("SushiRice",)), "BoiledSushiRice")

    bb = Blackboard()
    bb.tick = 100
    bb.commitment = Commitment(prep, 90, 0, 0, 0)

    # 只有 L5 候选进了 scored（承诺的 L6 被分层挡在外面）
    scored = [(fill, 4.8, {"cooker_idle": 4.8})]

    # 前两拍保持承诺
    assert planner._apply_commitment_inertia(scored, None, bb) == prep
    assert bb.commitment.infeasible_streak == 1
    assert planner._apply_commitment_inertia(scored, None, bb) == prep
    assert bb.commitment.infeasible_streak == 2
    # 第三拍改选
    assert planner._apply_commitment_inertia(scored, None, bb) == fill
    assert bb.commitment.infeasible_streak == 0
```

- [ ] **Step 9: 运行确认通过**

Run: `python3 -m pytest tests/test_layered_selection.py -v`
Expected: 2 passed

若 `_apply_commitment_inertia(scored, None, bb)` 因 `world=None` 报错（它内部调 `_time_pressure(top_task, world)`），改为传一个最小 world 替身：`type("W", (), {"cookers": [], "plates": [], "orders": []})()`。

- [ ] **Step 10: 跑全量并报告**

Run: `python3 -m pytest tests/ -q 2>&1 | tail -20`

在提交信息里写明实际数字。**如果不是 987 passed，先停下来把差异列给人看**，不要改测试凑数——「零测试改动」是分层正确性的核心证据。

- [ ] **Step 11: 提交**

```bash
git add ai/planner.py tests/test_layered_selection.py
git commit -m "$(cat <<'EOF'
feat(分层): 接进 plan()——只有最高层候选进入打分

plan() 在可行性过滤与打分之间插一步 layer_filter，_score_candidate /
_score_tasks 加末位可选参数 layer。layer 默认 None 时行为与分层前完全
一致（全项求和）——这是「零测试改动」的实现手段，28 处既有调用不受影响。

承诺惯性**一行不改**：分层管「挑哪个」，抢占仍由 COMMITMENT_MARGIN 与
pressure>=1.0 跳过惯性负责。当前承诺若落在低层，它不在 scored 里，走
现成的 infeasible_streak 3 拍宽限再改选——白捡一个防抖，
test_commitment_in_lower_layer_goes_through_infeasible_grace 钉住它
（spec §8.2 声称有这个性质但一直没测）。

decision_record 新增 top_layer / layers 两键，候选元组尾部加 layer /
in_top_layer：trace 据此区分「被否决」与「可行但层不够高」。

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: trace 记录层信息

**Files:**
- Modify: `ai/trace.py:193-225`（`_format_candidates`）
- Test: `tests/test_trace_layers.py`（新建）

**Interfaces:**
- Consumes: Task 5 产出的候选元组 `(task, score, terms, ok, veto, chosen, layer, in_top_layer)`
- Produces: trace JSONL 的每个候选 dict 新增两键 `"layer"`（`int` 或 `None`）、`"in_top_layer"`（`bool`）；顶层新增 `"top_layer"`

`_format_candidates` 现已做变长元组兼容（`len(cand) >= 6` / `>= 3` 两条分支），所以往后追加字段不破坏既有调用。

- [ ] **Step 1: 写失败测试**

Create `tests/test_trace_layers.py`:

```python
# -*- coding: utf-8 -*-
"""trace 的层信息（spec §6）。

「为什么没选救锅」要能一眼区分两种原因：被否决（ok=False + veto）还是
可行但层不够高（ok=True + in_top_layer=False）。这是分层最大的可观测性
收益，所以单独钉住。
"""
from ai import trace


class _Task(object):
    def __init__(self, name):
        # type: (str) -> None
        self._name = name

    def __repr__(self):
        return self._name


def test_candidate_carries_layer_and_in_top_layer():
    cands = [
        (_Task("FillCooker"), 4.8, {"cooker_idle": 4.8}, True, "", True, 5, True),
        (_Task("Chop"), 0.0, {"urgency": 3.0}, True, "", False, 6, False),
    ]
    out = trace._format_candidates(cands)
    assert out[0]["layer"] == 5
    assert out[0]["in_top_layer"] is True
    assert out[1]["layer"] == 6
    assert out[1]["in_top_layer"] is False
    # 「可行但层不够高」：ok 为真、veto 为空、in_top_layer 为假
    assert out[1]["ok"] is True and out[1]["veto"] == ""
```

- [ ] **Step 2: 运行确认失败**

Run: `python3 -m pytest tests/test_trace_layers.py -v`
Expected: FAIL with `KeyError: 'layer'`

- [ ] **Step 3: 改 `_format_candidates`**

Modify `ai/trace.py:193-225`：

```python
def _format_candidates(candidates):
    # type: (List) -> List[Dict]
    """格式化候选列表。

    Args:
        candidates: List of (task, score, terms, ok, veto, chosen[, layer,
            in_top_layer]) tuples

    Returns:
        List of formatted candidate dicts
    """
    formatted = []
    for cand in candidates:
        layer, in_top_layer = None, False
        # 解构候选元组。分层后尾部多两项（spec §6）；旧的 6 元组与 3 元组
        # 仍要能格式化——测试与 plan_firefighting 都还在用短元组。
        if len(cand) >= 8:
            task, score, terms, ok, veto, chosen, layer, in_top_layer = cand[:8]
        elif len(cand) >= 6:
            task, score, terms, ok, veto, chosen = cand[:6]
        elif len(cand) >= 3:
            # 兼容不完整的候选格式（测试场景）
            task, score, terms = cand[:3]
            ok, veto, chosen = True, None, False
        else:
            continue

        formatted.append({
            "task": repr(task),  # 使用 __repr__，如 "Chop(Tomato, board_01, order#order1)"
            "score": round(float(score), 2),
            "ok": ok,
            "veto": veto if veto else "",
            "terms": {k: round(float(v), 2) for k, v in terms.items()} if terms else {},
            "chosen": chosen,
            # 「为什么没选它」：ok=False 是被否决，ok=True + in_top_layer=False
            # 是「可行但层不够高」。两者在旧 trace 里无法区分。
            "layer": layer,
            "in_top_layer": bool(in_top_layer),
        })
    return formatted
```

注意：原实现里 `"chosen": chosen` 后面可能没有逗号（它是最后一项），加字段时要补上。

- [ ] **Step 4: 运行确认通过**

Run: `python3 -m pytest tests/test_trace_layers.py -v`
Expected: 1 passed

- [ ] **Step 5: 补向后兼容测试**

Append to `tests/test_trace_layers.py`:

```python
def test_six_tuple_still_formats():
    """plan_firefighting 仍发 6 元组（它不分层，只有一个全局候选）。"""
    cands = [(_Task("Extinguish"), 0.0, {}, True, "", True)]
    out = trace._format_candidates(cands)
    assert out[0]["layer"] is None
    assert out[0]["in_top_layer"] is False
    assert out[0]["chosen"] is True


def test_three_tuple_still_formats():
    """旧测试夹具用的短元组。"""
    out = trace._format_candidates([(_Task("Chop"), 1.5, {"eta": -1.0})])
    assert out[0]["ok"] is True
    assert out[0]["layer"] is None


def test_top_layer_goes_into_log_entry(tmp_path, monkeypatch):
    """顶层也要记 top_layer：复盘时先看这一拍在哪层，再看候选。"""
    import json

    monkeypatch.setattr(trace, "TRACE_PATH", str(tmp_path / "t.jsonl"))
    record = {
        "mode_reason": "default",
        "p1": {},
        "candidates": [(_Task("FillCooker"), 4.8, {}, True, "", True, 5, True)],
        "top_layer": 5,
    }
    trace.emit(record, "Normal", None, {"status": "running"}, [], 100, None)
    line = json.loads(open(str(tmp_path / "t.jsonl")).read().strip())
    assert line["top_layer"] == 5
```

- [ ] **Step 6: 运行确认失败（`top_layer` 还没进 log_entry）**

Run: `python3 -m pytest tests/test_trace_layers.py -v`
Expected: 3 passed, 1 failed（`test_top_layer_goes_into_log_entry` 报 `KeyError: 'top_layer'`）

- [ ] **Step 7: 把 `top_layer` 放进 log_entry**

Modify `ai/trace.py` 的 `emit`，在 `"candidates": _format_candidates(...)` 那一行之后加：

```python
        # 这一拍选在哪层。复盘先看它，再看候选明细（spec §6）。
        "top_layer": record.get("top_layer"),
```

- [ ] **Step 8: 运行确认通过**

Run: `python3 -m pytest tests/test_trace_layers.py -v`
Expected: 4 passed

- [ ] **Step 9: 跑全量测试**

Run: `python3 -m pytest tests/ -q 2>&1 | tail -5`
Expected: 991 passed, 78 skipped（基线 987 + 本任务新增 4）

`tests/test_trace.py` 若因 log_entry 多了一个键而失败，看它断言的是什么：若断言 `set(line.keys()) == {...}` 这类全集相等，那是需要更新的——这是**新增字段的必然结果**，不属于「改测试凑数」，可以改，但要在提交信息里说明。

- [ ] **Step 10: 提交**

```bash
git add ai/trace.py tests/test_trace_layers.py
git commit -m "$(cat <<'EOF'
feat(分层): trace 记录 layer / in_top_layer / top_layer

「为什么没选救锅」第一次能一眼区分两种原因：被否决（ok=False + veto）
还是可行但层不够高（ok=True + in_top_layer=False）。旧 trace 里这两者
长得一样。

_format_candidates 的变长元组兼容往后扩一档（8 元组），6 元组与 3 元组
仍能格式化——plan_firefighting 不分层、只发一个全局候选，仍用 6 元组。

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: 钉住 spec §4.3 的两处边界与 §10.2 的回归

**Files:**
- Test: `tests/test_layered_selection.py`（追加）

**Interfaces:**
- Consumes: Task 1~4 的全部产出
- Produces: 无新代码，只有测试。这些测试是整个改造的**价值证明**——它们断言的是分层解决了什么。

这些测试用 `layer_of_task` 的真实曲线，不用静态替身：要证明的是「真实局面下层序是对的」，替身会把被测的东西替掉。

- [ ] **Step 1: 钉住 §10.2 的回归 case（灶空转时该填灶）**

Append to `tests/test_layered_selection.py`:

```python
def test_fill_cooker_outranks_prep_structurally():
    """AGENTS.md §10.2 记的那次静默回退：urgency 改读 slack 后，备菜候选
    在中段能拿到 3~4 分，把 W_COOKER_IDLE=4.0 的填灶顶了下去——第 1 组在
    真实帧上验证过的七个「灶空转该填灶」的拍全部回退，而 982 个测试一个
    都没报警。当时的修法是把 4.0 提到 4.8。

    分层后这件事不再靠数字：FillCooker 在 L5、备菜在 L6，**备菜压根不进
    打分**。所以这条断言的是层，不是分数——数字怎么调都不会再翻车。
    """
    from ai import tasks as T

    fill = T.FillCooker(None, ("cooker1", ("SushiRice",)), "BoiledSushiRice")
    prep = T.FetchToPlate("order1", ("plate1", (3, 4)), "Seaweed")
    w = _world()
    assert PL.layer_of_task(fill, w) < PL.layer_of_task(prep, w)


def _world(cookers=(), plates=(), orders=()):
    # type: (Any, Any, Any) -> Any
    """最小 world 替身。层判定只读 cookers / plates。"""
    return type("W", (), {
        "cookers": list(cookers),
        "plates": list(plates),
        "orders": list(orders),
        "stations": [],
        "items": [],
        "chefs": [],
        "level": "Sushi_1_4_2P",
    })()
```

把 `_world` 定义移到文件中靠前的位置（在第一次使用之前），并在文件顶部 import 区补 `from typing import Any`。

- [ ] **Step 2: 运行确认通过**

Run: `python3 -m pytest tests/test_layered_selection.py -v`
Expected: 3 passed

- [ ] **Step 3: 钉住 §4.3 的「兑现 > 产能」**

Append to `tests/test_layered_selection.py`:

```python
def test_redeem_outranks_capacity():
    """spec §4.3：料齐的盘先送出去，别等填灶。

    依据是实测分布——4577 拍里「料齐的盘」0 盘占 4279 拍、1 盘占 298 拍，
    从没有两盘同时料齐。所以 L3 几乎永远至多一个候选，而 DeliverPlate
    实测 50 拍：它堵不住填灶，而 60 分 base 立即到账。

    反向排法的风险：开局三口空灶 + 5 单要米时 L5 堵着三个 FillCooker
    （每个实测 120 拍），料齐的盘要等 360 拍。
    """
    from ai import tasks as T

    deliver = T.DeliverPlate("order1", ("plate1",))
    fill = T.FillCooker(None, ("cooker1", ("SushiRice",)), "BoiledSushiRice")
    w = _world()
    assert PL.layer_of_task(deliver, w) < PL.layer_of_task(fill, w)


def test_rescue_outranks_unblock():
    """spec §4.3：救料 > 解卡。

    锅上时钟在走（TakeCookedToPlate 实测 125 拍）；洗盘 150 拍、产出的是
    「下一单的盘」，没有任何东西在倒计时。先洗盘再救料会让那锅多烧 150
    拍，而 ratio 从 0.5 到 1.0 只有 240 拍。

    注意**不要**误用 W_NO_PLATE_PLACED 的定标数据反驳这条：那组数据
    （W=5.0 时救锅保住 0 拍）测的是「摆盘压过 progress=23/24 的快糊锅」，
    对应现在的 L1——而 L1 > L4 本条已经保证。
    """
    from ai import tasks as T

    rescue = T.TakeCookedToPlate("order1", ("cooker1", "plate1"),
                                 "BoiledSushiRice")
    wash = T.WashPlate(resources=("sink1",))
    # 锅刚熟（ratio 0.5）+ 一块干净盘都没有（plate_pressure = 2.0）
    w = _world(cookers=[{"n": "cooker1", "cooking_time": 12.0,
                         "cooking_progress": 12.0, "cooked": True}],
               plates=[])
    assert PL.layer_of_task(rescue, w) == PL.L_RESCUE
    assert PL.layer_of_task(wash, w) == PL.L_UNBLOCK
    assert PL.layer_of_task(rescue, w) < PL.layer_of_task(wash, w)


def test_irreversible_outranks_unblock():
    """但快糊的锅仍压过补盘——这正是 W_NO_PLATE_PLACED 定标时测出来的：
    W=5.0 让摆盘吃满 8.5 分、压过 progress=23/24 的 RescueCooked(8.08)，
    等于用「盘没摆」换「锅烧糊」。分层后这条由 L1 > L4 结构性保证。
    """
    from ai import tasks as T

    rescue = T.RescueCooked(resources=("cooker1",))
    place = T.PlaceCleanPlate("order1", ((3, 4),))
    w = _world(cookers=[{"n": "cooker1", "cooking_time": 12.0,
                         "cooking_progress": 23.0}],
               plates=[])
    assert PL.layer_of_task(rescue, w) == PL.L_IRREVERSIBLE
    assert PL.layer_of_task(place, w) == PL.L_UNBLOCK
    assert PL.layer_of_task(rescue, w) < PL.layer_of_task(place, w)
```

- [ ] **Step 4: 运行确认通过**

Run: `python3 -m pytest tests/test_layered_selection.py -v`
Expected: 6 passed

若某个 task 的构造签名不对（如 `T.DeliverPlate` 的 resources 元数），用 `python3 -c "from ai import tasks as T; help(T.DeliverPlate)"` 或读 `ai/tasks/assembly.py` 查实际签名后改夹具。**不要改被测代码来迁就测试**。

- [ ] **Step 5: 钉住「盘的三档不会压过锅」这组完整序关系**

Append to `tests/test_layered_selection.py`:

```python
def test_full_layer_order_on_one_world():
    """一个局面里把八层的代表候选全摆出来，断言层序完整。

    这是分层的总验收：原来这组关系要靠 10.0 > 5.0 > 4.8 > 3.0 > 2.0 这串
    数字维持，而每个数字都在注释里解释自己「必须压过谁、不能压过谁」。
    现在它是一条可读的序。
    """
    from ai import tasks as T

    w = _world(cookers=[{"n": "cooker1", "cooking_time": 12.0,
                         "cooking_progress": 23.0}],   # 快糊
               plates=[])                              # 0 干净盘
    pairs = [
        (PL.L_FIRE,          T.Extinguish()),
        (PL.L_IRREVERSIBLE,  T.Discard(resources=("cooker1",))),
        (PL.L_REDEEM,        T.DeliverPlate("order1", ("plate1",))),
        (PL.L_UNBLOCK,       T.WashPlate(resources=("sink1",))),
        (PL.L_CAPACITY,      T.FillCooker(None, ("cooker2", ("SushiRice",)),
                                          "BoiledSushiRice")),
        (PL.L_PREP,          T.Chop("order1", ("board1",), "Cucumber")),
        (PL.L_CLEANUP,       T.StashHeld()),
    ]
    for expected, task in pairs:
        assert PL.layer_of_task(task, w) == expected, repr(task)
    # 序本身严格递增
    observed = [PL.layer_of_task(t, w) for _, t in pairs]
    assert observed == sorted(observed)
    assert len(set(observed)) == len(observed)
```

- [ ] **Step 6: 运行确认通过**

Run: `python3 -m pytest tests/test_layered_selection.py -v`
Expected: 7 passed

`T.Discard(resources=("cooker1",))` 的层取决于 `_time_pressure`——它在 `("TakeCookedToPlate", "RescueCooked", "Discard")` 名单里，progress 23/12 → ratio 0.96 → pressure ≈ 1.92 → L1。若实际落在别处，先用 `python3 -c "..."` 打印 `planner._time_pressure` 的返回值确认曲线，再决定是夹具错了还是白名单错了。

- [ ] **Step 7: 跑全量并提交**

Run: `python3 -m pytest tests/ -q 2>&1 | tail -5`
Expected: 997 passed, 78 skipped（991 + 本任务 6）

```bash
git add tests/test_layered_selection.py
git commit -m "$(cat <<'EOF'
test(分层): 钉住两处边界与 §10.2 的回归

这些测试是整个改造的价值证明——断言的是层，不是分数。

test_fill_cooker_outranks_prep_structurally 对应 AGENTS.md §10.2 那次
静默回退（urgency 改读 slack 后备菜顶翻填灶、七个真实帧验证过的拍全部
回退、982 个测试一个都没报警）。分层后备菜压根不进打分，数字怎么调都
不会再翻车。

另两条对应 spec §4.3 定过的边界：兑现 > 产能（依据是「料齐的盘」实测
分布，L3 几乎永远至多一个候选、堵不住填灶）、救料 > 解卡（锅上时钟在
走，洗盘产出的是下一单的盘、没有倒计时）。同时钉住 L1 > L4——快糊的锅
仍压过补盘，这正是 W_NO_PLATE_PLACED 定标时测出来的。

test_full_layer_order_on_one_world 在一个局面里摆出八层代表候选，断言
完整序。原来这组关系靠 10.0>5.0>4.8>3.0>2.0 这串数字维持，每个数字都
在注释里解释自己「必须压过谁」。

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 8: 权重注释更新与离线验证

**Files:**
- Modify: `ai/planner.py:484-617`（权重常量区的注释）
- Modify: `AGENTS.md`（§4 架构流程图、§10 当前状态）

**Interfaces:**
- Consumes: Task 5 落地后的实际行为
- Produces: 无新代码。文档与注释对齐，加上离线验证的记录。

- [ ] **Step 1: 给因分层而改变角色的权重加注释**

按 spec §7.1 的四类处置，在 `ai/planner.py` 的权重区逐个加一行注释。不改任何数值。

`W_DELIVER_BONUS`（560 行附近）加：

```python
# 【分层后】已退化为恒等项：L3 全部候选都是 DeliverPlate，给每个人加同一个
# 常数不改变排序，故不在任何层白名单里（见 planner_layers.TERM_WHITELIST）。
# 常量保留是因为三个测试文件引用它算量级天花板。它原本同时在调两件事
# （类别优先级 + 改选频率），现在前者归层序、后者归 COMMITMENT_MARGIN。
```

`W_PRESSURE`（490 行附近）加：

```python
# 【分层后】仅 L1/L2 层内有效，且两层各只有这一项 → 当前为恒等缩放。
# 保留是因为它同时是 _time_pressure 的量纲声明，删掉那条曲线会失去标尺。
# 注意层**阈值**用的是曲线原始值（1.5 / 1.0），不乘本权重。
```

`W_COOKER_IDLE`（547 行附近）在现有注释末尾加：

```python
# 【分层后】4.8 这个值的来历（「必须压过备菜的实际合计 2.7~3.3」）随分层
# 消失——填灶在 L5、备菜在 L6，不再同层比较。它现在只需在 L5 内部与
# W_ETA 可比，表达「缺口大的先填」。
```

`COMBO_PREP_DISCOUNT`（527 行附近）在现有注释末尾加：

```python
# 【分层后】原始理由（「0.5 时 FetchToPlate 以 2.98 压过 FillCooker 的
# 2.50」）消失：两者现在分属 L6 / L5。但它在 L6 内部仍有用——区分订单
# 先后时别盖过 completion_ratio，故保留。
```

`COOKER_SHORTFALL_SATURATION`（550 行附近）在现有注释末尾加：

```python
# 【分层后】原始理由（「不能无限加分，否则 5 单全要米时填灶盖过送餐和
# 灭火」）不再需要：填灶在 L5、送餐 L3、灭火 L0，层序已经保证。它现在
# 只影响 L5 层内排序（缺 1 份 vs 缺 5 份）。
```

`W_DEADLINE_LOOKAHEAD`（532 行）加：

```python
# 【注意】这个常量从未被引用：_score_candidate ⑩ 直接用
# -_deadline_forecast(...)，而后者返回 0~1，所以这一项的实际量级是 1.0
# 而不是 10.0。属于 spec §10 列的范围外 bug，待独立修。
```

- [ ] **Step 2: 跑全量确认注释没碰坏代码**

Run: `python3 -m pytest tests/ -q 2>&1 | tail -3`
Expected: 997 passed, 78 skipped

- [ ] **Step 3: 离线验证——倒挂率仍为 0%**

Run: `python3 tools/replay_urgency.py 2>&1 | tail -20`

若该工具需要 trace 文件参数，先 `ls trace-*.jsonl` 看有没有现成的；没有则跳过本步并在报告里注明「无 trace 可重放，待实机」。

Expected: 倒挂率 0%（这是 `53b6a2b` 修完 slack 解耦后的基线，分层不该破坏它）。

- [ ] **Step 4: 离线验证——重放一局真实 trace，比对选择分布**

Run: `ls tests/fixtures/*.ndjson.gz`

若有真实帧 fixture，用 `python3 tools/tree_viz.py <帧文件>` 做离线冒烟（建全部 task 树、走分支、不按键），确认分层没让任何 task 树构建失败。

若 `tests/fixtures/` 为空（AGENTS.md §7 记着「旧帧已于 2026-10-08 删除，待 record_frames 重录」），在报告里注明：**本轮只有单测验证，真实帧回归待重录**。

- [ ] **Step 5: 更新 AGENTS.md §4 的架构流程**

把 §4 里 Planner 那一段的描述改为：

```
- **Planner**（`planner.py`）：纯函数步进器。`select_task(world, bb, order, book)` 走：
  生成候选（枚举所有订单，Plan 4 多订单模式）→ 可行性过滤（`claims_available` 前置 +
  `_is_abandoned` 拉黑检查 + 各 task 的 `feasible()`）→ **分层过滤（取最高层，见
  `planner_layers.py`）** → 打分（该层白名单内的项）→ 承诺惯性（防抖 + 穿插）。
  **无状态每拍重算**，执行顺序从可行性涌现，不建 DAG、不拓扑排序。层是派生值：
  高层动作不可达时该候选自然从 feasible 消失、层自己掉下来。
```

- [ ] **Step 6: 更新 AGENTS.md §10 当前状态**

在 §10 开头的「分支 / 测试数」那句之后，加一段：

```
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
```

同时把 §10.2「改权重前必读」的开头改为：

```
**跨层权重不会再互相抵消（分层后），但层内仍会。** 原先的实例：urgency 从
`W_URGENCY/remaining` 改成读 slack 后，另一组验证过的七个「灶空转该填灶」的拍
全部回退，测试一个都没报警。那组回退现在由 L5 > L6 结构性排除
（`tests/test_layered_selection.py::test_fill_cooker_outranks_prep_structurally`
钉住）。动层**内**的 `W_*` 时，下面这套流程仍然适用。
```

- [ ] **Step 7: 跑全量并提交**

Run: `python3 -m pytest tests/ -q 2>&1 | tail -3`
Expected: 997 passed, 78 skipped

```bash
git add ai/planner.py AGENTS.md
git commit -m "$(cat <<'EOF'
docs(分层): 权重注释标注分层后的角色变化，同步 AGENTS.md

按 spec §7.1 的四类处置逐个加注释，不改任何数值：
- W_DELIVER_BONUS / W_PRESSURE 退化为恒等项（前者不进白名单，后者是
  L1/L2 的唯一项），但常量保留——测试文件引用它们算量级天花板；
- W_COOKER_IDLE 4.8 的来历（压过备菜合计）随分层消失，现在只需在 L5
  内与 W_ETA 可比；
- COMBO_PREP_DISCOUNT / COOKER_SHORTFALL_SATURATION 的原始理由消失但
  有第二作用（层内排序），保留；
- W_DEADLINE_LOOKAHEAD 标注「从未被引用」，属范围外 bug。

AGENTS.md §4 的 Planner 流程加入分层那一步，§10 记本轮改动，§10.2 的
开头改成「跨层不会再抵消，层内仍会」。

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## 完成标准

全部任务做完后：

1. `python3 -m pytest tests/ -q` → **997 passed, 78 skipped**（基线 987 + 新增 10）
2. `tests/` 下**没有任何既有测试被修改**（`git diff main --stat tests/` 只应显示新增文件 + `test_trace.py` 的键集合更新，若有）
3. `tools/replay_urgency.py` 的倒挂率 0%
4. 实机验证**未完成**，这是已知状态——按 AGENTS.md §10 的惯例，离线验证只回答「同样世界状态下会选什么」，不模拟物理执行

若第 2 条不成立（某个既有测试必须改才能通过），**停下来把 diff 列出来给人看**。那意味着分层改变了一处既有行为，需要判断是改进还是回退——这个判断不该由执行者单独做。
