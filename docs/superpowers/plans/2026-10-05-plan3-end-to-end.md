# Plan 3：端到端闭环实施计划

> **状态**: ✅ 已完成 (2026-10-06)  
> **测试**: 66/66 通过 (100%)  
> **提交**: 74362e1 → 2095e0d (11 commits)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付首次端到端闭环——从游戏状态快照到按键输出的完整路径。实现 `arbiter.py`（模式仲裁）、`planner.py`（单订单规划）、新 `agent.py`（主循环）、`trace.py`（决策日志），使 AI 能在实机**完整做出并送出至少一道正确菜**，无卡键、无模式死锁、trace 可回放。

**Architecture:** Arbiter → Planner → Executor 三层分离 + Blackboard 作为唯一可变状态容器。Plan 3 实现**单订单模式**（一次只追一单，送完再选下一单），不做多订单并行；暂不实现 P1 协同推断（Plan 4）。主循环每拍执行 13 步装配（spec §8.1），Arbiter 选模式（三行表 + 卡住前置守卫），Planner 在 Normal 模式下生成候选 → 过滤 → 打分 → 选 task（单订单简化：候选生成只看当前追踪的一个订单），Executor tick task 树产出按键。

**Tech Stack:** Python 3.9.6、pytest。依赖 Plan 1 的 `blackboard` / `reconcile` / `navigation` / `predicates`、Plan 1.5 的 `world` / `occupancy` / `stations`、Plan 2 的 `primitives` / `tasks` / `hands` / `recipes` / `placement`。

**Spec:** `docs/superpowers/specs/2026-10-04-ai-behavior-architecture-design.md`（§3 架构总览、§5 Arbiter、§6 Planner、§7 Executor、§8.1 主循环、§8.3 决策日志、§11.2 阶段 4、§11.2.1 Plan 3 映射、§11.3 场景单测、§11.5 完成标准）

## Global Constraints

- **Python 3.9.6**：类型标注用注释形式 `# type: (...) -> ...`，从 `typing` 导入泛型。
- **不改 `oc/`**：旧实现是行为对照组，只读参考。
- **不改 C# 插件**：帧格式按现状适配。
- **单订单模式**：Plan 3 的 Planner 一次只追一个订单（`current_order_id`），送完或过期后选下一单。多订单并行（穿插、多盘多灶认领）归 Plan 4。
- **暂不实现 P1 协同**：`p1_model.py` 归 Plan 4。本计划的协作过滤（spec §6.3）为空壳——接力 / 分单模式的开关存在，但 P1 推断为 `None`，协作过滤直接透传候选。
- **打分权重最小集**：Plan 3 只启用三项（spec §6.4）——订单紧迫（`remaining`）、时间压力（`cooked → burning`）、ETA。其余权重归 Plan 4 逐项加入。
- **注释用中文**，与 `ai/` 既有模块一致。
- **测试命令**：`cd /Users/dongyi/Documents/coding/oc2 && python3 -m pytest <path> -v`
- **分支**：本计划在 `plan3-end-to-end` 分支上做，不直接提交 main。

## Review Focus

Plan 3 是首次替换运行时行为，风险最高的七类输入：

1. **主循环的 13 步顺序**（spec §8.1）。步骤 ⑨"写回 claims"和 ⑩"登记 commitment"是 spec 初版遗漏后补的——不写回则认领与承诺永远为空，§4 / §6.5 全部失效。Task 8 Step 2 有断言。

2. **模式转移时的清理**（spec §5.4）。转移时必须 halt 并丢弃树、丢弃上一模式的 scratch。`Stuck` 是跨模式前置守卫（spec §5.1），不是优先级表里的一行——火中被堵时必须先脱困再继续灭火，否则死锁。Task 1 Step 4 / Task 8 Step 3 有断言。

3. **承诺惯性的两条规则**（spec §6.5）。(a) 进度项：改选门槛随 `steps_done / est_steps` 上升，否则离出餐口一步时会放下盘子跑去端锅。(b) 不可行需连续 M 拍才弃，否则 P1 手臂短暂挡住料就会丢弃承诺。Task 5 Step 5 有场景单测。

4. **认领的四个释放触发点**（spec §4.1）。task 成功、订单消失、资源消失 / 内容不符、TTL 兜底。主循环步骤 ③ 对账 + 步骤 ⑫ 处理 SUCCESS/FAILURE 时都要调用释放逻辑。遗漏会导致认领泄漏 → 资源楔死。Task 8 Step 4 卡口。

5. **单订单模式的订单选择**。当前订单送完 / 过期后，从剩余订单中按 `remaining` 升序选下一单（最紧急的）。`current_order_id` 为 None 时表示无订单可做，Planner 返回 `Idle` fallback。Task 4 Step 3 断言。

6. **Firefighting 的进入 / 退出条件**（spec §5.1）。进入："存在未被处理的着火灶 且 存在我可获得的灭火器"；退出："无未被处理的着火灶"。P1 持唯一灭火器时不进 Firefighting（Task 1 Step 5）。中断 task 连续 FAILURE 达 N 次强制退回 Normal 并拉黑目标（spec §5.1 末）。

7. **决策日志格式**（spec §8.3）。每拍一条 JSONL，含 `candidates` 数组（每个候选的 `score` / `ok` / `veto` / `terms` 拆解）、`chosen` 标记、`commit` 状态、`exec` 节点状态、`keys` 数组。`terms` 把打分拆成各项，日志能直接回答"为什么选它"。Task 7 有格式校验。

---

## 文件结构

| 文件 | 职责 | 产出任务 | 预估行数 |
|---|---|---|---|
| `ai/arbiter.py` | 模式仲裁：三行表 + 卡住前置守卫 + 转移纪律 | Task 1 | ~200 |
| `ai/executor.py` | 执行层包装：tick task 树、管理树生命周期（spec §7.6）、处理 SUCCESS/FAILURE | Task 2 | ~150 |
| `ai/planner.py` | 规划层：五步流水线（候选生成 → 可行性 → 协作 → 打分 → 承诺），单订单模式 | Task 3-6 | ~450 |
| `ai/trace.py` | 决策日志：JSONL 输出 + 格式化 | Task 7 | ~120 |
| `ai/agent.py` | 主循环：13 步装配 + 生命周期守卫，取代 `oc/ai/agent.py` | Task 8 | ~180 |
| `tests/test_arbiter.py` | Arbiter 单测：模式选择 + 转移 + 卡住守卫 | Task 1 | |
| `tests/test_executor.py` | Executor 单测：树生命周期 + SUCCESS/FAILURE 处理 | Task 2 | |
| `tests/test_planner.py` | Planner 单测：候选生成 + 打分 + 承诺惯性场景 | Task 3-6 | |
| `tests/test_trace.py` | Trace 格式校验 | Task 7 | |
| `tests/test_agent.py` | 主循环单测：13 步执行顺序 + 认领释放 | Task 8 | |
| `tests/test_end_to_end.py` | 端到端冒烟：假帧 → 主循环 → 验证按键序列，不接真实游戏 | Task 9 | |

依赖顺序：Task 1（Arbiter）→ 2（Executor）→ 3-6（Planner，分四个子任务）→ 7（Trace）→ 8（Agent 主循环）→ 9（端到端冒烟）。

---

### Task 1: Arbiter（模式仲裁层）

**Files:**
- Create: `ai/arbiter.py`
- Test: `tests/test_arbiter.py`

**Interfaces:**
- Consumes: `World`、`Blackboard`（读 `scratch`、`intents.assignments`、火情状态）
- Produces: `Mode`（枚举：`"Normal"` / `"Firefighting"` / `"Assigned"`）、mode 转移原因（日志用）

**Spec References:**
- §5.1 模式表：三行优先级表 + `Stuck` 跨模式前置守卫
- §5.2 模式判据
- §5.3 P1 持唯一灭火器时退回 Normal
- §5.4 转移纪律

实现三行模式选择表与卡住检测前置守卫。`Stuck` 不是模式表里的一行，而是跨模式生效的前置守卫——先检测卡住，成立则处理脱困（保留原模式），否则按优先级表选模式。

**Key Design Points:**

1. **`Stuck` 前置守卫**：`if stuck_detected(world, bb): → handle_stuck()，保留 bb.mode`。卡住检测复用 Plan 2 的 `primitives.stuck` 模块逻辑（`last_cell` / `same_count` 计数）。

2. **Firefighting 进入条件**："存在未被处理的着火灶 且 存在我可获得的灭火器"。未被处理 = P1 不在处理该灶（P1 不持该灶上方的灭火器、P1 不在该灶旁）。

3. **中断 task 失败降级**：非 Normal 模式下，task 连续 FAILURE 达 `INTERRUPT_FAILURE_THRESHOLD`（默认 3）次 → 强制退回 Normal 并拉黑目标（cooker_id / extinguisher_id）一段时间（`BLACKLIST_TICKS`，默认 100 拍 = 5 秒）。

4. **模式粘性**：退出条件真的满足才退，不因单帧抖动跳回。例：Firefighting 要 `onFire` 确实为 false 才退出。

5. **转移时清理**（spec §5.4）：模式变化时，halt 并丢弃当前 task 树（由 Executor 处理），丢弃上一模式的 `scratch` 对象（Arbiter 负责清空 `bb.scratch`）。

- [ ] **Step 1: 写失败测试**

创建 `tests/test_arbiter.py`：

```python
"""Arbiter 模式仲裁单测。

重点验证：
  1. Stuck 是前置守卫，先于模式表生效（火中被堵 → 先脱困）。
  2. Firefighting 进入 / 退出条件（资源视角，P1 持唯一灭火器时不进）。
  3. 模式转移时清空 scratch。
  4. 中断 task 连续失败降级。
"""
import pytest
from ai import arbiter
from ai.blackboard import Blackboard, Mode
# ... 导入 World / make_bb 等

def test_stuck_guard_takes_precedence_over_firefighting():
    """火中被堵：Stuck 守卫先生效，不死锁在 Firefighting。"""
    # 构造：cooker onFire、AI 在原地连续 3 拍、灭火器可得
    # 断言：检测到卡住，进入 Stuck 处理（bb.scratch 是 StuckScratch），mode 不变
    pass

def test_firefighting_entry_with_available_extinguisher():
    """有未被处理的着火灶且灭火器可得 → 进入 Firefighting。"""
    pass

def test_firefighting_not_entered_when_p1_holds_only_extinguisher():
    """P1 持唯一灭火器 → 不进 Firefighting，留在 Normal。"""
    pass

def test_firefighting_exit_when_all_fires_handled():
    """所有火灭了 → 退出 Firefighting 回 Normal。"""
    pass

def test_mode_transition_clears_scratch():
    """模式转移时清空 bb.scratch。"""
    pass

def test_interrupt_task_failure_degrades_to_normal():
    """Firefighting 模式下 task 连续失败 3 次 → 退回 Normal 并拉黑目标。"""
    pass
```

运行 `pytest tests/test_arbiter.py -v`，全红。

- [ ] **Step 2: 实现 `ai/arbiter.py` 骨架**

```python
# -*- coding: utf-8 -*-
"""模式仲裁层（Arbiter）。

职责：
  每拍第一个跑，决定把控制权交给哪个模式。不执行任何动作。

与 oc/ai/ 的差异：
  - 旧实现：模式切换逻辑混在 agent.py 和 _build_tree() 的闭包副作用里。
  - 新实现：独立的纯函数（world, bb）→ mode，三行模式表 + Stuck 跨模式前置守卫。

Spec: §5 Arbiter
"""
from typing import Tuple, Optional
from ai.blackboard import Blackboard, Mode

# 常量
INTERRUPT_FAILURE_THRESHOLD = 3  # 中断 task 连续失败 N 次强制退回 Normal
BLACKLIST_TICKS = 100  # 拉黑目标的时长（拍数）
STUCK_SAME_CELL_THRESHOLD = 3  # 连续多少拍在同一格算卡住

def select_mode(world, bb):
    # type: (World, Blackboard) -> Tuple[Mode, str]
    """选择当前拍的模式。
    
    返回: (mode, reason)
    reason 用于日志，例 "cooker#2 onFire" / "no fire"
    """
    # ① Stuck 前置守卫（跨模式生效）
    if _is_stuck(world, bb):
        return _handle_stuck(world, bb)
    
    # ② 按优先级表选模式
    # 优先级 1: Firefighting
    fire_reason = _should_enter_firefighting(world, bb)
    if fire_reason:
        return Mode.FIREFIGHTING, fire_reason
    
    # 优先级 2: Assigned（对话层指派，本计划为空壳）
    if bb.intents and bb.intents.assignments:
        return Mode.ASSIGNED, "user assignment"
    
    # 优先级 3: Normal（默认）
    return Mode.NORMAL, "default"

def _is_stuck(world, bb):
    # type: (World, Blackboard) -> bool
    """检测 AI 是否卡住（连续多拍在同一格未推进）。"""
    # 复用 bb.scratch.StuckScratch 的 last_cell / same_count
    pass

def _handle_stuck(world, bb):
    # type: (World, Blackboard) -> Tuple[Mode, str]
    """处理卡住：保留原模式，由 Executor 执行脱困原语。"""
    pass

def _should_enter_firefighting(world, bb):
    # type: (World, Blackboard) -> Optional[str]
    """检查是否应进入 Firefighting。
    
    返回 None（不进）或进入原因字符串（如 "cooker#2 onFire"）。
    """
    pass

def on_mode_transition(old_mode, new_mode, bb):
    # type: (Mode, Mode, Blackboard) -> None
    """模式转移时的清理：清空 scratch。
    
    注：halt 树由 Executor 负责，此处只清 scratch。
    """
    if old_mode != new_mode:
        bb.scratch = None
```

- [ ] **Step 3: 实现 Stuck 前置守卫**

实现 `_is_stuck` 和 `_handle_stuck`。卡住检测逻辑：读取 AI 厨师当前格子，与 `bb.scratch.last_cell` 比较，若连续 `STUCK_SAME_CELL_THRESHOLD` 拍在同一格则判定卡住。

- [ ] **Step 4: 实现 Firefighting 进入 / 退出条件**

实现 `_should_enter_firefighting`：

1. 枚举 `world.cookers`，找出 `onFire == true` 的灶。
2. 对每个着火灶，检查是否"未被处理"：
   - P1 不在该灶旁（`distance(p1_chef.pos, cooker.pos) > HANDLING_DISTANCE`，默认 2.0）
   - P1 不持灭火器
3. 检查 AI 能否获得灭火器：`world.items` 中有 `t == "extinguisher"` 且 `held == null` 或 `held == "Player 2"`。
4. 若存在未被处理的着火灶且灭火器可得 → 返回原因字符串，否则返回 None。

- [ ] **Step 5: 实现中断 task 失败降级**

在 `bb.commitment` 中加 `interrupt_failure_count` 字段。Arbiter 读取该字段，若非 Normal 模式且 `interrupt_failure_count >= INTERRUPT_FAILURE_THRESHOLD`，强制退回 Normal 并拉黑当前目标（写入 `bb.blacklisted_resources`，格式 `{resource_id: until_tick}`）。

- [ ] **Step 6: 跑通测试**

运行 `pytest tests/test_arbiter.py -v`，全绿。确认：
- Stuck 守卫先于 Firefighting 生效
- P1 持唯一灭火器时不进 Firefighting
- 模式转移清空 scratch
- 中断失败降级生效

---

### Task 2: Executor（执行层包装）

**Files:**
- Create: `ai/executor.py`
- Test: `tests/test_executor.py`

**Interfaces:**
- Consumes: `Task`（来自 Planner）、`Context`（world / bb / hands / ...）
- Produces: 按键输出（通过 `ctx["hands"]`）、`Status`（SUCCESS / FAILURE / RUNNING）

**Spec References:**
- §7.6 树的生命周期：task 身份相同则复用树，变化则 halt 并丢弃
- §7.1-7.3 原语 + task 树（已由 Plan 2 交付）

Executor 是 task 树的生命周期管理器：持有 `active_tree`（跨拍对象），根据 Planner 返回的 task 决定复用 / 重建 / halt。每拍 tick 树产出按键，返回 SUCCESS / FAILURE / RUNNING。

**Key Design Points:**

1. **树的生命周期**（spec §7.6）：
   - task 身份（四元组）与上拍相同 → 复用 `active_tree`（进度保留）
   - task 身份变化 → halt 旧树并丢弃，按新 task 构建新树
   - 模式被抢占（Arbiter 通知）→ halt 并丢弃

2. **SUCCESS / FAILURE 处理**：
   - SUCCESS → 通知主循环释放该 task 的认领
   - FAILURE → 通知主循环：task 失败，Planner 下一拍重算

3. **Timeout 装饰**（spec §7.2）：每个 task 树用 `Timeout` 装饰（默认 `MAX_TASK_TICKS = 300`，约 15 秒），超时 → FAILURE。

- [ ] **Step 1: 写失败测试**

创建 `tests/test_executor.py`：

```python
"""Executor 执行层单测。

重点验证：
  1. task 身份相同时复用树（进度保留）。
  2. task 身份变化时 halt 并重建。
  3. 模式抢占时 halt 并丢弃树。
  4. SUCCESS / FAILURE 正确返回。
"""
import pytest
from ai import executor
from ai.tasks import Task
# ...

def test_executor_reuses_tree_when_task_identity_unchanged():
    """task 身份不变 → 复用树，进度保留。"""
    pass

def test_executor_rebuilds_tree_when_task_identity_changes():
    """task 身份变化 → halt 旧树，构建新树。"""
    pass

def test_executor_halts_tree_on_mode_preemption():
    """模式被抢占 → halt 并丢弃树。"""
    pass

def test_executor_returns_success_when_tree_completes():
    """树跑完 → 返回 SUCCESS。"""
    pass

def test_executor_returns_failure_on_timeout():
    """task 超时 → 返回 FAILURE。"""
    pass
```

- [ ] **Step 2: 实现 `ai/executor.py`**

```python
# -*- coding: utf-8 -*-
"""执行层包装（Executor）。

职责：
  管理 task 树的生命周期（复用 / 重建 / halt），每拍 tick 树产出按键。

与 oc/ai/ 的差异：
  - 旧实现：每个 Action 类内部手搓字符串状态机。
  - 新实现：用 bt.Sequence 管理顺序，Executor 只负责树的创建 / tick / 销毁。

Spec: §7 Executor、§7.6 树的生命周期
"""
from typing import Optional
from ai.bt import Node, Status, halt
from ai.primitives import Timeout

MAX_TASK_TICKS = 300  # task 级超时（拍数）

class Executor:
    """执行层：管理 task 树的生命周期。"""
    
    def __init__(self):
        self.active_tree = None  # type: Optional[Node]
        self.active_task_id = None  # type: Optional[tuple]
        self.task_start_tick = None  # type: Optional[int]
    
    def tick(self, task, ctx):
        # type: (Task, dict) -> str
        """执行一拍。
        
        返回: Status.SUCCESS / Status.FAILURE / Status.RUNNING
        """
        current_tick = ctx.get("tick", 0)
        task_id = task.identity()  # (TYPE, order_id, resources, item)
        
        # ① 检查 task 身份是否变化
        if task_id != self.active_task_id:
            self._rebuild_tree(task, ctx, current_tick)
        
        # ② tick 树
        status = self.active_tree.tick(ctx)
        
        # ③ 检查超时
        if current_tick - self.task_start_tick > MAX_TASK_TICKS:
            return Status.FAILURE
        
        return status
    
    def _rebuild_tree(self, task, ctx, current_tick):
        # type: (Task, dict, int) -> None
        """halt 旧树并构建新树。"""
        if self.active_tree:
            halt(self.active_tree)
        
        self.active_tree = Timeout(task.build(ctx), MAX_TASK_TICKS)
        self.active_task_id = task.identity()
        self.task_start_tick = current_tick
    
    def halt_and_discard(self):
        """被模式抢占时：halt 并丢弃树。"""
        if self.active_tree:
            halt(self.active_tree)
        self.active_tree = None
        self.active_task_id = None
        self.task_start_tick = None
```

- [ ] **Step 3: 跑通测试**

运行 `pytest tests/test_executor.py -v`，全绿。

---

### Task 3: Planner — 候选生成（单订单模式）

**Files:**
- Create: `ai/planner.py`（骨架 + 候选生成）
- Test: `tests/test_planner.py`（候选生成部分）

**Interfaces:**
- Consumes: `World`、`Blackboard`、当前追踪订单 `current_order_id`
- Produces: `(chosen_task, claims', decision_record)`

**Spec References:**
- §6.0 task 身份与候选生成
- §6.1 候选 task 全集（16 个 task）

Planner 是五步流水线（候选生成 → 可行性 → 协作 → 打分 → 承诺），Task 3-6 分四个子任务实现。Task 3 先实现骨架和候选生成。

**单订单模式简化**：
- `current_order_id` 由主循环管理（送完 / 过期后按 `remaining` 升序选下一单）
- 候选生成只看 `current_order_id` 对应的订单需求，不枚举所有订单
- 无 `order_id` 的 task（StashHeld / RecoverLoosePot / Discard / Prefetch）照常生成

- [ ] **Step 1: 写失败测试**

创建 `tests/test_planner.py`：

```python
"""Planner 规划层单测。

Task 3 只测候选生成部分。
"""
import pytest
from ai import planner
from ai.tasks import TaskType
# ...

def test_generate_candidates_for_single_order():
    """单订单模式：只为 current_order_id 生成候选。"""
    # 构造：两个订单，current_order_id 指向第一个
    # 断言：候选只含第一个订单的 task，不含第二个
    pass

def test_generate_cleanup_tasks_without_order_id():
    """StashHeld / RecoverLoosePot 等无 order_id 的 task 照常生成。"""
    pass

def test_no_candidates_when_current_order_is_none():
    """current_order_id 为 None → 候选为空，返回 Idle fallback。"""
    pass
```

- [ ] **Step 2: 实现 `ai/planner.py` 骨架**

```python
# -*- coding: utf-8 -*-
"""规划层（Planner）。

职责：
  五步流水线：候选生成 → 可行性 → 协作 → 打分 → 承诺。
  返回 (chosen_task, claims', decision_record)。

与 oc/ai/ 的差异：
  - 旧实现：决策逻辑混在 _build_tree() 和 task.decompose() 里。
  - 新实现：纯步进函数，无副作用，每拍重算。

Spec: §6 Planner
"""
from typing import List, Tuple, Dict, Optional
from ai.blackboard import Blackboard
from ai.tasks import Task, TaskType, TASK_TYPES

def plan(world, bb, current_order_id):
    # type: (World, Blackboard, Optional[str]) -> Tuple[Task, Dict, Dict]
    """规划一拍。
    
    返回: (chosen_task, claims', decision_record)
    """
    # ① 候选生成
    candidates = _generate_candidates(world, bb, current_order_id)
    
    # ② 可行性过滤（Task 4）
    feasible = _filter_feasible(candidates, world, bb)
    
    # ③ 协作过滤（Task 4，本计划为空壳）
    after_collab = _filter_collaboration(feasible, world, bb)
    
    # ④ 打分（Task 5）
    scored = _score_tasks(after_collab, world, bb)
    
    # ⑤ 承诺惯性（Task 6）
    chosen, claims_new = _apply_commitment_inertia(scored, world, bb)
    
    # ⑥ 构造决策记录
    record = _build_decision_record(candidates, feasible, scored, chosen)
    
    return chosen, claims_new, record

def _generate_candidates(world, bb, current_order_id):
    # type: (World, Blackboard, Optional[str]) -> List[Task]
    """生成候选 task（单订单模式）。"""
    candidates = []
    
    if current_order_id is None:
        # 无订单可做，只生成清理 task
        candidates.extend(_generate_cleanup_tasks(world, bb))
        return candidates
    
    # 为 current_order_id 生成候选
    order = _find_order_by_id(world, current_order_id)
    if not order:
        return candidates
    
    # 枚举该订单的未满足需求
    # （简化：从 order.composition 与已就位盘的 contents diff）
    requirements = _order_requirements(order, world, bb)
    
    for req in requirements:
        # 为该需求生成能推进它的 task
        candidates.extend(_generate_tasks_for_requirement(req, order, world, bb))
    
    # 加入清理 task
    candidates.extend(_generate_cleanup_tasks(world, bb))
    
    return candidates

def _generate_cleanup_tasks(world, bb):
    # type: (World, Blackboard) -> List[Task]
    """生成清理 task（无 order_id）。"""
    tasks = []
    # StashHeld、RecoverLoosePot、Discard
    # ...
    return tasks

def _order_requirements(order, world, bb):
    # type: (dict, World, Blackboard) -> List[str]
    """计算订单的未满足需求。
    
    返回需求列表，如 ["BoiledSushiRice", "SushiFish", "Seaweed"]。
    """
    # 读取 order.composition，与已就位盘的 contents 比对
    pass

def _generate_tasks_for_requirement(req, order, world, bb):
    # type: (str, dict, World, Blackboard) -> List[Task]
    """为一个需求生成能推进它的 task。
    
    例如 req="BoiledSushiRice" → FetchToCooker / TakeCookedToPlate
    """
    pass
```

- [ ] **Step 3: 实现候选生成逻辑**

实现 `_order_requirements` 和 `_generate_tasks_for_requirement`。关键点：

1. 从 `order.composition` 读取需求列表。
2. 找到该订单的已就位盘（从 `bb.claims` 中找 `order_id` 匹配且 action 为 `"assemble"` 的盘认领）。
3. 用 `recipes.plate_has` 检查盘里是否已有该料（注意装盘态词汇映射，Plan 2 已实现）。
4. 对未满足的需求，按类型生成对应 task：
   - 需烹饪 → `FetchToCooker` / `TakeCookedToPlate`
   - 需切配 → `FetchToBoard` / `Chop`
   - 直接放 → `FetchToPlate`
   - 盘未就位 → `PlaceCleanPlate` / `WashPlate`

- [ ] **Step 4: 跑通候选生成测试**

运行 `pytest tests/test_planner.py::test_generate_candidates_for_single_order -v`，绿。

---

### Task 4: Planner — 可行性 + 协作过滤

**Files:**
- Modify: `ai/planner.py`（添加 `_filter_feasible` / `_filter_collaboration`）
- Test: `tests/test_planner.py`（可行性部分）

**Spec References:**
- §4.2 可行性必须读 claims
- §6.2 可行性过滤
- §6.3 协作过滤（本计划为空壳）

- [ ] **Step 1: 写可行性测试**

```python
def test_filter_feasible_reads_claims():
    """可行性过滤读取 claims：被他人认领的资源判为不可行。"""
    # 构造：cooker 被 P1 认领（mock，本计划无 P1 推断）
    # FetchToCooker(cooker) 判为不可行
    pass

def test_filter_feasible_with_hands_requirement():
    """手持状态纳入可行性：手持盘时 FetchToCooker 不可行（需空手）。"""
    pass
```

- [ ] **Step 2: 实现 `_filter_feasible`**

```python
def _filter_feasible(candidates, world, bb):
    # type: (List[Task], World, Blackboard) -> List[Task]
    """可行性过滤。
    
    全局前置（spec §4.2）：task 所需的每个 (resource, action) 未被他人认领。
    各 task 自己的可行性：调用 task.feasible(ctx)。
    """
    feasible = []
    for task in candidates:
        # ① 检查认领
        if not _claims_available(task, bb):
            continue
        
        # ② 检查 task 自己的可行性
        ctx = _make_context(world, bb)
        if task.feasible(ctx):
            feasible.append(task)
    
    return feasible

def _claims_available(task, bb):
    # type: (Task, Blackboard) -> bool
    """检查 task 所需的认领是否可得。"""
    required_claims = task.claims()  # [(resource_id, action), ...]
    for res_id, action in required_claims:
        claim_key = (res_id, action)
        if claim_key in bb.claims:
            claim = bb.claims[claim_key]
            # 被他人持有 且 未过 TTL
            if claim.owner != "P2" and not _claim_expired(claim, ctx["tick"]):
                return False
    return True
```

- [ ] **Step 3: 实现协作过滤（空壳）**

```python
def _filter_collaboration(feasible, world, bb):
    # type: (List[Task], World, Blackboard) -> List[Task]
    """协作过滤（本计划为空壳）。
    
    Plan 4 实现：
      - 接力模式：对 p1_active_task 做分数惩罚（不在此处筛掉）。
      - 分单模式：只留 order_assignment == "P2" 的 task。
    
    Plan 3：直接透传。
    """
    return feasible
```

- [ ] **Step 4: 跑通测试**

运行 `pytest tests/test_planner.py -k feasible -v`，绿。

---

### Task 5: Planner — 打分（最小权重集）

**Files:**
- Modify: `ai/planner.py`（添加 `_score_tasks`）
- Test: `tests/test_planner.py`（打分部分）

**Spec References:**
- §6.4 打分：六项加权（Plan 3 只启用三项）

Plan 3 的最小权重集：
1. **订单紧迫**：`score += w_urgency * (1.0 / max(order.remaining, 1))`
2. **时间压力**：`cooked → burning` 分数上升，`score += w_pressure * _time_pressure(task, world)`
3. **ETA**：`score -= w_eta * task.eta(world, bb)`（距离越近分越高）

其余权重（异步资源、接力、完成度、料复用、combo、截止前瞻）归 Plan 4。

- [ ] **Step 1: 写打分测试**

```python
def test_score_tasks_urgency():
    """订单紧迫：remaining 越小分数越高。"""
    pass

def test_score_tasks_time_pressure():
    """时间压力：cooked → burning 的锅，Discard 分数上升。"""
    pass

def test_score_tasks_eta():
    """ETA：距离越近分数越高。"""
    pass
```

- [ ] **Step 2: 实现 `_score_tasks`**

```python
# 权重常量（Plan 3 最小集）
W_URGENCY = 5.0
W_PRESSURE = 3.0
W_ETA = 0.1

def _score_tasks(tasks, world, bb):
    # type: (List[Task], World, Blackboard) -> List[Tuple[Task, float, Dict]]
    """打分。
    
    返回: [(task, score, terms), ...]
    terms 是各项分数的拆解，用于决策日志。
    """
    scored = []
    for task in tasks:
        terms = {}
        score = 0.0
        
        # ① 订单紧迫
        if task.order_id:
            order = _find_order_by_id(world, task.order_id)
            if order:
                urgency = 1.0 / max(order["remaining"], 1)
                terms["urgency"] = W_URGENCY * urgency
                score += terms["urgency"]
        
        # ② 时间压力
        pressure = _time_pressure(task, world)
        terms["pressure"] = W_PRESSURE * pressure
        score += terms["pressure"]
        
        # ③ ETA
        ctx = _make_context(world, bb)
        eta = task.eta(ctx)  # 调用 task 的 eta 方法（依赖 navigation.distance_field）
        terms["eta"] = -W_ETA * eta
        score += terms["eta"]
        
        scored.append((task, score, terms))
    
    return scored

def _time_pressure(task, world):
    # type: (Task, World) -> float
    """时间压力：cooked → burning 的锅越快糊，分数越高。"""
    # Discard / RescueCooked / TakeCookedToPlate 涉及的锅
    if task.TYPE in [TaskType.DISCARD, TaskType.RESCUE_COOKED, TaskType.TAKE_COOKED_TO_PLATE]:
        cooker_id = task.resources.get("cooker_id")
        if cooker_id:
            cooker = _find_cooker(world, cooker_id)
            if cooker and cooker.get("cooked"):
                # 简化：cooked 但未 burning 时给固定分，burning 时给更高分
                if cooker.get("burning"):
                    return 2.0
                else:
                    return 1.0
    return 0.0
```

- [ ] **Step 3: 跑通打分测试**

运行 `pytest tests/test_planner.py -k score -v`，绿。

---

### Task 6: Planner — 承诺惯性

**Files:**
- Modify: `ai/planner.py`（添加 `_apply_commitment_inertia`）
- Test: `tests/test_planner.py`（承诺惯性场景单测）

**Spec References:**
- §6.5 承诺惯性：防抖 + 两条规则（进度项 + 不可行迟滞）

- [ ] **Step 1: 写承诺惯性场景测试**

```python
def test_commitment_inertia_progress_bonus():
    """进度项：离出餐口一步时，另一锅 burning 不改主意。"""
    # 构造：当前 task = DeliverPlate，steps_done=2 / est_steps=3
    #       候选中有 Discard（burning 锅），分数更高
    # 断言：仍选 DeliverPlate（进度加成抬高改选门槛）
    pass

def test_commitment_inertia_infeasible_streak():
    """不可行迟滞：料被 P1 挡住一帧，不弃当前承诺。"""
    # 构造：当前 task 连续 1 拍不可行（未达 INFEASIBLE_THRESHOLD）
    # 断言：仍保持承诺，不改选
    pass

def test_commitment_abandoned_after_persistent_infeasible():
    """连续 M 拍不可行 → 放弃承诺。"""
    pass
```

- [ ] **Step 2: 实现 `_apply_commitment_inertia`**

```python
# 常量
COMMITMENT_MARGIN = 2.0  # 改选门槛（分数差）
PROGRESS_BONUS_PER_STEP = 1.0  # 每完成一步的进度加成
INFEASIBLE_THRESHOLD = 3  # 连续多少拍不可行才放弃

def _apply_commitment_inertia(scored, world, bb):
    # type: (List[Tuple[Task, float, Dict]], World, Blackboard) -> Tuple[Task, Dict]
    """承诺惯性：除非完成 / 失败 / 连续不可行 / 出现显著更高分者，否则不改选。
    
    返回: (chosen_task, claims_new)
    """
    if not scored:
        # 无候选，返回 Idle fallback
        return _idle_task(), {}
    
    # 按分数降序排序
    scored.sort(key=lambda x: x[1], reverse=True)
    top_task, top_score, top_terms = scored[0]
    
    # 检查是否有当前承诺
    if bb.commitment and bb.commitment.active_task:
        current_task_id = bb.commitment.active_task
        current = _find_task_in_scored(scored, current_task_id)
        
        if current:
            current_task, current_score, current_terms = current
            
            # ① 检查不可行连续性
            if current_task.identity() not in [t.identity() for t, _, _ in scored]:
                # 当前 task 已不在可行候选中
                bb.commitment.infeasible_streak += 1
                if bb.commitment.infeasible_streak < INFEASIBLE_THRESHOLD:
                    # 未达阈值，保持承诺
                    return current_task, _compute_claims(current_task)
                else:
                    # 连续不可行，放弃
                    bb.commitment = None
            else:
                # 重置不可行计数
                bb.commitment.infeasible_streak = 0
            
            # ② 计算进度加成
            progress_bonus = (bb.commitment.steps_done / max(current_task.est_steps(), 1)) * PROGRESS_BONUS_PER_STEP
            threshold = COMMITMENT_MARGIN + progress_bonus
            
            # ③ 若 top 分数未显著高于 current，保持承诺
            if top_score - current_score < threshold:
                return current_task, _compute_claims(current_task)
    
    # 无承诺 或 改选：选分数最高者
    return top_task, _compute_claims(top_task)

def _compute_claims(task):
    # type: (Task) -> Dict
    """计算 task 所需的认领。
    
    返回格式: {(resource_id, action): Claim(...), ...}
    """
    claims_new = {}
    for res_id, action in task.claims():
        claims_new[(res_id, action)] = {
            "owner": "P2",
            "order_id": task.order_id,
            "claimed_tick": ctx["tick"],
            "ttl": 200  # 默认 TTL
        }
    return claims_new
```

- [ ] **Step 3: 跑通承诺惯性测试**

运行 `pytest tests/test_planner.py -k commitment -v`，绿。

---

### Task 7: Trace（决策日志）

**Files:**
- Create: `ai/trace.py`
- Test: `tests/test_trace.py`

**Interfaces:**
- Consumes: `decision_record`（来自 Planner）、当前模式、执行状态、按键
- Produces: JSONL 行，写入 `/tmp/agent.log`

**Spec References:**
- §8.3 决策可观测性：JSONL 格式 + ASCII 渲染

- [ ] **Step 1: 写格式校验测试**

```python
"""Trace 决策日志单测。

重点验证：
  1. JSONL 格式正确（每行是合法 JSON）。
  2. 必需字段存在：t / mode / candidates / commit / exec / keys。
  3. candidates 数组每项含 task / score / ok / terms。
"""
import pytest
import json
from ai import trace

def test_trace_format_valid_jsonl():
    """每行是合法 JSON。"""
    pass

def test_trace_contains_required_fields():
    """必需字段存在。"""
    pass

def test_trace_candidates_structure():
    """candidates 数组结构正确。"""
    pass
```

- [ ] **Step 2: 实现 `ai/trace.py`**

```python
# -*- coding: utf-8 -*-
"""决策日志（Trace）。

职责：
  每拍输出一条 JSONL，记录模式 / 候选 / 打分 / 选中 task / 执行状态 / 按键。

Spec: §8.3 决策可观测性
"""
import json
from typing import Dict

TRACE_PATH = "/tmp/agent.log"

def emit(record, mode, commit, exec_state, keys, tick):
    # type: (Dict, str, Dict, Dict, List[str], int) -> None
    """输出一拍的决策日志。
    
    Args:
        record: Planner 返回的 decision_record
        mode: 当前模式
        commit: bb.commitment
        exec_state: {"node": "...", "status": "..."}
        keys: 本拍按下的键列表
        tick: 当前拍号
    """
    log_entry = {
        "t": tick,
        "mode": mode,
        "mode_reason": record.get("mode_reason", ""),
        "p1": record.get("p1", {}),
        "candidates": _format_candidates(record.get("candidates", [])),
        "commit": _format_commitment(commit),
        "exec": exec_state,
        "keys": keys
    }
    
    with open(TRACE_PATH, "a") as f:
        f.write(json.dumps(log_entry) + "\n")

def _format_candidates(candidates):
    # type: (List) -> List[Dict]
    """格式化候选列表。"""
    formatted = []
    for cand in candidates:
        task, score, terms, ok, veto, chosen = cand
        formatted.append({
            "task": task.describe(),  # 如 "DeliverPlate(plate3, order#1)"
            "score": round(score, 2),
            "ok": ok,
            "veto": veto,
            "terms": {k: round(v, 2) for k, v in terms.items()},
            "chosen": chosen
        })
    return formatted

def _format_commitment(commit):
    # type: (Dict) -> Dict
    """格式化承诺状态。"""
    if not commit or not commit.get("active_task"):
        return {}
    return {
        "task": commit["active_task"],
        "ticks": commit.get("started_tick", 0)
    }
```

- [ ] **Step 3: 跑通格式测试**

运行 `pytest tests/test_trace.py -v`，绿。

---

### Task 8: Agent 主循环（13 步装配）

**Files:**
- Create: `ai/agent.py`（新主循环，取代 `oc/ai/agent.py`）
- Test: `tests/test_agent.py`

**Interfaces:**
- Consumes: `World` 快照（从 `oc/ai/world.StateClient` 接收）
- Produces: 按键输出（通过 `Controller`）

**Spec References:**
- §8.1 主循环十三步
- §4.1 认领的四个释放触发点

新 `ai/agent.py` 是运行时入口，取代 `oc/ai/agent.py`（旧文件保留作对照）。主循环每拍执行 13 步装配，集成 Arbiter / Planner / Executor / Trace。

- [ ] **Step 1: 写主循环单测**

```python
"""Agent 主循环单测。

重点验证：
  1. 13 步执行顺序正确。
  2. 步骤 ⑨ 写回 claims、步骤 ⑩ 登记 commitment。
  3. 认领的四个释放触发点生效。
"""
import pytest
from ai import agent

def test_main_loop_13_steps():
    """主循环 13 步按序执行。"""
    pass

def test_main_loop_writes_back_claims():
    """步骤 ⑨ 写回 claims 到黑板。"""
    pass

def test_main_loop_registers_commitment():
    """步骤 ⑩ 登记 commitment。"""
    pass

def test_main_loop_releases_claims_on_task_success():
    """task SUCCESS → 释放该 task 的认领。"""
    pass

def test_main_loop_releases_claims_on_order_gone():
    """订单消失 → 释放该订单的所有认领。"""
    pass
```

- [ ] **Step 2: 实现 `ai/agent.py` 骨架**

```python
# -*- coding: utf-8 -*-
"""AI 主循环（新架构）。

职责：
  13 步装配：对账 → 推断 → 仲裁 → 规划 → 执行 → 日志。

与 oc/ai/agent.py 的差异：
  - 旧实现：1180 行 God Object，决策 / 建树 / 谓词 / 寻路混同。
  - 新实现：~100 行装配层，只做组装，逻辑在各层。

Spec: §8.1 主循环
"""
import time
from ai import arbiter, planner, executor, trace, reconcile
from ai.blackboard import Blackboard
from ai.world import World, StateClient
from ai.controller import Controller
from ai.hands import Hands
from ai.bb_factory import make_bb

class Agent:
    """AI 主循环。"""
    
    def __init__(self):
        self.client = StateClient()
        self.bb = make_bb()
        self.arbiter = arbiter
        self.planner = planner
        self.executor = executor.Executor()
        self.controller = Controller()
        self.hands = Hands(self.controller)
        self.tick = 0
        self.current_order_id = None  # 单订单模式：当前追踪的订单
    
    def run(self):
        """主循环入口。"""
        while True:
            try:
                # ① 收帧 → World
                snapshot = self.client.receive()
                if not snapshot:
                    time.sleep(0.05)
                    continue
                
                world = World(snapshot)
                
                # ② 若 scratch.skip_ticks > 0 → 松键、递减、return
                if self.bb.scratch and getattr(self.bb.scratch, "skip_ticks", 0) > 0:
                    self.hands.release_all()
                    self.bb.scratch.skip_ticks -= 1
                    time.sleep(0.05)
                    continue
                
                # ③ 认领对账
                reconcile.reconcile_claims(world, self.bb)
                
                # ④ P1 推断（本计划跳过，Plan 4 实现）
                # p1model.update(world, self.bb)
                
                # ⑤ 卡住检测（跨模式前置守卫）
                # （已集成在 arbiter.select_mode 里）
                
                # ⑥ 模式仲裁
                new_mode, mode_reason = self.arbiter.select_mode(world, self.bb)
                
                # ⑦ 模式变了 → halt 并丢弃树 + 丢弃 scratch
                if new_mode != self.bb.mode:
                    self.executor.halt_and_discard()
                    self.arbiter.on_mode_transition(self.bb.mode, new_mode, self.bb)
                    self.bb.mode = new_mode
                
                # ⑧ 选择 / 更新当前订单（单订单模式）
                self._update_current_order(world)
                
                # ⑨ 规划
                if new_mode == "Normal":
                    chosen_task, claims_new, record = self.planner.plan(world, self.bb, self.current_order_id)
                else:
                    # 中断模式：取固定 task（Firefighting / Stuck / Assigned）
                    chosen_task = self._get_interrupt_task(new_mode, world, self.bb)
                    claims_new = {}
                    record = {}
                
                # ⑩ 写回认领
                self.bb.claims.update(claims_new)
                
                # ⑪ 登记 / 更新 commitment
                self._update_commitment(chosen_task)
                
                # ⑫ 执行
                ctx = self._make_context(world)
                status = self.executor.tick(chosen_task, ctx)
                
                # ⑬ 处理 SUCCESS / FAILURE
                if status == "success":
                    self._release_task_claims(chosen_task)
                    self.bb.commitment = None
                elif status == "failure":
                    self.bb.commitment.interrupt_failure_count += 1
                    self.bb.commitment = None
                
                # ⑭ 决策日志
                exec_state = {"node": "...", "status": status}
                keys = self.hands.get_active_keys()
                trace.emit(record, new_mode, self.bb.commitment, exec_state, keys, self.tick)
                
                self.tick += 1
                time.sleep(0.05)
            
            except KeyboardInterrupt:
                self.hands.release_all()
                break
    
    def _update_current_order(self, world):
        """更新当前追踪的订单（单订单模式）。"""
        # 若当前订单已送完 / 过期，从剩余订单中按 remaining 升序选下一单
        pass
    
    def _update_commitment(self, task):
        """登记 / 更新 commitment。"""
        if not self.bb.commitment or self.bb.commitment.active_task != task.identity():
            self.bb.commitment = {
                "active_task": task.identity(),
                "started_tick": self.tick,
                "steps_done": 0,
                "infeasible_streak": 0,
                "interrupt_failure_count": 0
            }
    
    def _release_task_claims(self, task):
        """释放 task 的认领（四个释放点之一）。"""
        for res_id, action in task.claims():
            claim_key = (res_id, action)
            if claim_key in self.bb.claims:
                del self.bb.claims[claim_key]
    
    def _make_context(self, world):
        """构造执行上下文。"""
        return {
            "world": world,
            "bb": self.bb,
            "hands": self.hands,
            "tick": self.tick
        }
    
    def _get_interrupt_task(self, mode, world, bb):
        """获取中断模式的固定 task。"""
        # Firefighting → Firefighting task
        # Stuck → Nudge / MarkBlocked / AbandonTarget
        pass
```

- [ ] **Step 3: 实现订单选择逻辑**

```python
def _update_current_order(self, world):
    """更新当前追踪的订单（单订单模式）。
    
    规则：
      - 当前订单送完 / 过期 → 从剩余订单按 remaining 升序选下一单。
      - 无订单 → current_order_id = None。
    """
    if self.current_order_id:
        # 检查当前订单是否还在
        order = self._find_order_by_id(world, self.current_order_id)
        if not order:
            # 已送完 / 过期，选下一单
            self.current_order_id = None
    
    if self.current_order_id is None:
        # 从剩余订单中选 remaining 最小的
        orders = world.orders
        if orders:
            orders_sorted = sorted(orders, key=lambda o: o.get("remaining", 999))
            self.current_order_id = orders_sorted[0].get("id")
```

- [ ] **Step 4: 实现认领释放的四个触发点**

检查代码确认四个释放点都已实现：
1. **task 成功**：`_release_task_claims`（步骤 ⑬）
2. **订单消失**：`reconcile.reconcile_claims`（步骤 ③）
3. **资源消失 / 内容不符**：`reconcile.reconcile_claims`（步骤 ③）
4. **TTL 兜底**：`reconcile.reconcile_claims`（步骤 ③）

- [ ] **Step 5: 跑通主循环单测**

运行 `pytest tests/test_agent.py -v`，绿。

---

### Task 9: 端到端冒烟测试

**Files:**
- Test: `tests/test_end_to_end.py`

**Goal:** 用假帧 → 主循环 → 验证按键序列，不接真实游戏。确认 13 步装配正确、无崩溃、决策日志可回放。

- [ ] **Step 1: 编写端到端冒烟测试**

```python
"""端到端冒烟测试。

用假帧喂给主循环，验证：
  1. 主循环不崩溃。
  2. 产出按键序列合理。
  3. 决策日志 JSONL 可解析。
"""
import pytest
from ai.agent import Agent
from ai.world import World
# ...

def test_end_to_end_smoke():
    """端到端冒烟：假帧 → 主循环 → 按键 + 日志。"""
    # 构造一个简单场景：AI 空手、有干净盘、有订单需一个黄瓜
    # 喂给主循环跑 50 拍
    # 断言：
    #   - 选中 PlaceCleanPlate / FetchToPlate / DeliverPlate 等合理 task
    #   - 按键序列包含移动 + pickup + placement
    #   - /tmp/agent.log 有 50 行 JSONL
    pass

def test_end_to_end_firefighting_interrupt():
    """端到端：Normal → Firefighting → Normal。"""
    # 构造：开始正常做菜，中途着火，灭火后继续
    pass
```

- [ ] **Step 2: 跑通端到端冒烟**

运行 `pytest tests/test_end_to_end.py -v`，绿。

- [ ] **Step 3: 清理测试日志**

确保 `/tmp/agent.log` 在测试前清空或使用独立路径，避免干扰。

---

## 验收标准（Plan 3）

按 spec §11.2 阶段 4 的定性门槛：

1. **实机定性门槛**（不比分数）：
   - [ ] 完整做出并送出**至少一道**正确菜
   - [ ] 无卡键（所有按键在 halt 时正确释放）
   - [ ] 无模式死锁（火中被堵时 Stuck 守卫生效、Firefighting 正常退出）
   - [ ] 无楔死资源（认领的四个释放点生效，30 分钟浸泡无泄漏）
   - [ ] trace 可回放（/tmp/agent.log 每拍一条 JSONL，格式正确，candidates 含 terms 拆解）

2. **代码标准**：
   - [ ] `ai/agent.py` ≤ 180 行（主循环只做装配）
   - [ ] 无单个文件 > 400 行
   - [ ] 无 `self.sub` 式共享字符串状态机

3. **测试覆盖**：
   - [ ] 全部单测绿（Arbiter / Executor / Planner / Trace / Agent）
   - [ ] 端到端冒烟通过
   - [ ] 场景单测覆盖 Review Focus 的 7 类关键输入

---

## 已知缺口与 Plan 4 交接

Plan 3 完成后，以下能力归 Plan 4：

1. **多订单并行**：穿插、多盘多灶认领、料复用打分。
2. **P1 协同**：`p1model.py`、接力 / 分单模式、站位软代价图。
3. **打分权重完整集**：异步资源、接力加成、完成度、combo、截止前瞻。
4. **收尾 task**：`RescueCooked` / `ResetPlate` / `Prefetch`（Plan 3 可能不用到）。
5. **浸泡测试**：30 分钟实机运行，验证认领泄漏 / 模式死锁 / 承诺抖动。

Plan 3 交付的是**单订单模式的完整闭环**，为 Plan 4 打下可验证的基线。

---

## Ledger

| Task | Status | Commit | Notes |
|---|---|---|---|
| Task 1: Arbiter | ⬜ Not started | | 模式仲裁 + 卡住守卫 |
| Task 2: Executor | ⬜ Not started | | 树生命周期管理 |
| Task 3: Planner - 候选生成 | ⬜ Not started | | 单订单模式简化 |
| Task 4: Planner - 可行性 + 协作 | ⬜ Not started | | 协作过滤为空壳 |
| Task 5: Planner - 打分 | ⬜ Not started | | 最小权重集（三项）|
| Task 6: Planner - 承诺惯性 | ⬜ Not started | | 进度项 + 不可行迟滞 |
| Task 7: Trace | ⬜ Not started | | JSONL 决策日志 |
| Task 8: Agent 主循环 | ⬜ Not started | | 13 步装配 |
| Task 9: 端到端冒烟 | ⬜ Not started | | 假帧 → 按键 + 日志 |

---

**Plan 3 总行数估计**: ~1400 行代码 + ~800 行测试 = 2200 行

**预计工期**: 9 个任务 × 平均 1.5 小时 ≈ 13-15 小时（含测试调试）

**风险**: Plan 3 是首次替换运行时行为，多层同时集成。缓解：前 7 个任务各自单测 / 冒烟，Task 8-9 才端到端集成，面积最小。
